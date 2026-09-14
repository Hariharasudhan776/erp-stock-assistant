"""Small file-backed stores: runtime settings, learned knowledge, group-module overrides,
feedback and the audit log."""
from __future__ import annotations

import datetime as _dt
import json
import secrets
import threading
import time
from pathlib import Path

from config import CFG, LOGS, ROOT

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
KNOWLEDGE_FILE = DATA / "knowledge_learned.json"
GROUPS_FILE = DATA / "group_modules.json"
FEEDBACK_FILE = LOGS / "feedback.jsonl"
AUDIT_FILE = LOGS / "audit.jsonl"
_lock = threading.Lock()

MODELS = [  # id, label, one-line positioning
    ("claude-opus-5", "Claude Opus 5", "Most accurate SQL; best for demos and hard questions"),
    ("claude-sonnet-5", "Claude Sonnet 5", "Balanced and fast: about 2.5x cheaper than Opus, strong on routine questions"),
    ("claude-haiku-4-5", "Claude Haiku 4.5", "Cheapest and fastest: about 5x cheaper than Opus, weaker on tricky joins"),
]
EFFORTS = ("low", "medium", "high")

_DEFAULTS = {
    "model": CFG.model,
    "effort": CFG.effort,
    "daily_budget_usd": CFG.daily_budget_usd,
    "user_daily_budget_usd": 1.0,
    "show_sql_to_users": False,
    "max_rows": CFG.max_rows,     # rows kept for the grid / CSV
    "model_rows": 50,             # rows the model is allowed to read from a result (cost + speed)
    "erp_login": True,            # allow sign-in with ERP credentials
    "provider": "anthropic",      # "anthropic" (Claude API) or "ollama" (local model, nothing leaves the PC)
    "ollama_model": "qwen3:8b",
    "ollama_url": "http://127.0.0.1:11434",
}


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# -------------------------------------------------------------- settings ----
def settings() -> dict:
    s = dict(_DEFAULTS)
    s.update({k: v for k, v in _read_json(SETTINGS_FILE, {}).items() if k in _DEFAULTS})
    return s


def update_settings(patch: dict, by: str) -> dict:
    cur = settings()
    if "model" in patch:
        if patch["model"] not in {m[0] for m in MODELS}:
            raise ValueError("Unknown model.")
        cur["model"] = patch["model"]
    if "effort" in patch:
        if patch["effort"] not in EFFORTS:
            raise ValueError("Effort must be low, medium or high.")
        cur["effort"] = patch["effort"]
    for key in ("daily_budget_usd", "user_daily_budget_usd"):
        if key in patch:
            v = float(patch[key])
            if not 0 < v <= 500:
                raise ValueError(f"{key} must be between 0 and 500.")
            cur[key] = round(v, 2)
    if "max_rows" in patch:
        v = int(patch["max_rows"])
        if not 10 <= v <= 1000:
            raise ValueError("max_rows must be between 10 and 1000.")
        cur["max_rows"] = v
    if "model_rows" in patch:
        v = int(patch["model_rows"])
        if not 10 <= v <= 500:
            raise ValueError("model_rows must be between 10 and 500.")
        cur["model_rows"] = v
    for key in ("show_sql_to_users", "erp_login"):
        if key in patch:
            cur[key] = bool(patch[key])
    if "provider" in patch:
        if patch["provider"] not in ("anthropic", "ollama"):
            raise ValueError("provider must be anthropic or ollama.")
        cur["provider"] = patch["provider"]
    if "ollama_model" in patch:
        import re as _re
        v = str(patch["ollama_model"]).strip()
        if not _re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", v):
            raise ValueError("Local model name: letters, digits, . : / _ - only.")
        cur["ollama_model"] = v
    if "ollama_url" in patch:
        import re as _re
        v = str(patch["ollama_url"]).strip().rstrip("/")
        if not _re.fullmatch(r"https?://[A-Za-z0-9_.:-]+(?::\d+)?", v):
            raise ValueError("Ollama URL must look like http://127.0.0.1:11434")
        cur["ollama_url"] = v
    with _lock:
        _write_json(SETTINGS_FILE, cur)
    audit("settings_changed", by, changes=patch)
    return cur


# ------------------------------------------------------- group -> modules ----
def group_overrides() -> dict[str, list[str]]:
    d = _read_json(GROUPS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_group_overrides(mapping: dict[str, list[str]], by: str) -> dict[str, list[str]]:
    clean = {}
    for g, mods in (mapping or {}).items():
        g = str(g).strip()[:60]
        if not g:
            continue
        clean[g] = [str(m)[:40] for m in (mods or []) if str(m).strip()]
    with _lock:
        _write_json(GROUPS_FILE, clean)
    audit("group_mapping_changed", by, groups=len(clean))
    return clean


# ------------------------------------------------------------- knowledge ----
KINDS = ("rule", "table", "join", "example", "correction")


def notes() -> list[dict]:
    return _read_json(KNOWLEDGE_FILE, [])


def add_note(kind: str, text: str, by: str, question: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    text = (text or "").strip()
    if len(text) < 8:
        raise ValueError("Note too short.")
    if len(text) > 6000:
        raise ValueError("Note too long (6000 chars max).")
    note = {"id": secrets.token_hex(6), "kind": kind, "text": text, "by": by,
            "ts": time.strftime("%Y-%m-%d %H:%M"), "question": (question or "").strip()[:300] or None}
    with _lock:
        cur = _read_json(KNOWLEDGE_FILE, [])
        for n in cur:
            if n.get("kind") == kind and n.get("text", "").strip() == text:
                return n  # already saved: a looping model must not fill the knowledge base with copies
        cur.append(note)
        _write_json(KNOWLEDGE_FILE, cur)
    audit("knowledge_added", by, kind=kind, id=note["id"], chars=len(text))
    return note


def delete_note(note_id: str, by: str) -> bool:
    with _lock:
        cur = _read_json(KNOWLEDGE_FILE, [])
        new = [n for n in cur if n["id"] != note_id]
        if len(new) == len(cur):
            return False
        _write_json(KNOWLEDGE_FILE, new)
    audit("knowledge_deleted", by, id=note_id)
    return True


def learned_prompt() -> str:
    ns = notes()
    if not ns:
        return "# Learned knowledge\n(nothing saved yet)"
    out = ["# Learned knowledge (confirmed by admins in earlier sessions - trust these over guesses)"]
    for n in ns:
        head = f"- [{n['kind']}] "
        if n.get("question"):
            head += f"Q: {n['question']} -> "
        out.append(head + n["text"].replace("\n", "\n  "))
    return "\n".join(out)


# -------------------------------------------------------------- feedback ----
def add_feedback(user: str, role: str, vote: str, text: str, question: str, answer: str, sql: str | None) -> None:
    rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "user": user, "role": role, "vote": vote,
           "text": (text or "")[:2000], "question": question[:500], "answer": (answer or "")[:1500], "sql": (sql or "")[:3000]}
    with _lock, FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_feedback(n: int = 30) -> list[dict]:
    if not FEEDBACK_FILE.exists():
        return []
    out = []
    for ln in reversed(FEEDBACK_FILE.read_text(encoding="utf-8").splitlines()[-n:]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------- audit ----
def audit(event: str, user: str | None, **fields) -> None:
    rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "event": event, "user": user, **fields}
    try:
        with _lock, AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def recent_audit(n: int = 50) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    out = []
    for ln in reversed(AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-n:]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out
