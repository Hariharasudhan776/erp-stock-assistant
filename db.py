"""Read-only Oracle access for the stock assistant.

- Credentials come from the environment (on Windows also HKCU\\Environment, the same
  ORA_* values `setx` writes). Nothing is stored in this folder.
- Every statement is checked to be a single SELECT / WITH before it runs, the role
  policy is applied (restricted users can only touch stock tables), and the session
  is opened READ ONLY as a further line of defence.
- Results are capped (max rows) and time-boxed (call_timeout).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time

import oracledb

import policy
from config import CFG

# Oracle client for thick mode comes from ORACLE_CLIENT_DIR in .env; empty = thin mode.

_lock = threading.Lock()
_conn: oracledb.Connection | None = None
_client_ready = False
_known: set[str] | None = None


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except (ImportError, FileNotFoundError, OSError):
        return None


def profile() -> dict:
    p = CFG.db_profile  # "ORA_" or "ORA3_"
    return {
        "user": _env(p + "USER"),
        "pwd": _env(p + "PWD"),
        "dsn": _env(p + "DSN"),
        "schema": _env(p + "SCHEMA") or "",
    }


def _init_client() -> None:
    global _client_ready
    if _client_ready:
        return
    if not CFG.oracle_client_dir:
        _client_ready = True  # thin mode (fine for Oracle 12.1+; older servers need a client dir)
        return
    try:
        oracledb.init_oracle_client(lib_dir=CFG.oracle_client_dir)
    except Exception as e:  # already initialised is fine
        if "already initialized" not in str(e).lower():
            raise
    _client_ready = True


def connect() -> oracledb.Connection:
    """Return a shared connection, reconnecting if it has dropped."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.ping()
                return _conn
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None
        _init_client()
        p = profile()
        missing = [k for k in ("user", "pwd", "dsn") if not p[k]]
        if missing:
            raise RuntimeError(
                "Oracle credentials missing in the environment: "
                + ", ".join(CFG.db_profile + m.upper() for m in missing)
            )
        conn = oracledb.connect(user=p["user"], password=p["pwd"], dsn=p["dsn"])
        conn.call_timeout = int(CFG.sql_timeout_sec * 1000)
        with conn.cursor() as c:
            if p["schema"] and re.fullmatch(r"[A-Za-z0-9_$#]+", p["schema"]):
                c.execute("alter session set current_schema = " + p["schema"])
            c.execute("set transaction read only")
        _conn = conn
        return conn


# ---------------------------------------------------------------- safety ---

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"commit|rollback|savepoint|lock|call|execute|begin|declare|purge|rename|"
    r"comment|audit|noaudit|flashback|analyze|associate|disassociate)\b",
    re.I,
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_select(sql: str) -> str:
    """Return a cleaned statement or raise ValueError."""
    s = sql.strip().rstrip(";").rstrip("/").strip()
    bare = _strip_comments(s)
    if not bare.strip():
        raise ValueError("Empty statement.")
    if ";" in bare:
        raise ValueError("Only one statement at a time.")
    first = bare.strip().split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise ValueError("Only SELECT (or WITH ... SELECT) statements are allowed.")
    # strip string literals before scanning for forbidden verbs
    no_lit = re.sub(r"'(?:[^']|'')*'", "''", bare)
    m = _FORBIDDEN.search(no_lit)
    if m:
        raise ValueError(f"Statement contains a non-read-only keyword: {m.group(0).upper()}")
    if re.search(r"\bfor\s+update\b", no_lit, re.I):
        raise ValueError("FOR UPDATE is not allowed.")
    return s


# --------------------------------------------------------------- results ---

def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, _dt.datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%d/%m/%Y")
        return v.strftime("%d/%m/%Y %H:%M:%S")
    if isinstance(v, _dt.date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.3f}".rstrip("0").rstrip(".")  # no thousands separators: keeps CSV numeric in Excel
    if isinstance(v, oracledb.LOB):
        try:
            return str(v.read())[:2000]
        except Exception:
            return "<lob>"
    return str(v)


