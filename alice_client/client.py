"""Async transport shared by text chat, speech synthesis and recognition."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

DEFAULT_SERVER_URL = "wss://uniproxy.alice.ya.ru/uni.ws"
LOGGER = logging.getLogger(__name__)


class YandexAliceClientError(Exception):
    """Connection, server or protocol failure; messages never contain tokens."""


class AliceTimeoutError(YandexAliceClientError, TimeoutError):
    """A request did not finish before its deadline."""


@dataclass
class _Pending:
    needs: set[str]
    future: asyncio.Future
    directives: list[dict[str, Any]] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    audio_size: int = 0
    audio_chunks: int = 0


@dataclass
class _Stream:
    message_id: str
    buffers: bytearray = field(default_factory=bytearray)


def positive_timeout(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Таймаут должен быть положительным конечным числом.")
    return value


class YandexAliceClient:
    """Use from one event loop. Concurrent requests are matched by messageId.

    This implements an observed, undocumented protocol. A working transport
    does not imply service access, a subscription, or support from Yandex.
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        oauth_token: str | None = None,
        *,
        request_timeout: float = 10,
        max_audio_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(server_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("Ожидается WebSocket URL с ws:// или wss://.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("URL не должен содержать учётные данные или fragment.")
        if parsed.scheme == "ws" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Незашифрованный ws:// разрешён только для локальных тестов.")
        if not isinstance(max_audio_bytes, int) or max_audio_bytes <= 0:
            raise ValueError("Лимит аудио должен быть положительным целым числом.")
        self.server_url = server_url
        self.oauth_token = oauth_token.strip() if oauth_token else None
        self.request_timeout = positive_timeout(request_timeout)
        self.max_audio_bytes = max_audio_bytes
        self.device_uuid = str(uuid.uuid4())
        self.client_app_uuid = str(uuid.uuid4())
        self._ws: ClientConnection | None = None
        self._is_connected = False
        self._active_requests: dict[str, _Pending] = {}
        self._active_streams: dict[int, _Stream] = {}
        self._receive_loop_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._dialogue_lock = asyncio.Lock()
        self._session_id: str | None = None

    @property
    def connected(self) -> bool:
        return self._is_connected and self._ws is not None

    def set_oauth_token(self, token: str | None) -> None:
        self.reset_session()
        self.oauth_token = token.strip() if token else None

    def reset_session(self) -> None:
        """Start a new dialogue on the next request; never store server state on disk."""
        self._session_id = None

    async def __aenter__(self) -> YandexAliceClient:
        await self.connect()
        try:
            await self.synchronize_state()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if self.connected:
                return
            # Finish a previous receiver before reusing this instance.
            await self._close()
            try:
                self._ws = await connect(
                    self.server_url,
                    open_timeout=self.request_timeout,
                    close_timeout=3,
                    ping_interval=25,
                    ping_timeout=15,
                    max_size=2 * 1024 * 1024,
                )
            except Exception as error:
                raise YandexAliceClientError(
                    f"Не удалось открыть WebSocket ({type(error).__name__})."
                ) from None
            self._is_connected = True
            self._receive_loop_task = asyncio.create_task(
                self._receive_loop(), name="alice-receiver"
            )

    async def _receive_loop(self) -> None:
        ws = self._ws
        try:
            if ws is not None:
                async for message in ws:
                    await self._on_message(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception as error:
            LOGGER.warning("Receiver stopped (%s)", type(error).__name__)
        finally:
            self._is_connected = False
            self._fail_pending("Соединение закрыто до завершения запроса.")
            self._active_streams.clear()

    def _fail_pending(self, message: str) -> None:
        for request in self._active_requests.values():
            self._reject(request, message)

    @staticmethod
    def _reject(request: _Pending, message: str) -> None:
        if not request.future.done():
            request.future.set_exception(YandexAliceClientError(message))

    async def _send_event(
        self,
        namespace: str,
        name: str,
        payload: dict[str, Any],
        *,
        message_id: str | None = None,
        stream_id: int | None = None,
    ) -> str:
        if not self.connected:
            raise YandexAliceClientError("WebSocket не подключён.")
        message_id = message_id or str(uuid.uuid4())
        header = {"namespace": namespace, "name": name, "messageId": message_id}
        if stream_id is not None:
            header["streamId"] = stream_id
        await self._send_raw(json.dumps({"event": {"header": header, "payload": payload}}))
        return message_id

    async def _send_raw(self, message: str | bytes) -> None:
        if not self.connected or self._ws is None:
            raise YandexAliceClientError("WebSocket не подключён.")
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._ws.send(message)
        except Exception as error:
            self._is_connected = False
            self._fail_pending("Соединение потеряно при отправке.")
            raise YandexAliceClientError(f"Ошибка отправки ({type(error).__name__}).") from None

    async def _request(
        self, namespace: str, name: str, payload: dict[str, Any], needs: set[str]
    ) -> dict[str, Any]:
        if not self.connected:
            raise YandexAliceClientError("WebSocket не подключён.")
        if len(self._active_requests) >= 32:
            raise YandexAliceClientError("Слишком много одновременных запросов (максимум 32).")
        message_id = str(uuid.uuid4())
        pending = _Pending(set(needs), asyncio.get_running_loop().create_future())
        # The receiver must find the request even if send() immediately yields a reply.
        self._active_requests[message_id] = pending
        try:
            async with asyncio.timeout(self.request_timeout):
                await self._send_event(namespace, name, payload, message_id=message_id)
                return await pending.future
        except TimeoutError:
            raise AliceTimeoutError("Истекло время ожидания ответа сервера.") from None
        finally:
            self._active_requests.pop(message_id, None)
            for stream_id, stream in list(self._active_streams.items()):
                if stream.message_id == message_id:
                    self._active_streams.pop(stream_id, None)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()  # Retrieve an error if send failed before the await.

    async def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            self._on_binary_audio(message)
            return
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("directive"), dict):
            await self._on_directive(data["directive"])
        if isinstance(data.get("streamcontrol"), dict):
            self._on_stream_control(data["streamcontrol"])
        if "error" in data:
            self._fail_pending("Сервер вернул ошибку протокола.")

    async def _on_directive(self, directive: dict[str, Any]) -> None:
        header = directive.get("header")
        if not isinstance(header, dict):
            return
        message_id = header.get("refMessageId")
        if not isinstance(message_id, str):
            return
        request = self._active_requests.get(message_id)
        if request is None or request.future.done():
            return
        name = header.get("name")
        if not isinstance(name, str):
            return
        if name in {"Error", "Failure"}:
            self._reject(request, "Сервер отклонил запрос.")
            return
        if len(request.directives) >= 512:
            self._reject(request, "Превышен лимит директив ответа.")
            return
        request.directives.append(directive)
        request.needs.discard(name)
        if name == "Speak" and "audio" in request.needs:
            stream_id = header.get("streamId")
            if type(stream_id) is not int or not 0 <= stream_id <= 0xFFFFFFFF:
                self._reject(request, "Некорректный идентификатор аудиопотока.")
                return
            previous = self._active_streams.get(stream_id)
            if previous is not None and previous.message_id != message_id:
                self._reject(request, "Идентификатор аудиопотока уже занят другим запросом.")
                return
            self._active_streams.setdefault(stream_id, _Stream(message_id))
        self._complete(request)

    def _on_binary_audio(self, data: bytes) -> None:
        if len(data) <= 4:
            return
        stream = self._active_streams.get(int.from_bytes(data[:4], "big"))
        if stream is None:
            return
        request = self._active_requests.get(stream.message_id)
        if request is None or request.future.done():
            return
        chunk = data[4:]
        request.audio_size += len(chunk)
        request.audio_chunks += 1
        if request.audio_size > self.max_audio_bytes or request.audio_chunks > 65536:
            self._reject(request, "Превышен лимит размера аудиоответа.")
            return
        stream.buffers.extend(chunk)

    def _on_stream_control(self, control: dict[str, Any]) -> None:
        stream_id = control.get("streamId")
        if type(stream_id) is not int or control.get("action", 0) != 0:
            return
        stream = self._active_streams.pop(stream_id, None)
        if stream is None:
            return
        request = self._active_requests.get(stream.message_id)
        if request is None or request.future.done():
            return
        if control.get("reason", 0) != 0:
            self._reject(request, "Сервер прервал аудиопоток.")
            return
        request.audio.extend(stream.buffers)
        if not any(s.message_id == stream.message_id for s in self._active_streams.values()):
            request.needs.discard("audio")
        self._complete(request)

    @staticmethod
    def _complete(request: _Pending) -> None:
        if not request.needs and not request.future.done():
            request.future.set_result(
                {"directives": request.directives, "audio": bytes(request.audio) or None}
            )

    async def synchronize_state(self, lang: str = "ru-RU", voice: str = "levitan") -> str:
        """Send client state. This does not confirm successful authentication."""
        payload = {
            "uuid": self.device_uuid,
            "lang": lang,
            "voice": voice,
            "clientTime": datetime.now(UTC).isoformat(),
            "timezone": "Europe/Moscow",
        }
        if self.oauth_token:
            payload["auth_token"] = self.oauth_token
        return await self._send_event("System", "SynchronizeState", payload)

    def _get_application_payload(self) -> dict[str, str]:
        return {
            "app_id": "aliced_python_dialog_tts",
            "app_version": "0.4.3",
            "os_version": "CLI",
            "platform": "python_async_client",
            "uuid": self.client_app_uuid,
            "lang": "ru-RU",
            "client_time": datetime.now(UTC).isoformat(),
            "timezone": "Europe/Moscow",
            "timestamp": str(int(time.time())),
        }

    async def send_text(self, text: str, is_tts_response: bool = False) -> dict[str, Any]:
        # Stateful turns must be ordered. TTS requests remain independent.
        async with self._dialogue_lock:
            return await self._send_text(text, is_tts_response)

    async def _send_text(self, text: str, is_tts_response: bool) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Текст запроса не может быть пустым.")
        payload = {
            "request": {
                "voice_session": is_tts_response,
                "event": {"type": "text_input", "text": text},
            },
            "application": self._get_application_payload(),
        }
        if self.oauth_token:
            payload["request"]["additional_options"] = {"oauth_token": self.oauth_token}
        if self._session_id:
            payload["header"] = {"session_id": self._session_id}
        result = await self._request(
            "Vins",
            "TextInput",
            payload,
            {"VinsResponse", "audio"} if is_tts_response else {"VinsResponse"},
        )
        response = {}
        for directive in result["directives"]:
            if directive.get("header", {}).get("name") == "VinsResponse":
                body = directive.get("payload")
                value = body.get("response") if isinstance(body, dict) else None
                if not isinstance(value, dict):
                    raise YandexAliceClientError("Некорректный формат VinsResponse.")
                header = body.get("header")
                session_id = header.get("session_id") if isinstance(header, dict) else None
                if isinstance(session_id, str) and 0 < len(session_id) <= 4096:
                    self._session_id = session_id
                response = value
                break
        return {
            "response_payload": response,
            "audio": result["audio"],
            "full_directives": result["directives"],
        }

    async def tts(
        self,
        text: str,
        voice: str = "shitova.us",
        lang: str = "ru-RU",
        format_audio: str = "audio/opus",
        emotion: str = "neutral",
        quality: str = "UltraHigh",
    ) -> bytes:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Текст для синтеза речи не может быть пустым.")
        payload = {
            "voice": voice,
            "lang": lang,
            "format": format_audio,
            "emotion": emotion,
            "quality": quality,
            "text": text,
        }
        if self.oauth_token:
            payload["auth_token"] = self.oauth_token
        result = await self._request("TTS", "Generate", payload, {"audio"})
        if not result["audio"]:
            raise YandexAliceClientError("Сервер завершил поток без аудиоданных.")
        return result["audio"]

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self) -> None:
        self._is_connected = False
        self._fail_pending("Клиент закрыт.")
        task, self._receive_loop_task = self._receive_loop_task, None
        ws, self._ws = self._ws, None
        try:
            if task is not None and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            try:
                if ws is not None:
                    with suppress(Exception):
                        await ws.close()
            finally:
                self._active_streams.clear()
