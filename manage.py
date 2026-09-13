"""Command-line user management (works even when you are locked out of the page).

    python manage.py list
    python manage.py create <username> <role:admin|user>        (password asked interactively)
    python manage.py password <username>                          (new password asked interactively)
    python manage.py role <username> <admin|user>
    python manage.py delete <username>
"""
from __future__ import annotations

import getpass
import sys

import auth


def _ask_password() -> str:
    p1 = getpass.getpass("New password (min 8 chars): ")
    p2 = getpass.getpass("Repeat: ")
    if p1 != p2:
        sys.exit("Passwords do not match.")
    return p1


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("list", "create", "password", "role", "delete"):
        print(__doc__)
        return
    cmd = argv[0]
    try:
        if cmd == "list":
            for u in auth.list_users():
                print(f"{u['username']:<20} {u['role']:<6} created {u['created']}  last login {u['last_login']}")
            if not auth.list_users():
                print("(no users yet)")
        elif cmd == "create":
            username, role = argv[1], argv[2]
            auth.create_user(username, _ask_password(), role, by="cli")
            print("created", username, role)
        elif cmd == "password":
            auth.set_password(argv[1], _ask_password(), by="cli")
            print("password changed; existing sessions signed out")
        elif cmd == "role":
            auth.set_role(argv[1], argv[2], by="cli")
            print("role updated")
        elif cmd == "delete":
            auth.delete_user(argv[1], by="cli")
            print("deleted")
    except (IndexError, ValueError) as e:
        sys.exit(f"error: {e}" if str(e) else __doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
