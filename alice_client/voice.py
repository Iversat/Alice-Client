"""Local speech recognition and an explicit wake-word gate before Alice requests."""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import aclosing
from dataclasses import dataclass

from .audio import input_chunks, play_audio
from .client import YandexAliceClient, YandexAliceClientError
from .local_asr import LocalRecognizer


@dataclass
class WakeGate:
    word: str | tuple[str, ...] = ("алиса", "alice")
    window: float = 10
    until: float = 0

    def consume(self, text: str, now: float) -> str | None:
        """None = ignore; empty = wake-only; otherwise a command after activation."""
        text = text.strip()
        words = (self.word,) if isinstance(self.word, str) else self.word
        pattern = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
        match = re.match(r"^(?:(?:эй|hey)\s+)?(?:" + pattern + r")\b[\s,.:!?—–-]*", text, re.I)
        if match:
            self.until = now + self.window
            return text[match.end() :].strip()
        if now < self.until:
            return text.strip()
        return None


async def recognize(recognizer, operation, *args):
    # Inference must not block WebSocket reception or the microphone callback.
    job = asyncio.create_task(asyncio.to_thread(getattr(recognizer, operation), *args))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        # Finish the in-flight decoder before its caller releases the model.
        await job
        raise


async def run_local_asr(args, token=None) -> None:
    recognizer = LocalRecognizer(args.lang, args.model)
    print(f"Локальный ASR ({args.lang}); Ctrl+C — остановка.", file=sys.stderr, flush=True)
    last_partial = ""
    async with aclosing(input_chunks(args)) as source:
        async for chunk in source:
            text = await recognize(recognizer, "accept", chunk)
            if text:
                print(text, flush=True)
            elif args.partial:
                partial = recognizer.partial()
                if partial and partial != last_partial:
                    print(f"… {partial}", file=sys.stderr, flush=True)
                    last_partial = partial
    text = await recognize(recognizer, "finish")
    if text:
        print(text, flush=True)


async def run_voice(args, token) -> None:
    from .cli import response_text

    recognizer = LocalRecognizer(args.lang, args.model)
    words = (args.wake_word,) if isinstance(args.wake_word, str) else tuple(args.wake_word)
    gate = WakeGate(words, args.command_timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.duration if args.duration else float("inf")
    turns = 0
    audio_time = 0.0
    client = YandexAliceClient(args.server, token, request_timeout=args.timeout)
    print(
        f"Локальный ASR ({args.lang}). Скажите «{' / '.join(words)}» и запрос. "
        "Микрофон закрывается на время ответа. Ctrl+C — выход.",
        flush=True,
    )

    async def dispatch(command):
        nonlocal turns
        if command.casefold().strip(" .!?") in {"выход", "заверши разговор", "quit", "exit"}:
            return True
        print(f"Вы › {command}", flush=True)
        if not client.connected:
            await client.connect()
            await client.synchronize_state()
        result = await client.send_text(command)
        answer = response_text(result["response_payload"], require_text=True)
        print(f"Алиса › {answer}", flush=True)
        try:
            audio = await client.tts(answer)
            if args.output:
                args.output.mkdir(parents=True, exist_ok=True)
                (args.output / f"reply-{turns + 1:03d}.opus").write_bytes(audio)
            if not args.no_play:
                await play_audio(audio, args.output_device)
        except (YandexAliceClientError, OSError, TimeoutError) as error:
            # The text turn has already completed; never resend a user's command.
            print(
                f"Озвучивание недоступно ({type(error).__name__}); ответ показан текстом.",
                file=sys.stderr,
                flush=True,
            )
        turns += 1
        gate.until = (audio_time if args.file else loop.time()) + args.follow_up
        return args.once

    try:
        if args.file:
            # Same recognizer and gate, with reproducible file input for diagnostics.
            async with aclosing(input_chunks(args)) as source:
                async for chunk in source:
                    audio_time += len(chunk) / 32000  # PCM16 mono at 16 kHz.
                    text = await recognize(recognizer, "accept", chunk)
                    if text:
                        command = gate.consume(text, audio_time)
                        if command and await dispatch(command):
                            return
                command = gate.consume(await recognize(recognizer, "finish"), audio_time)
                if command:
                    await dispatch(command)
            return
        while loop.time() < deadline:
            # A new capture stream per turn discards the assistant's own playback.
            remaining = deadline - loop.time()
            args.duration = remaining if remaining != float("inf") else None
            command = None
            async with aclosing(input_chunks(args)) as source:
                async for chunk in source:
                    text = await recognize(recognizer, "accept", chunk)
                    if text:
                        command = gate.consume(text, loop.time())
                        if command is not None:
                            if command:
                                break
                            print("Слушаю запрос…", flush=True)
                if not command:
                    command = gate.consume(await recognize(recognizer, "finish"), loop.time())
            if command:
                if await dispatch(command):
                    return
            recognizer.reset()
    finally:
        await client.close()
