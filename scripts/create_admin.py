#!/usr/bin/env python3
"""
scripts/create_admin.py — One-time seed script to create an admin user.

Why a script and not a POST /users endpoint?
---------------------------------------------
This is an internal admin tool, not a consumer-facing service.  Exposing a
public "register new admin" endpoint would be a security hole: anyone who
found the endpoint could create admin accounts before the first legitimate
admin locked it down.  A local script avoids that entirely — you run it once
from a trusted machine, and the endpoint never exists.

This is the deliberate, documented approach for bootstrapping the first user.
Subsequent users (if needed) should also go through this script or a future
POST /users endpoint restricted to existing admins.

Usage
-----
  # From the project root (with .env present or env vars set):
  python scripts/create_admin.py --username alice --password <strong-password>

  # Or let the script prompt you (password is typed silently via getpass):
  python scripts/create_admin.py --username alice

The script is idempotent only in the sense that it will fail clearly if the
username already exists, rather than silently overwriting an existing account.
"""

import argparse
import getpass
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `from app.*` imports work
# regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password
from app.database import SessionLocal
from app.models import User


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an admin user in the feature-flag-service database.",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Username for the new admin account.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Plaintext password (will be bcrypt-hashed before storage). "
            "If omitted, the script prompts securely via getpass."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Prompt for password if not provided as an argument (preferred: avoids
    # the plaintext value appearing in shell history or process listings).
    password: str = args.password or getpass.getpass(
        f"Password for '{args.username}': "
    )

    if not password:
        print("ERROR: Password cannot be empty.", file=sys.stderr)
        sys.exit(1)

    hashed = hash_password(password)
    # Clear the plaintext reference immediately — belt-and-suspenders.
    del password

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == args.username).first()
        if existing:
            print(
                f"ERROR: A user with username '{args.username}' already exists.",
                file=sys.stderr,
            )
            sys.exit(1)

        user = User(
            username=args.username,
            hashed_password=hashed,
            role="admin",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(
            f"✓ Admin user '{user.username}' created successfully (id={user.id})."
        )
    except Exception as exc:
        db.rollback()
        print(f"ERROR: Failed to create user: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
