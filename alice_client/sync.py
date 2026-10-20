"""Small synchronous adapter for callers of the original terminal example."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from .client import DEFAULT_SERVER_URL, AliceTimeoutError, YandexAliceClient


class YandexAliceClientPython:
    def __init__(self, server_url: str | None = None, oauth_token: str | None = None):
        self._client = YandexAliceClient(server_url or DEFAULT_SERVER_URL, oauth_token)
        self.server_url = self._client.server_url
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="alice-sync", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _call(self, coroutine: Coroutine) -> Any:
        if not self._thread.is_alive():
            coroutine.close()
            raise RuntimeError("Клиент уже закрыт.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self._client.request_timeout + 5)
        except FutureTimeout:
            future.cancel()
            raise AliceTimeoutError("Истекло время ожидания синхронного вызова.") from None

    def connect(self) -> None:
        self._call(self._client.connect())

    def set_oauth_token(self, token: str | None) -> None:
        async def set_token() -> None:
            self._client.set_oauth_token(token)

        self._call(set_token())

    def synchronize_state(self, lang: str = "ru-RU", voice: str = "levitan") -> str:
        return self._call(self._client.synchronize_state(lang, voice))

    def send_text(self, text: str, is_tts: bool = False) -> dict[str, Any]:
        result = self._call(self._client.send_text(text, is_tts))
        return {"response": result["response_payload"], "audio": result["audio"]}

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._call(self._client.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    def __enter__(self) -> YandexAliceClientPython:
        try:
            self.connect()
            self.synchronize_state()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
