"""Configuration: reads .env in this folder (no extra dependency) and exposes CFG."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


_ENV = _load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name) or _ENV.get(name) or default


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    model: str
    effort: str
    db_profile: str
    max_rows: int
    sql_timeout_sec: float
    daily_budget_usd: float
    port: int
    max_tool_rounds: int
    oracle_client_dir: str


def _norm_profile(p: str) -> str:
    p = (p or "ORA_").upper()
    if not p.endswith("_"):
        p += "_"
    if not re.fullmatch(r"ORA\d?_", p):
        raise ValueError(f"DB_PROFILE must look like ORA_ or ORA3_, got {p!r}")
    return p


CFG = Config(
    anthropic_api_key=_get("ANTHROPIC_API_KEY"),
    model=_get("CHAT_MODEL", "claude-opus-5"),
    effort=_get("CHAT_EFFORT", "low"),
    db_profile=_norm_profile(_get("DB_PROFILE", "ORA_")),
    max_rows=int(_get("MAX_ROWS", "200")),
    sql_timeout_sec=float(_get("SQL_TIMEOUT_SEC", "60")),
    daily_budget_usd=float(_get("DAILY_BUDGET_USD", "2.00")),
    port=int(_get("PORT", "8765")),
    max_tool_rounds=int(_get("MAX_TOOL_ROUNDS", "12")),
    oracle_client_dir=_get("ORACLE_CLIENT_DIR", ""),
)
