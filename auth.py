"""Users, passwords, sessions and login lockout.

Two kinds of sign-in:
- App accounts in data/users.json (salted PBKDF2-SHA256), optionally linked to an ERP
  username so their modules come from the ERP groups.
- ERP credentials directly (when the admin has enabled it): the ERP's own username and
  password, verified against the ERP's stored hash by erp_roles; access from ERP groups.
Sessions live in memory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time

import store
from config import ROOT

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
USERS_FILE = DATA / "users.json"

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SEC = 12 * 3600      # absolute lifetime
SESSION_IDLE_SEC = 90 * 60       # idle timeout
LOCK_AFTER_FAILS = 5
LOCK_SECONDS = 15 * 60
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
ROLES = ("admin", "user")

_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_fails: dict[str, dict] = {}
_failed_today: list[float] = []


# ----------------------------------------------------------------- users ----
def _load() -> dict:
    if not USERS_FILE.exists():
        return {"users": {}}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {"users": {}}


def _save(d: dict) -> None:
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


def normalize_username(u: str) -> str:
    return (u or "").strip().lower()


def validate_username(u: str) -> str | None:
    if not USERNAME_RE.match(u):
        return "Username: 3-32 characters, lowercase letters, digits, . _ - only."
    return None


def validate_password(pw: str) -> str | None:
    if len(pw or "") < 8:
        return "Password must be at least 8 characters."
    if pw.strip() != pw:
        return "Password must not start or end with a space."
    return None


def hash_password(pw: str, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": dk.hex(), "iterations": iterations, "algo": "pbkdf2_sha256"}


def _verify(pw: str, rec: dict) -> bool:
    try:
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(rec["salt"]), int(rec["iterations"]))
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except (KeyError, ValueError, TypeError):
        return False


_DUMMY = hash_password("not-a-real-password")  # keeps timing equal for unknown users


def list_users() -> list[dict]:
    d = _load()
    return [{"username": name, "role": rec.get("role", "user"), "erp_user": rec.get("erp_user"),
             "created": rec.get("created"), "created_by": rec.get("created_by"), "last_login": rec.get("last_login")}
            for name, rec in sorted(d["users"].items())]


def user_count() -> int:
    return len(_load()["users"])


def create_user(username: str, password: str, role: str, by: str, erp_user: str | None = None) -> None:
    username = normalize_username(username)
    err = validate_username(username) or validate_password(password)
    if err:
        raise ValueError(err)
    if role not in ROLES:
        raise ValueError("Role must be admin or user.")
    erp_user = (erp_user or "").strip() or None
    if erp_user and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,40}", erp_user):
        raise ValueError("ERP username: letters, digits, . _ @ - only.")
    with _lock:
        d = _load()
        if username in d["users"]:
            raise ValueError("That username already exists.")
        rec = hash_password(password)
        rec.update({"role": role, "erp_user": erp_user, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "created_by": by, "last_login": None})
        d["users"][username] = rec
        _save(d)


def delete_user(username: str, by: str) -> None:
    username = normalize_username(username)
    with _lock:
        d = _load()
        if username not in d["users"]:
            raise ValueError("No such user.")
        if username == by:
            raise ValueError("You cannot delete your own account.")
        admins = [u for u, r in d["users"].items() if r.get("role") == "admin"]
        if d["users"][username].get("role") == "admin" and len(admins) <= 1:
            raise ValueError("Cannot delete the last admin.")
        del d["users"][username]
        _save(d)
    revoke_user_sessions(username)


def set_password(username: str, new_password: str, by: str) -> None:
    username = normalize_username(username)
    err = validate_password(new_password)
    if err:
        raise ValueError(err)
    with _lock:
        d = _load()
        if username not in d["users"]:
            raise ValueError("No such user.")
        rec = d["users"][username]
        rec.update(hash_password(new_password))
        rec["password_changed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["password_changed_by"] = by
        _save(d)
    revoke_user_sessions(username)


def set_role(username: str, role: str, by: str) -> None:
    username = normalize_username(username)
    if role not in ROLES:
        raise ValueError("Role must be admin or user.")
    with _lock:
        d = _load()
        if username not in d["users"]:
            raise ValueError("No such user.")
        if username == by and role != "admin":
            raise ValueError("You cannot remove your own admin role.")
        d["users"][username]["role"] = role
        _save(d)
    revoke_user_sessions(username)


def set_erp_link(username: str, erp_user: str | None, by: str) -> None:
    username = normalize_username(username)
    erp_user = (erp_user or "").strip() or None
    if erp_user and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,40}", erp_user):
        raise ValueError("ERP username: letters, digits, . _ @ - only.")
    with _lock:
        d = _load()
        if username not in d["users"]:
            raise ValueError("No such user.")
        d["users"][username]["erp_user"] = erp_user
        _save(d)
    revoke_user_sessions(username)


def check_password(username: str, password: str) -> bool:
    rec = _load()["users"].get(normalize_username(username))
    return _verify(password, rec) if rec else False


# --------------------------------------------------------------- lockout ----
def _fail_key(username: str, ip: str) -> str:
    return f"{username}|{ip}"


def locked_for(username: str, ip: str) -> int:
    now = time.time()
    for key in (_fail_key(username, ip), _fail_key("*", ip)):
        rec = _fails.get(key)
        if rec and rec.get("locked_until", 0) > now:
            return int(rec["locked_until"] - now)
    return 0


def _record_fail(username: str, ip: str) -> None:
    now = time.time()
    _failed_today.append(now)
    for key in (_fail_key(username, ip), _fail_key("*", ip)):
        rec = _fails.setdefault(key, {"count": 0, "locked_until": 0, "first": now})
        if now - rec["first"] > LOCK_SECONDS:
            rec.update({"count": 0, "first": now})
        rec["count"] += 1
        limit = LOCK_AFTER_FAILS if key.startswith(username) else LOCK_AFTER_FAILS * 4
        if rec["count"] >= limit:
            rec["locked_until"] = now + LOCK_SECONDS


def _clear_fails(username: str, ip: str) -> None:
    _fails.pop(_fail_key(username, ip), None)


def failed_logins_today() -> int:
    cutoff = time.time() - 24 * 3600
    return sum(1 for t in _failed_today if t > cutoff)


def locked_accounts() -> int:
    now = time.time()
    return sum(1 for k, r in _fails.items() if r.get("locked_until", 0) > now and not k.startswith("*|"))


# ---------------------------------------------------------------- access ----
def _default_access() -> dict:
    """An unlinked app user: the default module set from the knowledge file."""
    import erp_roles

    keys = list(getattr(erp_roles._kn, "DEFAULT_MODULES", [])) or (["stock"] if "stock" in erp_roles.MODULES else [])
    return {"erp_user": None, "groups": [], "modules": keys, "tables": erp_roles.tables_for(keys), "source": "default"}


def _access_for(app_role: str, erp_user: str | None) -> tuple[str, dict]:
    """Effective role and access for an app account, honouring an ERP link."""
    import erp_roles

    if app_role == "admin":
        return "admin", {"erp_user": erp_user, "groups": [], "modules": list(erp_roles.MODULES), "tables": [], "source": "app-admin"}
    if erp_user:
        r = erp_roles.resolve(erp_user)
        if r["exists"]:
            if r["is_admin"]:
                return "admin", {"erp_user": r["erp_user"], "groups": r["groups"], "modules": list(erp_roles.MODULES), "tables": [], "source": "erp-admin"}
            return "user", {"erp_user": r["erp_user"], "groups": r["groups"], "modules": r["modules"], "tables": r["tables"], "source": "erp"}
    return "user", _default_access()


# -------------------------------------------------------------- sessions ----
def _new_session(user: str, role: str, access: dict, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _sessions[token] = {"user": user, "role": role, "access": access, "created": now, "last": now, "ip": ip}
    return token


def authenticate(username: str, password: str, ip: str) -> tuple[str | None, str]:
    """Return (session_token, role) on success, (None, reason) on failure."""
    username = normalize_username(username)
    wait = locked_for(username, ip)
    if wait:
        return None, f"Too many failed attempts. Try again in {wait // 60 + 1} minutes."

    d = _load()
    rec = d["users"].get(username)
    if rec:
        if _verify(password, rec):
            try:
                role, access = _access_for(rec.get("role", "user"), rec.get("erp_user"))
            except Exception as e:  # ERP unreachable: fall back to the account's own role, no modules
                store.audit("erp_lookup_failed", username, error=str(e)[:200])
                role, access = rec.get("role", "user"), _default_access() if rec.get("role") != "admin" else {"erp_user": rec.get("erp_user"), "groups": [], "modules": [], "tables": [], "source": "app-admin"}
            with _lock:
                _clear_fails(username, ip)
                rec["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save(d)
            return _new_session(username, role, access, ip), role
        with _lock:
            _record_fail(username, ip)
        time.sleep(0.4)
        return None, "Incorrect username or password."

    # not an app account: try ERP credentials when enabled
    if store.settings().get("erp_login", True):
        import erp_roles

        try:
            if erp_roles.erp_password_matches(username, password):
                r = erp_roles.resolve(username)
                if r["exists"] and r["active"]:
                    role = "admin" if r["is_admin"] else "user"
                    access = {"erp_user": r["erp_user"], "groups": r["groups"],
                              "modules": list(erp_roles.MODULES) if role == "admin" else r["modules"],
                              "tables": [] if role == "admin" else r["tables"], "source": "erp-login"}
                    with _lock:
                        _clear_fails(username, ip)
                    return _new_session(r["erp_user"].lower(), role, access, ip), role
        except Exception as e:
            store.audit("erp_login_error", username, error=str(e)[:200])
    else:
        _verify(password, _DUMMY)  # equalise timing
    with _lock:
        _record_fail(username, ip)
    time.sleep(0.4)
    return None, "Incorrect username or password."


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock:
        s = _sessions.get(token)
        if not s:
            return None
        now = time.time()
        if now - s["created"] > SESSION_TTL_SEC or now - s["last"] > SESSION_IDLE_SEC:
            _sessions.pop(token, None)
            return None
        s["last"] = now
        return dict(s)


def revoke(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def revoke_user_sessions(username: str) -> None:
    with _lock:
        for t in [t for t, s in _sessions.items() if s["user"] == username]:
            _sessions.pop(t, None)


def active_sessions() -> int:
    now = time.time()
    with _lock:
        return sum(1 for s in _sessions.values() if now - s["created"] <= SESSION_TTL_SEC and now - s["last"] <= SESSION_IDLE_SEC)
