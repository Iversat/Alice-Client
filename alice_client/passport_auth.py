"""User-confirmed Yandex Passport sign-in without a user-registered OAuth app.

Protocol adapted from AlexxIT/YandexStation (MIT), see THIRD_PARTY_NOTICES.md.
Only this process's temporary session is used, never an existing browser cookie store.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .auth import AuthError, OAuthToken
from .client import positive_timeout
from .env_auth import save_env_token

PASSPORT = "https://passport.yandex.ru"
API = PASSPORT + "/pwl-yandex/api/passport/"
MOBILE = "https://mobileproxy.passport.yandex.net"
# Published application identifiers used by the upstream session-token flow.
# These identify the client application; they are not a user's credentials.
SESSION_CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
SESSION_CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"
ALLOWED_HOSTS = {"passport.yandex.ru", "mobileproxy.passport.yandex.net"}
MAX_RESPONSE = 2 * 1024 * 1024


class _PassportRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Token exchange and authenticated requests must never be redirected.
        if req.get_method() != "GET" or req.has_header("Authorization"):
            raise AuthError("request_failed")
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname or new.port not in {None, 443}:
            raise AuthError("request_failed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_login_link(link: str) -> str:
    parsed = urlsplit(link)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "passport.yandex.ru"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.path != "/am/push/qrsecure"
        or parsed.fragment
    ):
        raise AuthError("malformed_response")
    query = parse_qs(parsed.query)
    if not query.get("track_id") or not query.get("magic"):
        raise AuthError("malformed_response")
    return link


class PassportLogin:
    def __init__(self, *, timeout: float = 15):
        self.timeout = positive_timeout(timeout)
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(_PassportRedirect(), HTTPCookieProcessor(self.cookies))
        self._csrf: str | None = None
        self._track: dict | None = None

    def _request(self, url: str, *, data=None, as_json=False, headers=None, text=False):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("Неизвестный адрес Яндекса.")
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        if self._csrf and parsed.hostname == "passport.yandex.ru":
            request_headers["X-CSRF-Token"] = self._csrf
        body = None
        if data is not None:
            body = json.dumps(data).encode() if as_json else urlencode(data).encode()
            request_headers["Content-Type"] = (
                "application/json" if as_json else "application/x-www-form-urlencoded"
            )
        try:
            request = Request(url, data=body, headers=request_headers)
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise AuthError("malformed_response")
            if text:
                return raw.decode("utf-8")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AuthError("malformed_response")
            if result.get("errors") or result.get("error") or result.get("status") == "error":
                raise AuthError("access_denied")
            return result
        except HTTPError as error:
            raise AuthError(f"http_{error.code}") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise AuthError("request_failed") from None

    def start(self) -> str:
        page = self._request(PASSPORT + "/pwl-yandex", text=True)
        match = re.search(r'__CSRF__\s*=\s*"([^"\r\n]+)', page)
        if not match:
            raise AuthError("malformed_response")
        self._csrf = match[1]
        track = self._request(
            API + "auth/password/submit", data={"retpath": PASSPORT + "/"}, as_json=True
        )
        if not isinstance(track.get("track_id"), str) or not track["track_id"]:
            raise AuthError("malformed_response")
        self._track = track
        result = self._request(
            API + "auth/magic/code",
            data={
                "location_id": "0",
                "magic_track_id": track["track_id"],
                "track_id": "",
            },
        )
        link = result.get("link")
        if not isinstance(link, str):
            raise AuthError("malformed_response")
        return validate_login_link(link)

    def poll(self) -> OAuthToken | None:
        if self._track is None:
            raise ValueError("Сначала начните вход.")
        response = self._request(API + "auth/magic/code/status", data=self._track, as_json=True)
        if response.get("state") != "otp_auth_finished":
            if response.get("state") in {"expired", "failed", "denied", "cancelled"}:
                raise AuthError("access_denied")
            return None
        track_id = response.get("trackId")
        if not isinstance(track_id, str) or not track_id:
            raise AuthError("malformed_response")
        self._request(API + "sessions/get_session", data={"track_id": track_id})
        cookie = "; ".join(
            f"{item.name}={item.value}"
            for item in self.cookies
            if item.domain.lstrip(".") in {"yandex.ru", "passport.yandex.ru"}
        )
        if not cookie:
            raise AuthError("malformed_response")
        result = self._request(
            MOBILE + "/1/bundle/oauth/token_by_sessionid",
            data={
                "client_id": SESSION_CLIENT_ID,
                "client_secret": SESSION_CLIENT_SECRET,
            },
            headers={"Ya-Client-Host": "passport.yandex.ru", "Ya-Client-Cookie": cookie},
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise AuthError("malformed_response")
        # Validate the newly issued token before writing it. No profile data is saved.
        info = self._request(
            MOBILE + "/1/bundle/account/short_info/?avatar_size=islands-300",
            headers={"Authorization": f"OAuth {token}"},
        )
        if info.get("status") != "ok":
            raise AuthError("access_denied")
        return OAuthToken(token)

    def wait(self, *, timeout: float = 600, cancel: threading.Event | None = None) -> OAuthToken:
        deadline = time.monotonic() + positive_timeout(timeout)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            delay = min(2, max(0, remaining))
            if cancel is not None:
                if cancel.wait(delay):
                    raise AuthError("access_denied")
            else:
                time.sleep(delay)
            if time.monotonic() >= deadline:
                break
            token = self.poll()
            if token is not None:
                return token
        raise AuthError("expired_token")

    def close(self):
        self.cookies.clear()
        self._csrf = None
        self._track = None


def main(argv=None, *, default_env: Path | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Вход на сайте Яндекса → OAuth-токен в .env")
    parser.add_argument("--env-file", type=Path, default=default_env or Path.cwd() / ".env")
    parser.add_argument(
        "--no-browser", action="store_true", help="показать ссылку, открыть вручную"
    )
    parser.add_argument("--wait", type=float, default=600, help="таймаут подтверждения, секунды")
    args = parser.parse_args(argv)
    service = PassportLogin()
    try:
        timeout = positive_timeout(args.wait)
        link = service.start()
        print("Подтвердите вход в свой аккаунт на странице Яндекса.", flush=True)
        if args.no_browser or not webbrowser.open(link):
            print(link, flush=True)
        else:
            print("Страница Яндекса открыта в браузере. Ожидаю подтверждение…", flush=True)
        token = service.wait(timeout=timeout)
        save_env_token(args.env_file, token)
        print(f"Готово. Токен сохранён в {args.env_file.resolve()}")
        print("Теперь запустите: .\\.venv\\Scripts\\python.exe -m alice_client chat")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Вход отменён. Файл .env не изменён.", file=sys.stderr)
        return 130
    except (AuthError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (AuthError, ValueError)) else type(error).__name__
        print(f"Не удалось завершить вход: {message}", file=sys.stderr)
        return 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
