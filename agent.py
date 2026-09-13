"""Agent loop for the stock assistant.

One `Chat` per signed-in browser session. A manual tool loop over a provider
(`providers.AnthropicProvider` or `providers.OllamaProvider`), because we own the
history across turns, stream text to the page, trim old tool results, enforce the
session's table allow-list on every tool call, log usage per user and roll a
half-finished exchange back when the user presses Stop.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from typing import Callable

import anthropic
import oracledb

import db
import erp_roles
import policy
import providers
import store
from config import CFG, LOGS

try:
    from knowledge import SYSTEM_PROMPT  # your private schema notes (git-ignored)
except ImportError:  # fresh clone: start from the template and edit it
    from knowledge_example import SYSTEM_PROMPT

# ---------------------------------------------------------------- pricing --
# USD per 1M tokens: (input, output). Cache write (1h) = 2x input, cache read = 0.1x input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE_MULT = 2.0
CACHE_READ_MULT = 0.10


def price_for(model: str) -> tuple[float, float]:
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return (5.0, 25.0)


def cost_of(model: str, usage: dict) -> float:
    inp, out = price_for(model)
    cost = usage.get("input", 0) * inp + usage.get("cache_write", 0) * inp * CACHE_WRITE_MULT
    cost += usage.get("cache_read", 0) * inp * CACHE_READ_MULT + usage.get("output", 0) * out
    return cost / 1_000_000


# ------------------------------------------------------------- usage log ---
_USAGE_FILE = LOGS / "usage.jsonl"
_usage_lock = threading.Lock()


def record_usage(entry: dict) -> None:
    with _usage_lock, _USAGE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _usage_rows() -> list[dict]:
    if not _USAGE_FILE.exists():
        return []
    out = []
    with _USAGE_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def spent_since(days: int, user: str | None = None) -> float:
    cutoff = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    return round(sum(float(e.get("cost", 0)) for e in _usage_rows()
                     if e.get("date", "") >= cutoff and (user is None or e.get("user") == user)), 6)


def spent_today(user: str | None = None) -> float:
    return spent_since(1, user)


def usage_summary() -> dict:
    rows = _usage_rows()
    today = _dt.date.today().isoformat()
    by_user: dict[str, float] = {}
    for e in rows:
        if e.get("date") == today:
            k = e.get("user") or "?"
            by_user[k] = round(by_user.get(k, 0) + float(e.get("cost", 0)), 4)
    return {
        "today": spent_since(1), "last7": spent_since(7), "last30": spent_since(30),
        "questions_today": sum(1 for e in rows if e.get("date") == today),
        "questions_total": len(rows), "by_user_today": by_user,
        "local_questions": sum(1 for e in rows if e.get("provider") == "ollama"),
    }


def cost_estimates() -> dict:
    """Per-model cost of a typical question, from the measured token profile of recent Claude questions."""
    rows = [e for e in _usage_rows() if e.get("provider", "anthropic") == "anthropic" and (e.get("input") or e.get("cache_read"))][-60:]

    def med(key, default):
        vals = sorted(int(e.get(key) or 0) for e in rows)
        return vals[len(vals) // 2] if vals else default

    cache_read = med("cache_read", 16500)
    uncached = med("input", 3000)
    output = med("output", 600)
    cache_write = 8500
    out = []
    for mid, label, note in store.MODELS:
        inp, outp = price_for(mid)
        per_q = (cache_read * inp * CACHE_READ_MULT + uncached * inp + output * outp) / 1e6
        warm = cache_write * inp * CACHE_WRITE_MULT / 1e6
        out.append({
            "model": mid, "label": label, "note": note,
            "per_question": round(per_q, 4), "cache_warmup": round(warm, 4),
            "monthly_50_per_day": round(per_q * 50 * 22 + warm * 4 * 22, 2),
            "monthly_10_per_day": round(per_q * 10 * 22 + warm * 2 * 22, 2),
        })
    return {"profile": {"cache_read": cache_read, "uncached_input": uncached, "output": output, "sample": len(rows)}, "models": out}


# ----------------------------------------------------------------- tools ---
TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run one read-only Oracle SELECT against the ERP schema and return the rows as a text table "
            "(you see at most the first rows; the user sees the full capped result). Use it for every factual answer. "
            "No semicolon, one statement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The SELECT statement."},
                "purpose": {"type": "string", "description": "3-10 words saying what this query finds, shown to the user."},
            },
            "required": ["sql", "purpose"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "describe_table",
        "description": "List the columns and types of one table or view (admin sessions only).",
        "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "search_schema",
        "description": "Find tables and columns whose NAME contains a keyword (e.g. GRN, BATCH, VENDOR). Admin sessions only.",
        "input_schema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "sample_rows",
        "description": "Show a few raw rows of a table to see how its columns are populated (admin sessions only).",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}, "n": {"type": "integer", "description": "How many rows, 1-20."}},
            "required": ["table", "n"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "save_knowledge",
        "description": (
            "Admin sessions only. Save a confirmed fact for all future sessions: a table's meaning, a join key, "
            "a business rule, a correction from the admin, or a verified question->SQL example. "
            "Write it as a short reusable rule. Never save guesses."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["rule", "table", "join", "example", "correction"]},
                "text": {"type": "string", "description": "The fact, 1-6 lines. For 'example' include the question and the SQL."},
            },
            "required": ["kind", "text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

EventFn = Callable[[dict], None]


def current_provider(s: dict | None = None):
    """The provider selected in settings (Claude API or a local Ollama model)."""
    s = s or store.settings()
    if s.get("provider") == "ollama":
        return providers.OllamaProvider(s.get("ollama_url", "http://127.0.0.1:11434"), s.get("ollama_model", "qwen3:8b"))
    return providers.AnthropicProvider(CFG.anthropic_api_key, s["model"], s["effort"])


def model_label(s: dict | None = None) -> str:
    s = s or store.settings()
    if s.get("provider") == "ollama":
        return "local · " + s.get("ollama_model", "?")
    return s["model"].replace("claude-", "") + " · " + s["effort"]


class Chat:
    """One conversation for one signed-in user. Thread-safe for one question at a time."""

    def __init__(self, user: str = "local", role: str = "admin", access: dict | None = None) -> None:
        if role not in policy.ROLES:
            raise ValueError("bad role")
        self.user, self.role = user, role
        self.access = access or {}
        self.modules_info = erp_roles.module_info(self.access.get("modules", []))
        self.allowed_tables = None if role == "admin" else (self.access.get("tables") if self.access.get("modules") is not None else None)
        self.messages: list = []
        self.results: list[db.QueryResult] = []
        self.session_cost = 0.0
        self.turns = 0
        self.turn_log: list[dict] = []
        self._lock = threading.Lock()
        self.started = _dt.datetime.now()
        self._transcript = LOGS / f"chat-{self.started:%Y%m%d-%H%M%S}-{user}.md"

    # -- request shaping -------------------------------------------------
    def _system_blocks(self, provider_name: str) -> list[dict]:
        learned = {"type": "text", "text": store.learned_prompt()}
        if provider_name == "anthropic":
            learned["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return [{"type": "text", "text": SYSTEM_PROMPT}, learned, {"type": "text", "text": policy.role_prompt(self.role, self.modules_info)}]

    def _compact_history(self) -> None:
        last_q = None
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if msg["role"] == "user" and isinstance(msg["content"], str):
                last_q = i
                break
        if last_q is None:
            return
        for msg in self.messages[:last_q]:
            if msg["role"] != "user" or not isinstance(msg["content"], list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str) and len(c) > 600:
                        block["content"] = c[:500] + "\n... (older result trimmed)"
        MAX_MSGS = 40
        if len(self.messages) > MAX_MSGS:
            cut = len(self.messages) - MAX_MSGS
            while cut < len(self.messages) and not (
                self.messages[cut]["role"] == "user" and isinstance(self.messages[cut]["content"], str)
            ):
                cut += 1
            self.messages = self.messages[cut:]

    # -- tools -------------------------------------------------------------
    def _run_tool(self, name: str, inp: dict, emit: EventFn, question: str) -> tuple[str, bool]:
        role = self.role
        s = store.settings()
        try:
            if name == "run_sql":
                sql = str(inp.get("sql", ""))
                emit({"type": "tool", "name": name, "purpose": str(inp.get("purpose", "")), "sql": sql})
                try:
                    res = db.run_select(sql, role=role, max_rows=s["max_rows"], allowed_tables=self.allowed_tables)
                except ValueError as e:
                    return f"Rejected: {e}", True
                except PermissionError as e:
                    store.audit("sql_denied", self.user, role=role, reason=str(e)[:200], sql=sql[:300])
                    emit({"type": "sql_error", "text": "outside this user's privileges"})
                    return f"Denied by policy: {e} Do not retry with another table; tell the user exactly: {policy.REFUSAL}", True
                except oracledb.Error as e:
                    msg = str(e).splitlines()[0]
                    emit({"type": "sql_error", "text": msg})
                    return f"Oracle error: {msg}\nFix the SQL and try again.", True
                self.results.append(res)
                store.audit("sql_run", self.user, role=role, rows=res.rowcount, elapsed=round(res.elapsed, 2), sql=res.sql[:300])
                emit({"type": "result", "index": len(self.results) - 1, **res.to_dict()})
                return res.as_text(max_rows=s["model_rows"]), False
            if name in ("describe_table", "search_schema", "sample_rows") and role != "admin":
                return policy.REFUSAL, True
            if name == "describe_table":
                emit({"type": "tool", "name": name, "purpose": f"describe {inp.get('table', '')}"})
                return db.describe_table(str(inp.get("table", "")), role=role), False
            if name == "search_schema":
                emit({"type": "tool", "name": name, "purpose": f"search schema for {inp.get('keyword', '')}"})
                return db.search_schema(str(inp.get("keyword", "")), role=role), False
            if name == "sample_rows":
                emit({"type": "tool", "name": name, "purpose": f"sample {inp.get('table', '')}"})
                try:
                    n = int(inp.get("n", 5))
                except (TypeError, ValueError):
                    n = 5
                return db.sample_rows(str(inp.get("table", "")), n, role=role), False
            if name == "save_knowledge":
                if role != "admin":
                    return "Not permitted for this user.", True
                note = store.add_note(str(inp.get("kind", "rule")), str(inp.get("text", "")), by=self.user, question=question)
                emit({"type": "learned", "kind": note["kind"], "text": note["text"], "id": note["id"]})
                return f"Saved as {note['kind']} note {note['id']}. It will be available to all future sessions.", False
            return f"Unknown tool {name}", True
        except RuntimeError as e:
            return f"Database connection problem: {e}", True
        except ValueError as e:
            return f"Rejected: {e}", True
        except oracledb.Error as e:
            return f"Oracle error: {str(e).splitlines()[0]}", True

    # -- main entry --------------------------------------------------------
    def ask(self, question: str, emit: EventFn) -> str:
        with self._lock:
            return self._ask(question, emit)

    def _ask(self, question: str, emit: EventFn) -> str:
        s = store.settings()
        local = s.get("provider") == "ollama"
        if not local:
            spent = spent_today()
            if spent >= s["daily_budget_usd"]:
                msg = f"Today's total budget of ${s['daily_budget_usd']:.2f} is used up (spent ${spent:.2f}). An admin can raise it in the admin panel."
                emit({"type": "error", "text": msg})
                return msg
            mine = spent_today(self.user)
            if mine >= s["user_daily_budget_usd"]:
                msg = f"Your daily budget of ${s['user_daily_budget_usd']:.2f} is used up (spent ${mine:.2f}). Ask an admin to raise it."
                emit({"type": "error", "text": msg})
                return msg

        turn = {"q": question.strip(), "ts": _dt.datetime.now().isoformat(timespec="seconds"), "events": []}
        self.turn_log.append(turn)
        raw_emit = emit

        def emit(ev: dict) -> None:
            if ev.get("type") in ("tool", "result", "sql_error", "error", "final", "learned"):
                turn["events"].append(ev)
            raw_emit(ev)

        store.audit("question", self.user, role=self.role, modules=self.access.get("modules"), provider=s.get("provider", "anthropic"), text=question.strip()[:300])
        today = _dt.date.today().strftime("%d/%m/%Y")
        self.messages.append({"role": "user", "content": f"{question.strip()}\n\n[Today is {today}]"})
        self._compact_history()

        try:
            provider = current_provider(s)
        except RuntimeError as e:
            return self._fail(emit, str(e))
        model_name = getattr(provider, "model", "?")
        q_cost = 0.0
        usage_tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        final_text = ""
        t0 = time.time()
        rounds = 0
        try:
            while True:
                rounds += 1
                if rounds > CFG.max_tool_rounds + 1:
                    final_text = "I ran too many queries without reaching an answer. Please narrow the question."
                    emit({"type": "text_delta", "text": final_text})
                    self.messages.append({"role": "assistant", "content": final_text})
                    break

                emit({"type": "status", "text": "thinking" if rounds == 1 else "working on results"})
                text_this_round: list[str] = []

                def on_text(t: str) -> None:
                    text_this_round.append(t)
                    emit({"type": "text_delta", "text": t})

                reply = provider.stream(self._system_blocks(provider.name), TOOLS, self.messages, on_text)
                if provider.name == "anthropic":
                    q_cost += cost_of(reply.model or model_name, reply.usage)
                for k in usage_tot:
                    usage_tot[k] += reply.usage.get(k, 0)

                self.messages.append({"role": "assistant", "content": reply.content})

                tool_blocks = [b for b in reply.content if b.get("type") == "tool_use"]
                if tool_blocks:
                    tool_results = []
                    for block in tool_blocks:
                        inp = block.get("input")
                        if not isinstance(inp, dict):
                            try:
                                inp = json.loads(inp) if isinstance(inp, str) else {}
                            except ValueError:
                                inp = {}
                        text, is_err = self._run_tool(block.get("name", ""), inp, emit, question)
                        tr = {"type": "tool_result", "tool_use_id": block.get("id", ""), "content": text}
                        if is_err:
                            tr["is_error"] = True
                        tool_results.append(tr)
                    self.messages.append({"role": "user", "content": tool_results})
                    if text_this_round:
                        emit({"type": "text_break"})
                    continue

                if reply.stop_reason == "pause_turn":
                    continue

                final_text = "".join(text_this_round) or "".join(b.get("text", "") for b in reply.content if b.get("type") == "text")
                if reply.stop_reason == "refusal":
                    why = f" ({reply.stop_details})" if reply.stop_details else ""
                    final_text = (final_text + f"\n\nThe model declined to answer this{why}.").strip()
                    emit({"type": "text_delta", "text": f"\n\nThe model declined to answer this{why}."})
                elif reply.stop_reason == "max_tokens":
                    emit({"type": "text_delta", "text": "\n\n(answer cut off - ask me to continue)"})
                if not final_text.strip():
                    final_text = "The model returned an empty answer. Please ask again, perhaps with more detail."
                    emit({"type": "text_delta", "text": final_text})
                break
        except anthropic.AuthenticationError:
            return self._fail(emit, "The AI service key is invalid. An admin must update ANTHROPIC_API_KEY in .env.")
        except anthropic.RateLimitError:
            return self._fail(emit, "The AI service is rate-limited right now. Wait a minute and try again.")
        except anthropic.APIStatusError as e:
            return self._fail(emit, f"AI service error {e.status_code}. Try again in a moment.")
        except anthropic.APIConnectionError:
            return self._fail(emit, "Cannot reach the AI service. Check the internet connection.")
        except RuntimeError as e:  # local model problems (Ollama down, model missing)
            return self._fail(emit, str(e)[:300])
        finally:
            self.session_cost += q_cost
            self.turns += 1
            if any(usage_tot.values()):
                record_usage({
                    "date": _dt.date.today().isoformat(),
                    "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                    "user": self.user, "role": self.role, "model": model_name, "provider": provider.name,
                    "cost": round(q_cost, 6), "rounds": rounds, "elapsed": round(time.time() - t0, 1), **usage_tot,
                    "question": question.strip()[:200],
                })
            self._log_transcript(question, final_text, q_cost)

        emit({
            "type": "final", "text": final_text, "cost": round(q_cost, 4),
            "session_cost": round(self.session_cost, 4), "spent_today": round(spent_today(), 4),
            "budget": s["daily_budget_usd"], "usage": usage_tot, "elapsed": round(time.time() - t0, 1),
            "model": model_name, "provider": provider.name, "turn": len(self.turn_log) - 1,
        })
        return final_text

    def _fail(self, emit: EventFn, msg: str) -> str:
        emit({"type": "error", "text": msg})
        self._rollback_to_question()
        return msg

    def discard_unfinished(self) -> None:
        with self._lock:
            if self.turn_log and not any(e.get("type") == "final" for e in self.turn_log[-1]["events"]):
                self.turn_log.pop()
                self._rollback_to_question()

    def history(self) -> dict:
        return {"turns": self.turn_log}

    def _rollback_to_question(self) -> None:
        while self.messages and not (
            self.messages[-1]["role"] == "user" and isinstance(self.messages[-1]["content"], str)
        ):
            self.messages.pop()
        if self.messages:
            self.messages.pop()

    def _log_transcript(self, q: str, a: str, cost: float) -> None:
        try:
            with self._transcript.open("a", encoding="utf-8") as f:
                f.write(f"\n## {_dt.datetime.now():%H:%M:%S}  {self.user} (${cost:.4f})\n**Q:** {q.strip()}\n\n{a.strip()}\n")
        except OSError:
            pass

    # -- feedback / learning -----------------------------------------------
    def feedback(self, turn_index: int, vote: str, text: str) -> dict:
        if vote not in ("up", "down"):
            raise ValueError("vote must be up or down")
        if not 0 <= turn_index < len(self.turn_log):
            raise ValueError("unknown turn")
        turn = self.turn_log[turn_index]
        final = next((e for e in turn["events"] if e.get("type") == "final"), None)
        good_sql = None
        for e in turn["events"]:
            if e.get("type") == "tool" and e.get("sql"):
                good_sql = e["sql"]
        store.add_feedback(self.user, self.role, vote, text, turn["q"], final["text"] if final else "", good_sql)
        learned = None
        if self.role == "admin":
            if vote == "up" and good_sql:
                learned = store.add_note("example", f"Verified answer. Question: {turn['q']}\nSQL:\n{good_sql.strip()}", by=self.user, question=turn["q"])
            elif vote == "down" and (text or "").strip():
                learned = store.add_note("correction", f"Question: {turn['q']}\nAdmin correction: {text.strip()}", by=self.user, question=turn["q"])
        return {"ok": True, "learned": learned}

    def result_csv(self, index: int) -> tuple[str, str]:
        import csv
        import io

        res = self.results[index]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(res.columns)
        w.writerows(res.rows)
        return f"result-{index + 1}.csv", buf.getvalue()

    def status(self) -> dict:
        s = store.settings()
        return {
            "user": self.user, "role": self.role, "model": s["model"], "effort": s["effort"],
            "provider": s.get("provider", "anthropic"), "model_label": model_label(s),
            "db": db.connection_summary(), "spent_today": round(spent_today(), 4),
            "my_spent_today": round(spent_today(self.user), 4),
            "budget": s["daily_budget_usd"], "my_budget": s["user_daily_budget_usd"],
            "session_cost": round(self.session_cost, 4), "turns": self.turns, "max_rows": s["max_rows"],
            "show_sql": self.role == "admin" or bool(s["show_sql_to_users"]),
            "erp_user": self.access.get("erp_user"),
            "modules": [m["label"] for m in self.modules_info if m.get("enabled")] if self.role != "admin" else ["all modules"],
            "groups": self.access.get("groups", []),
        }
