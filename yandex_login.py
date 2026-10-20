"""Run once to sign in through Yandex and save .env beside this script."""

import sys
from pathlib import Path

from alice_client.passport_auth import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--oauth-app" in args:
        from alice_client.env_auth import main

        args.remove("--oauth-app")
    raise SystemExit(main(args, default_env=Path(__file__).resolve().parent / ".env"))
