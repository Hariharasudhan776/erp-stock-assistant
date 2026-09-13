"""Web server for the stock assistant.

    python app.py            -> http://127.0.0.1:8765  (opens the browser)

Standard-library HTTP server. Sign-in with username/password (see auth.py), one
conversation per signed-in session, streaming chat endpoint, admin API, and the
hardening measures listed in SECURITY_NOTES (shown in the admin panel).
"""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import auth
import db
import erp_roles
import store
from agent import Chat, cost_estimates, usage_summary
from config import CFG, ROOT

try:
    import knowledge as _kn  # private deployment config (git-ignored)
except ImportError:
    import knowledge_example as _kn
BRAND = getattr(_kn, "BRAND", {"name": "Stock Assistant", "subtitle": ""})
LIBRARY = getattr(_kn, "LIBRARY", [])

STATIC = ROOT / "static"
COOKIE = "adk_session"
MAX_BODY = 64 * 1024

_chats: dict[str, Chat] = {}  # session token -> conversation
_chats_lock = threading.Lock()

SECURITY_NOTES = [
    "Sign-in required for every page action and API call; passwords stored as salted PBKDF2-SHA256 hashes (600k iterations), never in plain text.",
    "Account lockout: 5 failed attempts lock the username for 15 minutes; a generic error hides whether the username exists; failed attempts are audited.",
    "Sessions: random 256-bit token in an HttpOnly, Secure, SameSite=Strict cookie; 12 h lifetime, 90 min idle timeout; all sessions of a user are revoked when their password or role changes.",
    "Roles come from the ERP: a person's active ERP groups are read at sign-in and mapped to modules; each module has a fixed table list enforced server-side on every SQL statement, not just by the prompt. Admin is a fixed list of ERP usernames, never a user-type flag.",
    "Credential tables and password columns of the ERP are unreadable through the assistant for every role, admins included.",
    "ERP sign-in verifies the typed password against the ERP's stored hash in constant time; the hash never leaves the server and is never shown to the model.",
    "Database: dedicated read-only account, session opened READ ONLY, SELECT-only validator (comments and literals stripped before scanning), one statement per call, row cap and query timeout.",
    "Prompt hardening: the model only sees query results as data; it cannot save knowledge or browse the schema unless the signed-in user is an admin; restricted users get a fixed refusal for anything outside stock.",
    "Web: Content-Security-Policy, X-Frame-Options DENY, nosniff, no-referrer, Permissions-Policy; the API rejects requests without the fetch header (CSRF); request bodies capped at 64 KB; error messages never include stack traces.",
    "Output rendering: model markdown is sanitised with DOMPurify before it touches the page.",
    "Spend control: global and per-user daily budgets stop the AI service when reached; every question's cost is logged per user.",
    "Audit trail: sign-ins, failed sign-ins, questions, every SQL executed (with user and row count), denied queries, admin actions and knowledge changes are written to logs/audit.jsonl.",
    "Secrets: API key and DB credentials come from .env / OS environment, never from the repository; the schema notes and logs are git-ignored.",
    "Network: the server listens on 127.0.0.1 only; remote access goes through an HTTPS tunnel, and the same sign-in applies there.",
]

