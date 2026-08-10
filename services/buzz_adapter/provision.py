"""Generate a one-time Buzz bearer token and its hashed identity record."""

import argparse
import json
import secrets
from os import getenv

from services.buzz_adapter.auth import token_digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    pepper = getenv("BUZZ_TOKEN_PEPPER")
    if not pepper:
        raise SystemExit("BUZZ_TOKEN_PEPPER is required")
    token = f"buzz_{secrets.token_urlsafe(32)}"
    record = {
        "subject": f"buzz:{args.user_id}",
        "token_hash": token_digest(token, pepper),
        "active": True,
    }
    print(f"token={token}")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
