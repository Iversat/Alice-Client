"""Yandex OAuth device flow and native OS credential storage.

No browser cookies or account passwords are read. Tokens are never logged.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .client import YandexAliceClientError, positive_timeout

SERVICE = "Alice-Client/Yandex-OAuth"
ACCOUNT = "default"
OAUTH = "https://oauth.yandex.ru"
VERIFY_URL = OAUTH + "/device"


class AuthError(YandexAliceClientError):
    """Sanitized authentication error."""

    def __init__(self, code: str = "request_failed") -> None:
        known = {
            "authorization_pending",
            "slow_down",
            "access_denied",
            "expired_token",
            "invalid_grant",
            "invalid_client",
            "invalid_scope",
            "invalid_request",
            "unauthorized_client",
            "unsupported_grant_type",
            "bad_verification_code",
            "malformed_response",
            "request_failed",
            "http_401",
            "http_403",
        }
        self.code = code if code in known else "request_failed"
        super().__init__(f"Яндекс OAuth/API: {self.code}.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(
    url: str, *, data: dict | None = None, token: str | None = None, timeout: float = 15
) -> dict[str, Any]:
    """Only fixed Yandex endpoints; never forward credentials to a redirect."""
    if url not in {
        OAUTH + "/device/code",
        OAUTH + "/token",
        "https://login.yandex.ru/info?format=json",
        "https://api.iot.yandex.net/v1.0/user/info",
    }:
        raise ValueError("Неизвестный адрес API.")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urlencode(data).encode() if data is not None else None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=body, headers=headers)
    status = 200
    try:
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=positive_timeout(timeout))
        except HTTPError as error:
            response = error
            status = error.code
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise AuthError("malformed_response")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AuthError(f"http_{status}" if status != 200 else "malformed_response") from None
        if not isinstance(result, dict):
            raise AuthError("malformed_response")
        if result.get("error") or status != 200:
            raise AuthError(str(result.get("error") or f"http_{status}"))
        return result
    except (URLError, OSError, ValueError) as error:
        if isinstance(error, AuthError):
            raise
        raise AuthError() from None


@dataclass(repr=False)
class OAuthToken:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    scope: str | None = None
    client_id: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at

    @classmethod
    def from_response(cls, data: dict, client_id: str) -> OAuthToken:
        access = data.get("access_token")
        if not isinstance(access, str) or not access.strip():
            raise AuthError("malformed_response")
        if str(data.get("token_type", "bearer")).lower() != "bearer":
            raise AuthError("malformed_response")
        expires = data.get("expires_in")
        try:
            lifetime = positive_timeout(expires) if expires is not None else None
        except (ValueError, TypeError):
            raise AuthError("malformed_response") from None
        refresh = data.get("refresh_token")
        scope = data.get("scope")
        if refresh is not None and not isinstance(refresh, str):
            raise AuthError("malformed_response")
        if scope is not None and not isinstance(scope, str):
            raise AuthError("malformed_response")
        return cls(access, refresh, time.time() + lifetime if lifetime else None, scope, client_id)


class TokenStore:
    """One local profile, protected by Windows Vault/macOS Keychain/Linux keyring."""

    def __init__(self, backend=None) -> None:
        if backend is None:
            import keyring

            backend = keyring.get_keyring()
            if type(backend).__module__ not in {
                "keyring.backends.Windows",
                "keyring.backends.macOS",
                "keyring.backends.SecretService",
                "keyring.backends.kwallet",
            }:
                raise AuthError("request_failed")
        self.backend = backend

    def load(self) -> OAuthToken | None:
        try:
            raw = self.backend.get_password(SERVICE, ACCOUNT)
            if not raw:
                return None
            data = json.loads(raw)
            token = OAuthToken(**data)
            if not isinstance(token.access_token, str) or not token.access_token.strip():
                raise ValueError
            if token.expires_at is not None:
                if not isinstance(token.expires_at, (int, float)) or not math.isfinite(
                    token.expires_at
                ):
                    raise ValueError
            for value in (token.refresh_token, token.scope, token.client_id):
                if value is not None and not isinstance(value, str):
                    raise ValueError
            return token
        except Exception:
            raise AuthError("malformed_response") from None

    def save(self, token: OAuthToken) -> None:
        try:
            self.backend.set_password(SERVICE, ACCOUNT, json.dumps(vars(token)))
        except Exception:
            raise AuthError() from None

    def delete(self) -> None:
        try:
            if self.backend.get_password(SERVICE, ACCOUNT) is not None:
                self.backend.delete_password(SERVICE, ACCOUNT)
        except Exception:
            raise AuthError() from None


@dataclass(repr=False)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    interval: float
    deadline: float
    verification_url: str = VERIFY_URL


class YandexAuth:
    def __init__(self, client_id: str, client_secret: str | None = None, *, timeout: float = 15):
        if not client_id or not client_id.strip():
            raise ValueError(
                "Задайте ClientID своего приложения: --client-id или YANDEX_CLIENT_ID."
            )
        self.client_id = client_id.strip()
        self._client_secret = client_secret
        self.timeout = positive_timeout(timeout)

    def start(self, scope: str | None = None) -> DeviceCode:
        data = {"client_id": self.client_id}
        if scope:
            data["scope"] = scope
        result = request_json(OAUTH + "/device/code", data=data, timeout=self.timeout)
        try:
            if not all(
                isinstance(result.get(k), str) and result[k] for k in ("device_code", "user_code")
            ):
                raise ValueError
            if result.get("verification_url") != VERIFY_URL:
                raise ValueError
            interval = positive_timeout(result["interval"])
            expires = positive_timeout(result["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise AuthError("malformed_response") from None
        return DeviceCode(
            result["device_code"], result["user_code"], interval, time.monotonic() + expires
        )

    def wait(self, code: DeviceCode, *, timeout: float = 600, cancel=None) -> OAuthToken:
        deadline = min(code.deadline, time.monotonic() + positive_timeout(timeout))
        interval = code.interval
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= interval:
                raise AuthError("expired_token")
            if cancel is None:
                time.sleep(interval)
            elif cancel.wait(interval):
                raise AuthError("access_denied")
            data = {
                "grant_type": "device_code",
                "code": code.device_code,
                "client_id": self.client_id,
            }
            if self._client_secret:
                data["client_secret"] = self._client_secret
            try:
                response = request_json(
                    OAUTH + "/token",
                    data=data,
                    timeout=min(self.timeout, max(0.01, deadline - time.monotonic())),
                )
                return OAuthToken.from_response(response, self.client_id)
            except AuthError as error:
                if error.code == "slow_down":
                    interval += 5
                elif error.code != "authorization_pending":
                    raise

    def refresh(self, token: OAuthToken) -> OAuthToken:
        if not token.refresh_token:
            raise ValueError("Нет refresh-токена. Выполните auth login повторно.")
        if token.client_id != self.client_id:
            raise ValueError("ClientID не совпадает с приложением сохранённого токена.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": self.client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        result = OAuthToken.from_response(
            request_json(OAUTH + "/token", data=data, timeout=self.timeout), self.client_id
        )
        if not result.refresh_token:
            result.refresh_token = token.refresh_token
        return result


def get_token(*, anonymous: bool = False) -> str | None:
    """Environment, current-directory .env, then OS vault. Anonymous bypasses all."""
    if anonymous:
        return None
    token = os.getenv("YANDEX_OAUTH_TOKEN", "").strip()
    if token:
        return token
    from .env_auth import file_token

    saved_file = file_token()
    if saved_file:
        if saved_file.expired:
            raise ValueError("Токен в .env истёк: выполните новый вход через yandex_login.py.")
        return saved_file.access_token
    try:
        store = TokenStore()
    except AuthError:
        return None  # Headless systems can use environment or a hidden prompt.
    saved = store.load()
    if saved and saved.expired:
        raise ValueError("Сохранённый токен истёк: выполните auth refresh или auth login.")
    return saved.access_token if saved else None


def check_access(token: str) -> dict[str, Any]:
    """Read-only checks, returning no profile data or household identifiers."""
    summary: dict[str, Any] = {}
    for name, url in (
        ("identity", "https://login.yandex.ru/info?format=json"),
        ("smart_home", "https://api.iot.yandex.net/v1.0/user/info"),
    ):
        try:
            result = request_json(url, token=token)
            if name == "identity":
                summary[name] = {"ok": bool(result.get("id"))}
            else:
                summary[name] = {"ok": result.get("status") == "ok"}
                if summary[name]["ok"]:
                    summary[name]["devices"] = len(result.get("devices", []))
                    summary[name]["groups"] = len(result.get("groups", []))
                    summary[name]["scenarios"] = len(result.get("scenarios", []))
        except AuthError as error:
            summary[name] = {"ok": False, "error": error.code}
    return summary
