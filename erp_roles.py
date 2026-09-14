"""Roles and responsibilities from the ERP's own security tables.

- A signed-in person's ERP groups are read live at sign-in (AXUSERLEVELGROUPS with
  active dates, plus the group columns on AXUSERS).
- Groups map to modules (GROUP_MODULES in the private knowledge file, overridable from
  the admin panel); each module carries the tables it may query and a plain-language scope.
- Admin is a fixed list of ERP usernames (ADMIN_ERP_USERS), never a user-type flag.
- Optional sign-in with ERP credentials: the ERP stores an unsalted MD5 hex of the
  password; we hash what the person typed and compare in constant time. The stored hash
  never leaves this module and is never shown to the model.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
import zlib

import db
import store

try:
    import knowledge as _kn
except ImportError:
    import knowledge_example as _kn

ADMIN_ERP_USERS = frozenset(u.upper() for u in getattr(_kn, "ADMIN_ERP_USERS", []))
MODULES: dict = getattr(_kn, "MODULES", {})
_GROUP_MODULES_DEFAULT: dict = getattr(_kn, "GROUP_MODULES", {})
_USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,40}$")

_lock = threading.Lock()
_groups_cache: dict[str, tuple[float, list[dict]]] = {}
GROUPS_TTL = 300  # seconds: the admin panel's group list
SCREEN_TTL = 3600  # seconds: tables behind one ERP screen (its definition rarely changes)
_screen_cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
_access_cache: dict[tuple, tuple[float, dict]] = {}
_caption_cache: dict[str, tuple[float, dict[str, str]]] = {}
# real tables whose names are ordinary words: a match in a screen definition proves nothing
_GENERIC = frozenset({"DATA", "MASTER", "TEST", "CONTROL", "DAYS", "COMPANY", "MTYPE", "SEQUENCE", "TEMP", "VALUE",
                      "VALUES", "TYPE", "NAME", "DATE", "TIME", "USER", "USERS", "TABLE", "TEXT", "LEVEL", "STATUS",
                      "FLAG", "COUNT", "ORDER", "GROUP", "ITEMS", "ROOT", "PAGE", "FIELD", "GRID", "BUTTON", "REPORT",
                      "ROWS", "LIST", "DUAL"})


def valid_erp_username(u: str) -> bool:
    return bool(u) and bool(_USER_RE.match(u))


def group_modules() -> dict[str, list[str]]:
    """Group -> modules, defaults from knowledge merged with admin overrides."""
    merged = {k: list(v) for k, v in _GROUP_MODULES_DEFAULT.items()}
    for k, v in store.group_overrides().items():
        merged[k] = [m for m in v if m in MODULES]
    return merged


def module_info(keys: list[str]) -> list[dict]:
    out = []
    for k in keys:
        m = MODULES.get(k)
        if m:
            out.append({"key": k, "label": m.get("label", k), "scope": m.get("scope", ""), "enabled": bool(m.get("tables")),
                        "admin_only": bool(m.get("admin_only"))})
    return out


def tables_for(keys: list[str]) -> list[str]:
    t: set[str] = set()
    for k in keys:
        t.update(x.upper() for x in MODULES.get(k, {}).get("tables", []))
    return sorted(t)


# ---------------------------------------------- tables behind ERP screens ----
_SYSTEM_PREFIXES = ("AX", "BK_", "TEMP", "TMP", "IWTEMP", "DUMMY", "TEST_", "SYS_")
_BACKUP_RE = re.compile(r"(_BKP|BKP_|_BACKUP|_OLD$|_DUMMY|_TEMP$|_TMP$|SELECTION[0-9]+$|[0-9]{6,8}$|_[0-9]{2}_[0-9]{2}_[0-9]{2,4}$)")


def derivable(table: str) -> bool:
    """Tables a screen may grant: business tables only, never ERP system tables or backups."""
    t = table.upper()
    return t not in _GENERIC and not t.startswith(_SYSTEM_PREFIXES) and not _BACKUP_RE.search(t)


def _table_tokens(text: str) -> set[str]:
    """Identifiers in a screen definition that are real tables of the schema."""
    known = db.known_tables()
    toks = {t.upper() for t in re.findall(r"[A-Za-z_][\w$#]{3,}", text)}
    return {t for t in toks & known if derivable(t)}


def _tables_for_screen(name: str, stype: str) -> set[str]:
    """Tables a transaction (stype t) or report view (stype i) reads or writes, cached."""
    key = (name.lower(), stype)
    with _lock:
        hit = _screen_cache.get(key)
        if hit and time.time() - hit[0] < SCREEN_TTL:
            return set(hit[1])
    tabs: set[str] = set()
    try:
        if stype == "t":
            for (tn,) in db.fetch_internal("select tablename from axpdc where lower(tstruct) = :n", n=name.lower()):
                if tn:
                    tabs.add(str(tn).strip().upper())
            for (blob,) in db.fetch_internal("select props from tstructs where lower(name) = :n", n=name.lower()):
                data = blob.read() if hasattr(blob, "read") else blob
                if data:
                    try:  # 4-byte length prefix + zlib stream
                        xml = zlib.decompress(bytes(data)[4:]).decode("utf-8", "ignore")
                    except zlib.error:
                        xml = ""
                    tabs |= _table_tokens(xml)
        elif stype == "i":
            for (clob,) in db.fetch_internal("select props from iviews where lower(name) = :n", n=name.lower()):
                txt = clob.read() if hasattr(clob, "read") else (clob or "")
                m = re.search(r'ptable="([^"]+)"', txt)
                if m:
                    tabs.add(m.group(1).strip().upper())
                tabs |= _table_tokens(txt)
    except Exception as ex:  # a broken definition must not break sign-in
        store.audit("erp_screen_parse_failed", None, screen=name, kind=stype, error=str(ex)[:150])
    known = db.known_tables()
    tabs = {t for t in tabs if t in known and derivable(t)}
    with _lock:
        _screen_cache[key] = (time.time(), set(tabs))
    return tabs


def _screens_for_groups(groups: list[str]) -> list[tuple[str, str]]:
    """Transactions and report views the groups may open: direct rights plus rights on menu pages."""
    groups = [g for g in groups if g][:200]
    if not groups:
        return []
    binds = {f"g{i}": g.upper() for i, g in enumerate(groups)}
    ph = ", ".join(f":{k}" for k in binds)
    rows = db.fetch_internal(
        f"select distinct sname, stype from axuseraccess where upper(rname) in ({ph}) and stype in ('i', 't') "
        f"union select distinct d.sname, d.stype from axuseraccess a join axpagedetail d on d.name = a.sname "
        f"where upper(a.rname) in ({ph}) and a.stype = 'p' and d.stype in ('i', 't')",
        **binds,
    )
    return [(str(r[0]).strip(), str(r[1]).strip()) for r in rows if r[0] and r[1]]


def _captions(stype: str) -> dict[str, str]:
    with _lock:
        hit = _caption_cache.get(stype)
        if hit and time.time() - hit[0] < SCREEN_TTL:
            return hit[1]
    table = "tstructs" if stype == "t" else "iviews"
    rows = db.fetch_internal(f"select name, caption from {table}")
    caps = {str(r[0]).lower(): str(r[1] or r[0]).strip() for r in rows if r[0]}
    with _lock:
        _caption_cache[stype] = (time.time(), caps)
    return caps


def screen_access(groups: list[str], include_employee: bool = False) -> dict:
    """Everything the ERP grants these groups through its screens: the screens themselves and the
    union of the tables behind them (credential, confidential and - unless HR is granted - employee
    tables removed). Cached per group set."""
    import policy

    key = (tuple(sorted(g.lower() for g in groups)), include_employee)
    with _lock:
        hit = _access_cache.get(key)
        if hit and time.time() - hit[0] < GROUPS_TTL:
            return hit[1]
    t0 = time.time()
    screens = _screens_for_groups(groups)
    caps = {"t": _captions("t"), "i": _captions("i")}
    tabs: set[str] = set()
    items = []
    for name, stype in screens:
        t = _tables_for_screen(name, stype)
        tabs |= t
        items.append({"name": name, "type": "transaction" if stype == "t" else "report",
                      "caption": caps[stype].get(name.lower(), name), "tables": len(t)})
    tabs = {t for t in tabs if t not in policy.CREDENTIAL_TABLES and not policy.is_sensitive_table(t)}
    if not include_employee:
        tabs = {t for t in tabs if not policy.is_employee_table(t)}
    items.sort(key=lambda x: x["caption"].lower())
    out = {"tables": sorted(tabs), "screens": items, "count": len(items),
           "captions": [x["caption"] for x in items[:12]], "seconds": round(time.time() - t0, 1)}
    with _lock:
        _access_cache[key] = (time.time(), out)
    return out


# ------------------------------------------------------------- ERP lookups ----
def erp_account(erp_user: str) -> dict | None:
    """Active flag and group columns for one ERP user, or None if unknown."""
    if not valid_erp_username(erp_user):
        return None
    rows = db.fetch_internal(
        "select username, active, usergroup, allusergroup from axusers where upper(username) = :u",
        u=erp_user.upper(),
    )
    if not rows:
        return None
    username, active, usergroup, allusergroup = rows[0]
    return {"username": username, "active": (active or "").strip().upper() == "T",
            "usergroup": usergroup or "", "allusergroup": allusergroup or ""}


def erp_groups(erp_user: str) -> list[str]:
    """Active group memberships (dated table + the two group columns on the user row)."""
    acct = erp_account(erp_user)
    if not acct:
        return []
    rows = db.fetch_internal(
        "select distinct usergroup from axuserlevelgroups where upper(username) = :u "
        "and (enddate is null or enddate >= trunc(sysdate)) and (startdate is null or startdate <= sysdate)",
        u=erp_user.upper(),
    )
    groups = {r[0].strip() for r in rows if r[0]}
    for col in (acct["usergroup"], acct["allusergroup"]):
        for g in str(col).split(","):
            if g.strip():
                groups.add(g.strip())
    return sorted(groups, key=str.lower)


def erp_all_groups() -> list[dict]:
    """Every group with its number of active members (cached a few minutes)."""
    with _lock:
        hit = _groups_cache.get("all")
        if hit and time.time() - hit[0] < GROUPS_TTL:
            return hit[1]
    rows = db.fetch_internal(
        "select g.usergroup, count(distinct g.username) from axuserlevelgroups g "
        "join axusers u on upper(u.username) = upper(g.username) and u.active = 'T' "
        "where (g.enddate is null or g.enddate >= trunc(sysdate)) group by g.usergroup order by 2 desc, 1"
    )
    out = [{"group": r[0], "members": int(r[1])} for r in rows if r[0]]
    with _lock:
        _groups_cache["all"] = (time.time(), out)
    return out


def erp_password_matches(erp_user: str, password: str) -> bool:
    """Compare against the ERP's stored MD5 hex. Several common formulas are tried."""
    if not valid_erp_username(erp_user) or not password:
        return False
    rows = db.fetch_internal("select password from axusers where upper(username) = :u and active = 'T'", u=erp_user.upper())
    if not rows or not rows[0][0]:
        return False
    stored = str(rows[0][0]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", stored):
        return False
    u = erp_user
    candidates = (password, password.upper(), password.lower(), u + password, password + u,
                  u.upper() + password, u.lower() + password, password + u.upper(), password + u.lower())
    ok = False
    for c in candidates:  # evaluate all, constant-time compare each
        if hmac.compare_digest(hashlib.md5(c.encode("utf-8")).hexdigest(), stored):
            ok = True
    return ok


def resolve(erp_user: str) -> dict:
    """Everything the app needs to know about an ERP user's access."""
    acct = erp_account(erp_user)
    if not acct:
        return {"exists": False, "active": False, "erp_user": erp_user, "groups": [], "modules": [], "tables": [], "is_admin": False}
    groups = erp_groups(erp_user)
    is_admin = erp_user.upper() in ADMIN_ERP_USERS
    gm = {k.lower(): v for k, v in group_modules().items()}  # ERP group names vary in case
    keys: list[str] = []
    for g in groups:
        for m in gm.get(g.lower(), []):
            if m in MODULES and m not in keys:
                keys.append(m)
    if not is_admin:  # modules flagged admin_only (employee data) need the assistant admin, not just the ERP group
        keys = [k for k in keys if not MODULES[k].get("admin_only")]
    tables = set(tables_for(keys))
    screens = {"count": 0, "captions": [], "tables": 0}
    if not is_admin and groups:
        try:
            sa = screen_access(groups, include_employee=("hr" in keys))
            tables |= set(sa["tables"])
            screens = {"count": sa["count"], "captions": sa["captions"], "tables": len(sa["tables"])}
            store.audit("erp_screens", acct["username"], screens=sa["count"], tables=len(sa["tables"]), seconds=sa["seconds"])
        except Exception as ex:  # fall back to the module tables only
            store.audit("erp_screens_failed", acct["username"], error=str(ex)[:200])
    return {
        "exists": True, "active": acct["active"], "erp_user": acct["username"], "groups": groups,
        "modules": keys, "tables": sorted(tables), "screens": screens, "is_admin": is_admin,
    }