class QueryResult:
    def __init__(self, sql: str, columns: list[str], rows: list[list], truncated: bool, elapsed: float):
        self.sql = sql
        self.columns = columns
        self.rows = rows  # already formatted as strings
        self.truncated = truncated
        self.elapsed = elapsed

    @property
    def rowcount(self) -> int:
        return len(self.rows)

    def as_text(self, max_width: int = 60, max_rows: int | None = None) -> str:
        """Compact pipe table for the model (optionally only the first rows, to save tokens)."""
        if not self.columns:
            return "(no columns)"
        shown = self.rows if not max_rows or len(self.rows) <= max_rows else self.rows[:max_rows]
        widths = [
            min(max_width, max(len(c), *(len(r[i]) for r in shown)) if shown else len(c))
            for i, c in enumerate(self.columns)
        ]

        def line(vals):
            return " | ".join(v[: widths[i]].ljust(widths[i]) for i, v in enumerate(vals))

        out = [line(self.columns), "-+-".join("-" * w for w in widths)]
        out += [line(r) for r in shown]
        tail = f"({self.rowcount} rows"
        if len(shown) < self.rowcount:
            tail += f"; you are shown the first {len(shown)} - the user sees all {self.rowcount} in the grid, so summarise or aggregate instead of listing"
        if self.truncated:
            tail += f", truncated to the first {self.rowcount} - add filters or aggregate if you need more"
        tail += f", {self.elapsed:.1f}s)"
        out.append(tail)
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {
            "sql": self.sql,
            "columns": self.columns,
            "rows": self.rows,
            "rowcount": self.rowcount,
            "truncated": self.truncated,
            "elapsed": round(self.elapsed, 2),
        }


def run_select(sql: str, role: str = "admin", max_rows: int | None = None, allowed_tables=None) -> QueryResult:
    """Validate, apply the role policy, and run one SELECT.

    Raises ValueError (bad SQL), PermissionError (outside the role's scope) or oracledb.Error.
    """
    max_rows = max_rows or CFG.max_rows
    clean = validate_select(sql)
    policy.check_sql(clean, role, allowed_tables)
    conn = connect()
    t0 = time.time()
    with _lock:
        with conn.cursor() as cur:
            cur.arraysize = min(max_rows + 1, 1000)
            cur.execute(clean)
            cols = [d[0] for d in cur.description]
            raw = cur.fetchmany(max_rows + 1)
    truncated = len(raw) > max_rows
    rows = [[fmt(v) for v in r] for r in raw[:max_rows]]
    return QueryResult(clean, cols, rows, truncated, time.time() - t0)


def fetch_internal(sql: str, **binds) -> list[tuple]:
    """Internal read for the app itself (ERP roles, sign-in). Bind variables only, never model input."""
    conn = connect()
    with _lock, conn.cursor() as cur:
        cur.execute(sql, binds)
        return cur.fetchmany(2000)


# ------------------------------------------------------ schema helpers ----

def schema_owner() -> str:
    return profile()["schema"]


def known_tables() -> set[str]:
    """All table and view names of the schema (cached for the life of the process)."""
    global _known
    if _known is None:
        owner = schema_owner()
        conn = connect()
        with _lock, conn.cursor() as cur:
            cur.execute(
                "select table_name from all_tables where owner = :o "
                "union select view_name from all_views where owner = :o",
                o=owner,
            )
            _known = {r[0] for r in cur.fetchall()}
    return _known


