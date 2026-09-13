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
            out.append({"key": k, "label": m.get("label", k), "scope": m.get("scope", ""), "enabled": bool(m.get("tables"))})
    return out


def tables_for(keys: list[str]) -> list[str]:
    t: set[str] = set()
    for k in keys:
        t.update(x.upper() for x in MODULES.get(k, {}).get("tables", []))
    return sorted(t)


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
    return {
        "exists": True, "active": acct["active"], "erp_user": acct["username"], "groups": groups,
        "modules": keys, "tables": tables_for(keys), "is_admin": is_admin,
    }
