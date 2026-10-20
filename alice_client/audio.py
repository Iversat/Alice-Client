"""Optional audio I/O. Microphone audio stays in memory unless explicitly saved."""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from .client import YandexAliceClientError, positive_timeout

RATE = 16000
BLOCK = 800


def dependencies():
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
        import soxr
    except ImportError:
        raise YandexAliceClientError(
            'Установите голосовой режим: .\\.venv\\Scripts\\python.exe -m pip install ".[voice]"'
        ) from None
    return np, sd, sf, soxr


def devices() -> str:
    return str(dependencies()[1].query_devices())


def output_device(sd, requested=None):
    """Use the same Windows endpoint through WASAPI, preserving the user's routing."""
    info = sd.query_devices(requested, "output")
    if requested is None and sys.platform == "win32":
        apis = sd.query_hostapis()
        for candidate in sd.query_devices():
            if (
                candidate["name"] == info["name"]
                and candidate["max_output_channels"] > 0
                and apis[candidate["hostapi"]]["name"] == "Windows WASAPI"
            ):
                return candidate["index"], candidate
    return requested, info


def file_chunks(path: Path) -> Iterator[bytes]:
    """Decode supported files and stream PCM16 mono/16 kHz, including the resampler tail."""
    np, _, sf, soxr = dependencies()
    try:
        with sf.SoundFile(path) as source:
            if source.channels > 32 or source.samplerate > 384000:
                raise ValueError("Неподдерживаемое число каналов или частота аудио.")
            resampler = soxr.ResampleStream(source.samplerate, RATE, 1, dtype="float32")
            while True:
                data = source.read(8192, dtype="float32", always_2d=True)
                last = source.tell() == source.frames
                pcm = resampler.resample_chunk(data.mean(axis=1), last=last)
                raw = (np.clip(pcm, -1, 32767 / 32768) * 32768).astype("<i2").tobytes()
                for pos in range(0, len(raw), BLOCK * 2):
                    yield raw[pos : pos + BLOCK * 2]
                if last:
                    break
    except (sf.LibsndfileError, RuntimeError) as error:
        raise YandexAliceClientError(
            f"Не удалось прочитать аудиофайл ({type(error).__name__})."
        ) from None


async def microphone_chunks(
    duration: float | None = None, device: int | str | None = None
) -> AsyncIterator[bytes]:
    """50 ms blocks with bounded buffering; overflow is an explicit error."""
    _, sd, _, _ = dependencies()
    if duration is not None:
        duration = positive_timeout(duration)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
    failure = None
    accepting = True

    def deliver(data, status):
        nonlocal failure
        if not accepting:
            return
        if status or queue.full():
            failure = YandexAliceClientError("Переполнение микрофона; выберите другое устройство.")
        else:
            queue.put_nowait(data)

    def callback(data, frames, timing, status):
        loop.call_soon_threadsafe(deliver, bytes(data), bool(status))

    stream = None
    try:
        stream = sd.RawInputStream(
            samplerate=RATE,
            blocksize=BLOCK,
            device=device,
            channels=1,
            dtype="int16",
            callback=callback,
        )
        stream.start()
        deadline = loop.time() + duration if duration else float("inf")
        while loop.time() < deadline:
            if failure:
                raise failure
            try:
                yield await asyncio.wait_for(queue.get(), min(0.25, deadline - loop.time()))
            except TimeoutError:
                continue
    except sd.PortAudioError:
        raise YandexAliceClientError(
            "Не удалось открыть микрофон. Проверьте разрешения Windows и --device; список: devices."
        ) from None
    finally:
        accepting = False
        if stream is not None:
            stream.abort()
            stream.close()


async def input_chunks(args) -> AsyncIterator[bytes]:
    if args.file:
        # File iteration is bounded per block, yielding to WebSocket reception.
        for chunk in file_chunks(args.file):
            yield chunk
            await asyncio.sleep(0)
    else:
        source = microphone_chunks(args.duration, args.device)
        try:
            async for chunk in source:
                yield chunk
        finally:
            await source.aclose()


async def play_audio(audio: bytes, device: int | str | None = None) -> None:
    _, sd, sf, soxr = dependencies()
    stream = None
    try:
        data, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
        # Bluetooth/WASAPI devices may reject the server's 48 kHz. Convert to
        # their native shared-mode rate instead of relying on driver conversion.
        device, output_info = output_device(sd, device)
        output_rate = int(output_info["default_samplerate"])
        if rate != output_rate:
            data = soxr.resample(data, rate, output_rate)
            rate = output_rate
        if data.shape[1] > output_info["max_output_channels"]:
            data = data.mean(axis=1, keepdims=True)
        # Callback output avoids blocking the event loop and permits Ctrl+C.
        position = 0
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def callback(output, frames, timing, status):
            nonlocal position
            part = data[position : position + frames]
            output.fill(0)
            output[: len(part)] = part
            position += len(part)
            if position >= len(data):
                raise sd.CallbackStop

        def finished():
            loop.call_soon_threadsafe(done.set)

        stream = sd.OutputStream(
            samplerate=rate,
            channels=data.shape[1],
            device=device,
            callback=callback,
            finished_callback=finished,
        )
        stream.start()
        await asyncio.wait_for(done.wait(), len(data) / rate + 10)
    except (sd.PortAudioError, sf.LibsndfileError):
        raise YandexAliceClientError(
            "Не удалось воспроизвести аудио; проверьте устройство вывода."
        ) from None
    finally:
        if stream is not None:
            stream.abort()
            stream.close()
