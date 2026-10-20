"""Local speech recognition and an explicit wake-word gate before Alice requests."""

from __future__ import annotations

import asyncio
import re
import sys
from contextlib import aclosing
from dataclasses import dataclass

from .audio import input_chunks, play_audio
from .client import YandexAliceClient
from .local_asr import LocalRecognizer


@dataclass
class WakeGate:
    word: str = "алиса"
    window: float = 10
    until: float = 0

    def consume(self, text: str, now: float) -> str | None:
        """None = ignore; empty = wake-only; otherwise a command after activation."""
        match = re.match(r"^(?:эй\s+)?" + re.escape(self.word) + r"\b[\s,.:!?-]*", text, re.I)
        if match:
            self.until = now + self.window
            return text[match.end() :].strip()
        if now < self.until:
            return text.strip()
        return None


async def run_local_asr(args, token=None) -> None:
    recognizer = LocalRecognizer(args.lang, args.model)
    print(f"Локальный ASR ({args.lang}); Ctrl+C — остановка.", file=sys.stderr, flush=True)
    last_partial = ""
    async with aclosing(input_chunks(args)) as source:
        async for chunk in source:
            text = recognizer.accept(chunk)
            if text:
                print(text, flush=True)
            elif args.partial:
                partial = recognizer.partial()
                if partial and partial != last_partial:
                    print(f"… {partial}", file=sys.stderr, flush=True)
                    last_partial = partial
    text = recognizer.finish()
    if text:
        print(text, flush=True)


async def run_voice(args, token) -> None:
    from .cli import response_text

    recognizer = LocalRecognizer(args.lang, args.model)
    gate = WakeGate(args.wake_word.casefold(), args.command_timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.duration if args.duration else float("inf")
    turns = 0
    client = YandexAliceClient(args.server, token, request_timeout=args.timeout)
    print(
        f"Локальный ASR ({args.lang}). Скажите «{args.wake_word}» и запрос. "
        "Микрофон закрывается на время ответа. Ctrl+C — выход.",
        flush=True,
    )

    async def dispatch(command):
        nonlocal turns
        if command.casefold() in {"выход", "заверши разговор", "quit", "exit"}:
            return True
        print(f"Вы › {command}", flush=True)
        if not client.connected:
            await client.connect()
            await client.synchronize_state()
        result = await client.send_text(command)
        answer = response_text(result["response_payload"], require_text=True)
        print(f"Алиса › {answer}", flush=True)
        audio = await client.tts(answer)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / f"reply-{turns + 1:03d}.opus").write_bytes(audio)
        if not args.no_play:
            await play_audio(audio, args.output_device)
        turns += 1
        gate.until = loop.time() + args.follow_up
        return args.once

    try:
        if args.file:
            # Same recognizer and gate, with reproducible file input for diagnostics.
            async with aclosing(input_chunks(args)) as source:
                async for chunk in source:
                    text = recognizer.accept(chunk)
                    if text:
                        command = gate.consume(text, loop.time())
                        if command and await dispatch(command):
                            return
                command = gate.consume(recognizer.finish(), loop.time())
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
                    text = recognizer.accept(chunk)
                    if text:
                        command = gate.consume(text, loop.time())
                        if command is not None:
                            if command:
                                break
                            print("Слушаю запрос…", flush=True)
                if not command:
                    command = gate.consume(recognizer.finish(), loop.time())
            if command:
                if await dispatch(command):
                    return
            recognizer.reset()
    finally:
        await client.close()