CSP = (
    "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


def _chat_for(token: str, sess: dict) -> Chat:
    with _chats_lock:
        c = _chats.get(token)
        if c is None or c.user != sess["user"] or c.role != sess["role"]:
            c = Chat(sess["user"], sess["role"], access=sess.get("access"))
            _chats[token] = c
        return c


def _drop_chat(token: str | None) -> None:
    if token:
        with _chats_lock:
            _chats.pop(token, None)


class _ClientGone(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "stock-assistant"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: N802 - quiet console
        if self.path.startswith("/api/chat"):
            sys.stdout.write("%s - chat\n" % self.address_string())

    # ---- plumbing ------------------------------------------------------
    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, status=200, set_cookie: str | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self._write(body)

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            ctype = "text/javascript"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self._write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.rfile.read(min(n, MAX_BODY))
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b""
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _client_ip(self) -> str:
        return (self.headers.get("Cf-Connecting-Ip") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def _token(self) -> str | None:
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return m.value if m else None

    def _session(self) -> dict | None:
        return auth.get_session(self._token())

    @staticmethod
    def _cookie(token: str, max_age: int) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={max_age}"

    def handle(self):  # noqa: D401
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- GET -------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if u.path.startswith("/static/"):
            target = (STATIC / u.path[len("/static/"):]).resolve()
            if STATIC.resolve() not in target.parents:
                return self._json({"error": "forbidden"}, 403)
            return self._file(target)
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if u.path == "/api/branding":  # public: the sign-in page needs the name
            return self._json({"brand": BRAND, "library": LIBRARY})

        sess = self._session()
        if u.path == "/api/me":
            if not sess:
                return self._json({"error": "sign in required", "users_exist": auth.user_count() > 0}, 401)
            return self._json(_chat_for(self._token(), sess).status())
        if not sess:
            return self._json({"error": "sign in required"}, 401)
        chat = _chat_for(self._token(), sess)

        if u.path == "/api/status":
            return self._json(chat.status())
        if u.path == "/api/history":
            return self._json(chat.history())
        if u.path == "/api/dbcheck":
            try:
                res = db.run_select("select sysdate now, user usr from dual", role="admin", max_rows=1)
                return self._json({"ok": True, "now": res.rows[0][0], "user": res.rows[0][1], "db": db.connection_summary()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e).splitlines()[0][:200]}, 500)
        if u.path == "/api/csv":
            q = parse_qs(u.query)
            try:
                idx = int(q.get("i", ["-1"])[0])
                name, text = chat.result_csv(idx)
            except (ValueError, IndexError):
                return self._json({"error": "no such result"}, 404)
            data = text.encode("utf-8-sig")
            store.audit("csv_download", sess["user"], index=idx, rows=max(0, text.count("\n") - 1))
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            return self._write(data)
        if u.path == "/api/admin/overview":
            if sess["role"] != "admin":
                return self._json({"error": "admin only"}, 403)
            return self._json({
                "users": auth.list_users(),
                "settings": store.settings(),
                "models": [{"id": m, "label": l, "note": n} for m, l, n in store.MODELS],
                "efforts": list(store.EFFORTS),
                "usage": usage_summary(),
                "costs": cost_estimates(),
                "knowledge": store.notes(),
                "feedback": store.recent_feedback(30),
                "audit": store.recent_audit(40),
                "erp": {
                    "admin_users": sorted(erp_roles.ADMIN_ERP_USERS),
                    "modules": [{"key": k, "label": m.get("label", k), "enabled": bool(m.get("tables")), "tables": len(m.get("tables", []))} for k, m in erp_roles.MODULES.items()],
                },
                "security": {
                    "active_sessions": auth.active_sessions(),
                    "failed_logins_24h": auth.failed_logins_today(),
                    "locked_accounts": auth.locked_accounts(),
                    "notes": SECURITY_NOTES,
                },
            })
        if u.path == "/api/admin/groups":
            if sess["role"] != "admin":
                return self._json({"error": "admin only"}, 403)
            try:
                groups = erp_roles.erp_all_groups()
            except Exception as e:
                return self._json({"error": "ERP lookup failed: " + str(e).splitlines()[0][:150]}, 500)
            mapping = erp_roles.group_modules()
            low = {k.lower(): v for k, v in mapping.items()}
            return self._json({
                "groups": [{"group": g["group"], "members": g["members"], "modules": low.get(g["group"].lower(), [])} for g in groups],
                "modules": [{"key": k, "label": m.get("label", k), "enabled": bool(m.get("tables"))} for k, m in erp_roles.MODULES.items()],
                "overrides": store.group_overrides(),
            })
        return self._json({"error": "not found"}, 404)

    # ---- POST ------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        try:
            body = self._read_json()  # drain first, or a 4xx can reset the socket before the client reads it
        except ValueError as e:
            return self._json({"error": str(e)}, 413)
        if self.headers.get("X-Requested-With") != "fetch":  # CSRF: browsers cannot add this header cross-site without CORS
            return self._json({"error": "bad request"}, 400)
        ip = self._client_ip()

        if u.path == "/api/login":
            username = str(body.get("username", ""))[:64]
            password = str(body.get("password", ""))[:256]
            token, info = auth.authenticate(username, password, ip)
            if not token:
                store.audit("login_failed", auth.normalize_username(username) or None, ip=ip, reason=info)
                return self._json({"error": info}, 429 if info.startswith("Too many") else 401)
            store.audit("login", auth.normalize_username(username), ip=ip)
            return self._json({"ok": True, "user": auth.normalize_username(username), "role": info},
                              set_cookie=self._cookie(token, auth.SESSION_TTL_SEC))

        sess = self._session()
        if not sess:
            return self._json({"error": "sign in required"}, 401)
        token = self._token()
        user = sess["user"]

        if u.path == "/api/logout":
            auth.revoke(token)
            _drop_chat(token)
            store.audit("logout", user, ip=ip)
            return self._json({"ok": True}, set_cookie=self._cookie("", 0))
        if u.path == "/api/password":
            try:
                if not auth.check_password(user, str(body.get("current", ""))):
                    return self._json({"error": "Current password is incorrect."}, 400)
                auth.set_password(user, str(body.get("new", "")), by=user)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            store.audit("password_changed", user, ip=ip)
            return self._json({"ok": True, "signed_out": True})

        chat = _chat_for(token, sess)
        if u.path == "/api/reset":
            _drop_chat(token)
            _chat_for(token, sess)
            return self._json({"ok": True})
        if u.path == "/api/feedback":
            try:
                res = chat.feedback(int(body.get("turn", -1)), str(body.get("vote", "")), str(body.get("text", ""))[:2000])
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e) or "bad request"}, 400)
            return self._json(res)
        if u.path == "/api/chat":
            question = str(body.get("message") or "").strip()
            if not question:
                return self._json({"error": "empty message"}, 400)
            if len(question) > 4000:
                return self._json({"error": "question too long"}, 400)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self._security_headers()
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(ev: dict):
                try:
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise _ClientGone()

            try:
                chat.ask(question, emit)
            except _ClientGone:
                chat.discard_unfinished()
                store.audit("question_stopped", user)
            except Exception as e:  # last resort: a plain message, never a stack trace
                store.audit("chat_error", user, error=f"{type(e).__name__}: {e}"[:300])
                try:
                    emit({"type": "error", "text": "Something went wrong on the server. The admin can check the audit log."})
                except _ClientGone:
                    pass
            return

        # ---- admin ----
        if u.path.startswith("/api/admin/"):
            if sess["role"] != "admin":
                store.audit("admin_denied", user, path=u.path)
                return self._json({"error": "admin only"}, 403)
            try:
                if u.path == "/api/admin/users":
                    action = body.get("action")
                    uname = str(body.get("username", ""))
                    if action == "create":
                        auth.create_user(uname, str(body.get("password", "")), str(body.get("role", "user")), by=user,
                                         erp_user=str(body.get("erp_user", "") or "") or None)
                        store.audit("user_created", user, target=auth.normalize_username(uname), role=body.get("role"), erp_user=body.get("erp_user"))
                    elif action == "link":
                        auth.set_erp_link(uname, str(body.get("erp_user", "") or ""), by=user)
                        store.audit("user_erp_linked", user, target=auth.normalize_username(uname), erp_user=body.get("erp_user"))
                    elif action == "delete":
                        auth.delete_user(uname, by=user)
                        store.audit("user_deleted", user, target=auth.normalize_username(uname))
                    elif action == "password":
                        auth.set_password(uname, str(body.get("password", "")), by=user)
                        store.audit("user_password_reset", user, target=auth.normalize_username(uname))
                    elif action == "role":
                        auth.set_role(uname, str(body.get("role", "")), by=user)
                        store.audit("user_role_changed", user, target=auth.normalize_username(uname), role=body.get("role"))
                    else:
                        return self._json({"error": "unknown action"}, 400)
                    return self._json({"ok": True, "users": auth.list_users()})
                if u.path == "/api/admin/groups":
                    mapping = body.get("mapping")
                    if not isinstance(mapping, dict):
                        return self._json({"error": "mapping must be an object"}, 400)
                    return self._json({"ok": True, "overrides": store.save_group_overrides(mapping, by=user)})
                if u.path == "/api/admin/settings":
                    return self._json({"ok": True, "settings": store.update_settings(body, by=user)})
                if u.path == "/api/admin/knowledge":
                    if body.get("action") == "delete":
                        return self._json({"ok": store.delete_note(str(body.get("id", "")), by=user)})
                    if body.get("action") == "add":
                        n = store.add_note(str(body.get("kind", "rule")), str(body.get("text", "")), by=user)
                        return self._json({"ok": True, "note": n})
                    return self._json({"error": "unknown action"}, 400)
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e) or "bad request"}, 400)
        return self._json({"error": "not found"}, 404)


def main() -> None:
    port = CFG.port
    if not CFG.anthropic_api_key:
        print("ANTHROPIC_API_KEY missing in .env - the page will load but questions will fail.")
    if auth.user_count() == 0:
        print("No users yet. Create the first admin with:  python manage.py create <username> admin")
    try:
        print("DB:", db.connection_summary(), "->", end=" ")
        db.run_select("select 1 from dual", max_rows=1)
        print("connected")
    except Exception as e:
        print("NOT connected:", str(e).splitlines()[0])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    s = store.settings()
    print(f"Stock assistant  model={s['model']} effort={s['effort']}  ->  {url}   (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