def describe_table(name: str, role: str = "admin") -> str:
    name = name.strip().upper().split(".")[-1]
    if not re.fullmatch(r"[A-Z0-9_$#]+", name):
        return f"Invalid table name: {name!r}"
    if not policy.table_allowed(name, role):
        return policy.REFUSAL
    owner = schema_owner()
    conn = connect()
    with _lock, conn.cursor() as cur:
        cur.execute(
            """select column_name, data_type,
                      case when data_type like '%CHAR%' then data_length end len,
                      nullable
                 from all_tab_columns
                where owner = :o and table_name = :t
                order by column_id""",
            o=owner,
            t=name,
        )
        cols = cur.fetchall()
        if not cols:
            cur.execute(
                "select object_type from all_objects where owner = :o and object_name = :t",
                o=owner,
                t=name,
            )
            r = cur.fetchone()
            if not r:
                return f"No table or view named {name} in {owner}. Use search_schema to find the right name."
        cur.execute(
            "select num_rows, last_analyzed from all_tables where owner = :o and table_name = :t",
            o=owner,
            t=name,
        )
        stat = cur.fetchone()
    lines = [f"{owner}.{name}"]
    if stat and stat[0] is not None:
        lines.append(f"~{int(stat[0]):,} rows (optimizer stats {fmt(stat[1])})")
    for c, t, ln, nul in cols:
        typ = f"{t}({ln})" if ln else t
        lines.append(f"  {c} {typ}{'' if nul == 'Y' else ' NOT NULL'}")
    return "\n".join(lines)


def search_schema(keyword: str, role: str = "admin", limit: int = 40) -> str:
    """Find tables and columns whose name contains the keyword (restricted roles see only their tables)."""
    kw = keyword.strip().upper()
    if not kw or not re.fullmatch(r"[A-Z0-9_%$#]+", kw):
        return "Give a single keyword (letters/digits/underscore), e.g. GRN or BATCH."
    owner = schema_owner()
    conn = connect()
    like = f"%{kw}%"
    with _lock, conn.cursor() as cur:
        cur.execute(
            """select table_name, num_rows from all_tables
                where owner = :o and table_name like :k
                  and table_name not like 'BK%' and table_name not like '%BKP%'
                  and table_name not like '%SELECTION%' and table_name not like 'IWTEMP%'
                order by num_rows desc nulls last fetch first :n rows only""",
            o=owner,
            k=like,
            n=limit,
        )
        tables = cur.fetchall()
        cur.execute(
            """select table_name, column_name, data_type from all_tab_columns
                where owner = :o and column_name like :k
                  and table_name not like 'BK%' and table_name not like '%BKP%'
                  and table_name not like '%SELECTION%' and table_name not like 'IWTEMP%'
                order by table_name fetch first :n rows only""",
            o=owner,
            k=like,
            n=limit,
        )
        columns = cur.fetchall()
    if role != "admin":
        tables = [t for t in tables if policy.table_allowed(t[0], role)]
        columns = [c for c in columns if policy.table_allowed(c[0], role)]
    out = [f"Tables matching {kw} ({len(tables)} shown):"]
    out += [f"  {t}  ~{int(n):,} rows" if n is not None else f"  {t}" for t, n in tables] or ["  (none)"]
    out.append(f"Columns matching {kw} ({len(columns)} shown):")
    out += [f"  {t}.{c} {d}" for t, c, d in columns] or ["  (none)"]
    return "\n".join(out)


def sample_rows(table: str, n: int = 5, role: str = "admin") -> str:
    table = table.strip().upper().split(".")[-1]
    if not re.fullmatch(r"[A-Z0-9_$#]+", table):
        return f"Invalid table name: {table!r}"
    if not policy.table_allowed(table, role):
        return policy.REFUSAL
    n = max(1, min(int(n), 20))
    try:
        res = run_select(f"select * from {table} fetch first {n} rows only", role=role, max_rows=n)
    except (oracledb.Error, PermissionError) as e:
        return f"Error: {str(e).splitlines()[0]}"
    return res.as_text(max_width=40)


def connection_summary() -> str:
    p = profile()
    return f"{p['user']}@{p['dsn']}" + (f" schema {p['schema']}" if p["schema"] else "")
