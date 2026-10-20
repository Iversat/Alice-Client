"""Explicit local .env login, without reading browser cookies or passwords."""

from __future__ import annotations

import argparse
import getpass
import math
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

from dotenv import dotenv_values, set_key

from .auth import AuthError, OAuthToken, YandexAuth
from .client import positive_timeout

TOKEN_KEYS = (
    "YANDEX_OAUTH_TOKEN",
    "YANDEX_REFRESH_TOKEN",
    "YANDEX_TOKEN_EXPIRES_AT",
    "YANDEX_TOKEN_SCOPE",
)


def read_env(path: Path | None = None) -> dict[str, str | None]:
    path = path if path is not None else Path.cwd() / ".env"
    if not path.exists():
        return {}
    return dict(dotenv_values(path, interpolate=False, encoding="utf-8-sig"))


def setting(name: str, path: Path | None = None) -> str | None:
    return os.getenv(name, "").strip() or (read_env(path).get(name) or "").strip() or None


def file_token(path: Path | None = None) -> OAuthToken | None:
    values = read_env(path)
    token = (values.get("YANDEX_OAUTH_TOKEN") or "").strip()
    if not token:
        return None
    expiry = values.get("YANDEX_TOKEN_EXPIRES_AT")
    try:
        expires_at = float(expiry) if expiry else None
        if expires_at is not None and not math.isfinite(expires_at):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Некорректный срок действия токена в .env. Выполните вход повторно."
        ) from None
    return OAuthToken(
        token,
        values.get("YANDEX_REFRESH_TOKEN") or None,
        expires_at,
        values.get("YANDEX_TOKEN_SCOPE") or None,
        values.get("YANDEX_CLIENT_ID") or None,
    )


def save_env_token(path: Path, token: OAuthToken) -> None:
    """Replace the complete token set atomically, preserving unrelated entries."""
    if path.is_symlink():
        raise ValueError("Файл .env не должен быть символической ссылкой.")
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    values = {
        "YANDEX_CLIENT_ID": token.client_id or "",
        "YANDEX_OAUTH_TOKEN": token.access_token,
        "YANDEX_REFRESH_TOKEN": token.refresh_token or "",
        "YANDEX_TOKEN_EXPIRES_AT": str(token.expires_at) if token.expires_at is not None else "",
        "YANDEX_TOKEN_SCOPE": token.scope or "",
    }
    fd, filename = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(old)
        for key, value in values.items():
            set_key(temp, key, value, quote_mode="always")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None, *, default_env: Path | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Вход через Яндекс с сохранением токена в .env.")
    parser.add_argument("--client-id", help="ClientID вашего OAuth-приложения")
    parser.add_argument("--env-file", type=Path, default=default_env or Path.cwd() / ".env")
    parser.add_argument("--scope", help="права, разрешённые в настройках приложения")
    parser.add_argument("--secret-prompt", action="store_true", help="скрытый ввод Client secret")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--wait", type=float, default=600)
    parser.add_argument("--refresh", action="store_true", help="обновить токен из этого .env")
    args = parser.parse_args(argv)
    try:
        wait = positive_timeout(args.wait)
        client_id = args.client_id or setting("YANDEX_CLIENT_ID", args.env_file)
        if not client_id:
            print("Один раз создайте приложение на https://oauth.yandex.ru/ и скопируйте ClientID.")
            print("Для чтения умного дома приложению нужен доступ iot:view.")
            client_id = input("ClientID приложения: ").strip()
        secret = setting("YANDEX_CLIENT_SECRET", args.env_file)
        if args.secret_prompt:
            secret = getpass.getpass("Client secret (ввод скрыт; не сохраняется): ").strip() or None
        service = YandexAuth(client_id, secret)
        if args.refresh:
            old = file_token(args.env_file)
            if old is None:
                raise ValueError("В выбранном .env нет токена. Сначала выполните вход.")
            token = service.refresh(old)
        else:
            code = service.start(args.scope)
            print(f"Откройте {code.verification_url}\nКод устройства: {code.user_code}", flush=True)
            print(
                "Подтвердите вход и доступ на сайте Яндекса. Токен будет получен автоматически.",
                flush=True,
            )
            if not args.no_browser:
                webbrowser.open(code.verification_url)
            token = service.wait(code, timeout=wait)
        save_env_token(args.env_file, token)
        print(f"Вход сохранён: {args.env_file.resolve()}")
        print("Запуск: .\\.venv\\Scripts\\python.exe -m alice_client chat")
        if os.getenv("YANDEX_OAUTH_TOKEN", "").strip():
            print("Внимание: YANDEX_OAUTH_TOKEN в окружении имеет приоритет над .env.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Вход отменён. Сохранённый токен не изменён.", file=sys.stderr)
        return 130
    except (AuthError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (AuthError, ValueError)) else type(error).__name__
        print(f"Ошибка входа: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
