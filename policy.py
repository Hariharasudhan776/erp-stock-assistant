"""Role policy: what each session may query (enforced server-side, independent of the
model) and the role-specific instructions appended to the system prompt.

- Credential tables and password columns are unreadable for everyone, admins included.
- Restricted sessions carry an allow-list of tables (from their ERP modules, or the default
  module set for unlinked accounts). Anything else is rejected before reaching Oracle.
"""
from __future__ import annotations

import re

try:
    from knowledge import USER_TABLES as _USER_TABLES
except ImportError:
    from knowledge_example import USER_TABLES as _USER_TABLES

ROLES = ("admin", "user")

REFUSAL = "You don't have enough privileges to access this information. Please contact the admin."
UNKNOWN = "I don't have enough information to answer this yet. Please contact the admin so they can teach me."

USER_TABLES = frozenset(t.upper() for t in _USER_TABLES) | {"DUAL"}

# Never readable through the assistant, whatever the role.
CREDENTIAL_TABLES = frozenset({
    "AXUSERS", "AXUSERSPWDPOLICY", "AXUSERS_AUDIT_LOG", "TEMP_AXUSERS", "AXUSRHISTORY",
    "PUSERHISTORY", "AXUSERSMAILSTEMP",
})
CREDENTIAL_TOKENS = frozenset({"PASSWORD", "PWD", "PPASSWORD", "EMAILPASSWORD", "SINGLELOGINKEY", "ACCESSCODE", "PREVPWDS"})

_DICT_PREFIXES = ("ALL_", "DBA_", "USER_", "V$", "GV$", "CDB_", "SYS_", "AUD$", "X$")
_STOP = {
    "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "ON", "USING",
    "GROUP", "ORDER", "HAVING", "UNION", "MINUS", "INTERSECT", "FETCH", "SELECT", "WITH",
    "CONNECT", "START", "MODEL", "PIVOT", "UNPIVOT", "OFFSET", "FOR", "SAMPLE", "PARTITION",
}
_TOKEN = re.compile(r"[A-Z_][A-Z0-9_$#]*|[(),.]")
_IDENT = re.compile(r"[A-Z_][A-Z0-9_$#]*")
_CTE = re.compile(r"\b([A-Z_][A-Z0-9_$#]*)\s*(?:\([^)]*\))?\s+AS\s*\(")


def _strip(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"[^"]*"', " ", sql)
    return sql.upper()


def tables_referenced(sql: str) -> set[str]:
    """Names that appear as a table source (after FROM / JOIN, including comma lists)."""
    s = _strip(sql)
    ctes = set(_CTE.findall(s))
    toks = _TOKEN.findall(s)
    refs: set[str] = set()
    i, n = 0, len(toks)
    while i < n:
        if toks[i] in ("FROM", "JOIN"):
            j = i + 1
            while j < n:
                if toks[j] == "(" or toks[j] in _STOP:
                    break
                name = toks[j]
                if j + 2 < n and toks[j + 1] == ".":  # OWNER.TABLE
                    name = toks[j + 2]
                    j += 2
                if re.match(r"[A-Z_]", name):
                    refs.add(name)
                k = j + 1
                while k < n and toks[k] not in (",", "(", ")") and toks[k] not in _STOP:
                    k += 1
                if k < n and toks[k] == ",":
                    j = k + 1
                    continue
                break
            i = j
        i += 1
    return refs - ctes


def check_sql(sql: str, role: str, allowed_tables=None) -> None:
    """Raise PermissionError when the statement is outside the session's scope."""
    s = _strip(sql)
    idents = set(_IDENT.findall(s))
    refs = tables_referenced(sql)
    if idents & CREDENTIAL_TOKENS or refs & CREDENTIAL_TABLES:
        raise PermissionError("Credential tables and password columns are never readable through this assistant.")
    if role == "admin":
        return
    for tok in idents:
        if tok.startswith(_DICT_PREFIXES) or tok in ("SYS", "SYSTEM"):
            raise PermissionError("The data dictionary is outside this user's privileges. " + REFUSAL)
    allowed = (frozenset(t.upper() for t in allowed_tables) | {"DUAL"}) if allowed_tables is not None else USER_TABLES
    bad = sorted(t for t in refs if t not in allowed)
    if bad:
        raise PermissionError(f"Access to {', '.join(bad)} is outside this user's privileges. " + REFUSAL)


def table_allowed(table: str, role: str, allowed_tables=None) -> bool:
    t = table.upper()
    if t in CREDENTIAL_TABLES:
        return False
    if role == "admin":
        return True
    allowed = (frozenset(x.upper() for x in allowed_tables) | {"DUAL"}) if allowed_tables is not None else USER_TABLES
    return t in allowed


def role_prompt(role: str, modules: list[dict] | None = None) -> str:
    if role == "admin":
        return (
            "# Current user: ADMIN\n"
            "- Full read access to the schema (credential tables excepted). Use describe_table / search_schema / "
            "sample_rows freely.\n"
            "- LEARNING MODE. When you cannot answer because you lack a table, a join key or a business rule, "
            "do NOT guess and do NOT apologise at length: ask the admin one or two precise questions "
            "(e.g. \"Which table records MR approvals, and what does status 'C' mean?\"). When the admin answers, "
            "or confirms a rule you verified with a query, call save_knowledge so every future session knows it. "
            "Also call save_knowledge when the admin corrects you. Save only facts confirmed by the admin or verified "
            "against the data, written as short reusable rules (table, columns, join key, meaning).\n"
            "- The interface shows your SQL to the admin, so keep SQL out of the prose unless asked.\n"
        )
    enabled = [m for m in (modules or []) if m.get("enabled")]
    lines = ["# Current user: RESTRICTED USER (access follows their ERP roles)"]
    if enabled:
        lines.append("- You may answer questions ONLY in these modules:")
        for m in enabled:
            lines.append(f"  * {m['label']}: {m.get('scope') or 'as named'}")
        lines.append(
            "- For ANYTHING else (other modules, employees, HR, payroll, salaries, finance, accounts, payments, "
            "contracts, general knowledge, coding, or the database structure itself) run no query and reply exactly: "
            f"\"{REFUSAL}\""
        )
        lines.append(
            "- Decide by TOPIC first: purchase orders, PO approvals, vendors' orders and quotations belong to Purchasing; "
            "employees and salaries to HR; ledgers, payments and bills to Finance. If the topic's module is not in the list "
            "above, use the privileges message - never the 'not enough information' message, and never run a query to check."
        )
    else:
        lines.append(
            f"- This user has no enabled modules. Run no query and reply exactly: \"{REFUSAL}\" to every data question. "
            "You may still greet them and explain that access is granted by the admin."
        )
    lines.append(f"- If a question inside their modules needs a table or rule you do not have, do not guess; reply: \"{UNKNOWN}\"")
    lines.append("- Never show SQL, table names or column names to this user. Never call save_knowledge, describe_table, "
                 "search_schema or sample_rows for this user.")
    return "\n".join(lines) + "\n"
