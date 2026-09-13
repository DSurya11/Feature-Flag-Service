#!/usr/bin/env python3
"""
scripts/reset_admin_password.py — Reset an existing user's password to a known value.

Usage (from project root):
  python3 scripts/reset_admin_password.py --username admin --password admin123
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password
from app.database import SessionLocal
from app.models import User


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            print(f"ERROR: User '{args.username}' not found.", file=sys.stderr)
            sys.exit(1)
        user.hashed_password = hash_password(args.password)
        db.commit()
        print(f"✓ Password for '{args.username}' reset successfully.")
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
