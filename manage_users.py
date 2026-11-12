#!/usr/bin/env python3
"""
Manage Image Store user accounts from the command line.

  docker compose exec app python manage_users.py add alice                    # regular user; prompts for password
  docker compose exec app python manage_users.py add jane --role annotator
  docker compose exec app python manage_users.py add bob --role admin         # (--admin also works)
  docker compose exec app python manage_users.py role alice annotator         # change someone's role
  docker compose exec app python manage_users.py passwd alice         # reset a password
  docker compose exec app python manage_users.py list
  docker compose exec app python manage_users.py remove alice
"""
import argparse
import getpass
import sys

from fastapi import HTTPException

import app as store


def ask_password():
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match")
    return pw


def main():
    p = argparse.ArgumentParser(description="Image Store user accounts")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("username")
    a.add_argument("--role", choices=store.ROLES, default="user"); a.add_argument("--admin", action="store_true")
    ro = sub.add_parser("role"); ro.add_argument("username"); ro.add_argument("role", choices=store.ROLES)
    pw = sub.add_parser("passwd"); pw.add_argument("username")
    sub.add_parser("list")
    r = sub.add_parser("remove"); r.add_argument("username")
    args = p.parse_args()

    try:
        if args.cmd == "add":
            role = "admin" if args.admin else args.role
            store.create_user(args.username, ask_password(), role)
            print(f"Created {args.username} ({store.ROLE_LABELS[role]})")
        elif args.cmd == "role":
            store.set_role(args.username, args.role)
            print(f"{args.username} is now {store.ROLE_LABELS[args.role]}")
        elif args.cmd == "passwd":
            if not store.users.find_one({"username": args.username}):
                sys.exit("No such user")
            new = ask_password()
            store.check_new_password(new)
            store.users.update_one({"username": args.username}, {"$set": {"password_hash": store.hash_password(new)}})
            store.sessions.delete_many({"username": args.username})
            print(f"Password changed for {args.username}")
        elif args.cmd == "list":
            for u in store.users.find({}, {"password_hash": 0}).sort("username", 1):
                print(f'{u["username"]:<24} {store.ROLE_LABELS[store.role_of(u)]}')
        elif args.cmd == "remove":
            if not store.users.delete_one({"username": args.username}).deleted_count:
                sys.exit("No such user")
            store.sessions.delete_many({"username": args.username})
            print(f"Removed {args.username} (their images are kept)")
    except HTTPException as e:
        sys.exit(e.detail)


if __name__ == "__main__":
    main()
