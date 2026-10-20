"""Streaming recognition using the ASR messages found in the original example."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .client import DEFAULT_SERVER_URL, AliceTimeoutError, YandexAliceClient, YandexAliceClientError

RATE = 16000
CHANNELS = 1
FRAMES_PER_BUFFER = 1024  # 64 ms; 2048 bytes of mono PCM16 at 16 kHz.
YandexASRClientError = YandexAliceClientError


class YandexASRClient(YandexAliceClient):
    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        oauth_token: str | None = None,
        asr_key: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        request_timeout: float = 10,
        language: str = "ru-RU",
    ) -> None:
        super().__init__(server_url, oauth_token, request_timeout=request_timeout)
        self.asr_key = asr_key
        if language not in {"ru-RU", "en-US"}:
            raise ValueError("ASR поддерживает выбор ru-RU или en-US.")
        self.language = language
        self.partial_result_callback: Callable[[str, str], None] | None = None
        self.final_result_callback: Callable[[str, str], None] | None = None
        self._current_asr_message_id: str | None = None
        self._current_stream_id_counter = 0
        self._utterance_in_progress = False
        self._asr_done = asyncio.Event()
        self._asr_error: YandexAliceClientError | None = None
        self._end_sent = False
        self._running = False

    async def _receive_loop(self) -> None:
        try:
            await super()._receive_loop()
        finally:
            if self._utterance_in_progress and self._asr_error is None:
                self._asr_error = YandexASRClientError("Соединение закрыто во время распознавания.")
            self._asr_done.set()

    async def _on_directive(self, directive: dict[str, Any]) -> None:
        header = directive.get("header")
        if not isinstance(header, dict):
            return
        if header.get("namespace") != "ASR" or header.get("name") != "Result":
            await super()._on_directive(directive)
            return
        message_id = header.get("refMessageId")
        if message_id != self._current_asr_message_id or not self._utterance_in_progress:
            return
        payload = directive.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("responseCode") != "OK":
            self._asr_error = YandexASRClientError("Сервис ASR отклонил запрос.")
            self._asr_done.set()
            return
        text = ""
        recognition = payload.get("recognition")
        if isinstance(recognition, list) and recognition and isinstance(recognition[0], dict):
            variant = recognition[0]
            normalized = variant.get("normalized")
            words = variant.get("words", [])
            if isinstance(normalized, str) and normalized:
                text = normalized
            elif isinstance(words, list):
                text = " ".join(
                    word["value"]
                    for word in words
                    if isinstance(word, dict) and isinstance(word.get("value"), str)
                )
        if payload.get("endOfUtt") is True:
            await self._send_stream_control(message_id, self._current_stream_id_counter)
            self._utterance_in_progress = False
            self._asr_done.set()
            if self.final_result_callback is not None:
                self.final_result_callback(text, message_id)
        elif self.partial_result_callback is not None:
            self.partial_result_callback(text, message_id)

    async def _send_stream_control(self, message_id: str, stream_id: int) -> None:
        if self._end_sent:
            return
        self._end_sent = True
        await self._send_raw(
            json.dumps(
                {
                    "streamcontrol": {
                        "streamId": stream_id,
                        "action": 0,
                        "reason": 0,
                        "messageId": message_id,
                    }
                }
            )
        )

    async def run_realtime_asr(self, audio_queue: asyncio.Queue[bytes | None]) -> None:
        """Consume PCM16 mono chunks; None ends input and waits for the final result."""
        if not self.oauth_token or not self.asr_key:
            raise ValueError("Для ASR задайте YANDEX_OAUTH_TOKEN и YANDEX_ASR_KEY.")
        if self._running:
            raise YandexASRClientError("Распознавание уже запущено в этом клиенте.")
        self._running = True
        carry_chunk = None
        try:
            if not self.connected:
                await self.connect()
            await self.synchronize_state(lang=self.language)
            while self.connected:
                # Wait for real audio before opening the next utterance. EOF must
                # not create an empty ASR request after an already final result.
                if carry_chunk is None:
                    try:
                        first_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                    except TimeoutError:
                        continue
                    try:
                        if first_chunk is None:
                            return
                        if not isinstance(first_chunk, bytes) or len(first_chunk) % 2:
                            raise ValueError("Ожидаются байты PCM16 с чётной длиной.")
                        carry_chunk = first_chunk
                    finally:
                        audio_queue.task_done()
                self._current_asr_message_id = str(uuid.uuid4())
                self._current_stream_id_counter = self._current_stream_id_counter % 0xFFFFFFFF + 1
                message_id, stream_id = (
                    self._current_asr_message_id,
                    self._current_stream_id_counter,
                )
                self._utterance_in_progress = True
                self._asr_error = None
                self._asr_done.clear()
                self._end_sent = False
                await self._send_event(
                    "ASR",
                    "Recognize",
                    {
                        "asr": {
                            "lang": self.language,
                            "topic": "general",
                            "application": "python_realtime_mic_client",
                            "format": f"audio/x-pcm;bit=16;rate={RATE}",
                            "key": self.asr_key,
                            "advancedASROptions": {"partial_results": True, "capitalize": False},
                        }
                    },
                    message_id=message_id,
                    stream_id=stream_id,
                )
                while self.connected and not self._asr_done.is_set():
                    from_queue = carry_chunk is None
                    if from_queue:
                        try:
                            chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                        except TimeoutError:
                            continue
                    else:
                        chunk, carry_chunk = carry_chunk, None
                    try:
                        if chunk is None:
                            await self._send_stream_control(message_id, stream_id)
                            try:
                                await asyncio.wait_for(self._asr_done.wait(), self.request_timeout)
                            except TimeoutError:
                                raise AliceTimeoutError(
                                    "ASR не прислал финальный результат."
                                ) from None
                            if self._asr_error is not None:
                                raise self._asr_error
                            return
                        if not isinstance(chunk, bytes) or len(chunk) % 2:
                            raise ValueError("Ожидаются байты PCM16 с чётной длиной.")
                        # A final result may have arrived while get() was suspended.
                        # Preserve the next chunk for the next utterance.
                        if self._asr_done.is_set():
                            carry_chunk = chunk
                            break
                        await self._send_raw(stream_id.to_bytes(4, "big") + chunk)
                    finally:
                        if from_queue:
                            audio_queue.task_done()
                if self._asr_error is not None:
                    raise self._asr_error
            raise YandexASRClientError("Соединение ASR закрыто.")
        finally:
            if self.connected and self._utterance_in_progress and self._current_asr_message_id:
                with suppress(YandexAliceClientError):
                    await self._send_stream_control(
                        self._current_asr_message_id, self._current_stream_id_counter
                    )
            self._utterance_in_progress = False
            self._current_asr_message_id = None
            self._running = False


async def microphone_worker(
    audio_queue: asyncio.Queue[bytes | None], stop_event: asyncio.Event
) -> None:
    """Load PyAudio only when recording is explicitly requested."""
    try:
        import pyaudio
    except ImportError:
        raise YandexASRClientError(
            "Установите поддержку микрофона: pip install '.[audio]'."
        ) from None
    audio = pyaudio.PyAudio()
    stream = None
    operation = None
    try:
        operation = asyncio.create_task(
            asyncio.to_thread(
                audio.open,
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=FRAMES_PER_BUFFER,
            )
        )
        try:
            stream = await asyncio.shield(operation)
        except asyncio.CancelledError:
            stream = await operation
            raise
        while not stop_event.is_set():
            operation = asyncio.create_task(
                asyncio.to_thread(
                    stream.read,
                    FRAMES_PER_BUFFER,
                    exception_on_overflow=False,
                )
            )
            try:
                chunk = await asyncio.shield(operation)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await operation
                raise
            await audio_queue.put(chunk)
    finally:
        try:
            if stream is not None:
                try:
                    await asyncio.to_thread(stream.stop_stream)
                finally:
                    await asyncio.to_thread(stream.close)
        finally:
            await asyncio.to_thread(audio.terminate)
