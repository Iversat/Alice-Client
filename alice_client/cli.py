"""Command-line entry points. Credentials are read without printing them."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import threading
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .asr import YandexASRClient, microphone_worker
from .auth import OAuthToken, TokenStore, YandexAuth, check_access, get_token
from .client import DEFAULT_SERVER_URL, YandexAliceClient, YandexAliceClientError, positive_timeout
from .env_auth import file_token, setting


def save_audio_to_file(audio_bytes: bytes | None, filename: str | Path = "response.opus") -> Path:
    if not audio_bytes:
        raise ValueError("Нет аудиоданных для сохранения.")
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio_bytes)
    return path


def response_text(payload: dict[str, Any], *, require_text: bool = False) -> str:
    if payload.get("error"):
        raise YandexAliceClientError("Сервис вернул ошибку в ответе.")
    card = payload.get("card")
    if isinstance(card, dict) and isinstance(card.get("text"), str) and card["text"]:
        return card["text"]
    text = payload.get("text")
    if isinstance(text, str) and text:
        return text
    if require_text:
        raise YandexAliceClientError("В ответе нет текста для озвучивания.")
    return "Ответ получен без текстового содержимого."


async def read_line(prompt: str) -> str:
    """A daemon reader keeps WebSocket reception active and does not block exit."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(value: str | None, error: BaseException | None) -> None:
        if not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

    def worker() -> None:
        try:
            value, error = input(prompt), None
        except (EOFError, KeyboardInterrupt) as caught:
            value, error = None, caught
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, value, error)

    threading.Thread(target=worker, name="alice-console", daemon=True).start()
    return await future


async def run_chat(args: argparse.Namespace, token: str | None) -> None:
    async with YandexAliceClient(args.server, token, request_timeout=args.timeout) as client:
        index = 0
        while True:
            if args.text is not None:
                text = args.text
            else:
                try:
                    text = (await read_line("Вы › ")).strip()
                except EOFError:
                    return
                if text.lower() in {"exit", "quit", "выход"}:
                    return
                if not text:
                    continue
            # In the live anonymous check voice_session=True returned text but
            # no audio stream. Request the text, then synthesize that answer.
            result = await client.send_text(text, is_tts_response=False)
            payload = result["response_payload"]
            text_output = response_text(payload, require_text=args.audio)
            print(
                json.dumps(payload, ensure_ascii=False) if args.json else f"Алиса › {text_output}"
            )
            if args.audio:
                audio = result["audio"] or await client.tts(text_output)
                index += 1
                output = args.output or Path("responses") / f"reply-{index:03d}.opus"
                path = save_audio_to_file(audio, output)
                print(f"Аудио: {path}", file=sys.stderr if args.json else sys.stdout)
            if args.text is not None:
                return


async def run_tts(args: argparse.Namespace, token: str | None) -> None:
    async with YandexAliceClient(args.server, token, request_timeout=args.timeout) as client:
        while True:
            if args.text is None:
                try:
                    text = (await read_line("Текст для озвучивания › ")).strip()
                except EOFError:
                    return
                if text.lower() in {"exit", "quit", "выход"}:
                    return
                if not text:
                    continue
            else:
                text = args.text
            audio = await client.tts(text, voice=args.voice)
            print(f"Аудио: {save_audio_to_file(audio, args.output)}")
            if args.text is not None:
                return


async def run_asr(args: argparse.Namespace, token: str | None) -> None:
    key = setting("YANDEX_ASR_KEY")
    if not token or not key:
        raise ValueError("Для ASR задайте YANDEX_OAUTH_TOKEN и YANDEX_ASR_KEY.")
    client = YandexASRClient(args.server, token, key, request_timeout=args.timeout)
    client.final_result_callback = lambda text, message_id: print(text, flush=True)
    if args.partial:
        client.partial_result_callback = lambda text, message_id: print(
            f"… {text}", file=sys.stderr
        )
    queue = asyncio.Queue(maxsize=32)
    stop = asyncio.Event()
    tasks = []
    try:
        await client.connect()
        mic = asyncio.create_task(microphone_worker(queue, stop), name="alice-microphone")
        recognition = asyncio.create_task(client.run_realtime_asr(queue), name="alice-asr")
        tasks.extend([mic, recognition])
        timer = None
        if args.duration is not None:
            timer = asyncio.create_task(asyncio.sleep(args.duration))
            tasks.append(timer)
        print("Распознавание. Для остановки нажмите Ctrl+C.", file=sys.stderr)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if recognition in done:
            await recognition
        elif mic in done:
            await mic
        elif timer in done:
            stop.set()
            mic.cancel()
            await asyncio.gather(mic, return_exceptions=True)
            if not recognition.done():
                await asyncio.wait_for(queue.put(None), args.timeout)
            await recognition
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Исследовательский WebSocket-клиент Алисы.")
    commands = parser.add_subparsers(dest="command", required=True)
    auth_parser = commands.add_parser("auth", help="вход через Яндекс и управление токеном")
    actions = auth_parser.add_subparsers(dest="auth_action", required=True)
    login = actions.add_parser("login", help="получить токен через официальный вход по коду")
    login.add_argument("--client-id")
    login.add_argument("--scope", help="права из настроек вашего OAuth-приложения")
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--wait", type=float, default=600)
    actions.add_parser("status", help="проверить наличие токена, не раскрывая его")
    actions.add_parser("check", help="проверить Яндекс ID и чтение умного дома")
    actions.add_parser("import", help="сохранить существующий токен через скрытый ввод")
    actions.add_parser("refresh", help="обновить сохранённый токен")
    actions.add_parser("logout", help="удалить локальный токен; не отзывает его у Яндекса")
    export = actions.add_parser("token", help="явно вывести токен для использования в своём коде")
    export.add_argument("--show", action="store_true", help="разрешить вывод секрета в stdout")
    chat = commands.add_parser("chat", help="текстовый чат и озвученный ответ")
    chat.add_argument("--text", help="один запрос вместо интерактивного чата")
    chat.add_argument("--audio", action="store_true", help="запросить аудиоответ")
    chat.add_argument("--output", type=Path, help="путь аудиофайла; иначе responses/reply-NNN.opus")
    chat.add_argument("--json", action="store_true", help="ответ сервера в JSON")
    tts = commands.add_parser("tts", help="запрос синтеза речи")
    tts.add_argument("text", nargs="?", help="текст для синтеза; иначе интерактивный режим")
    tts.add_argument("--voice", default="shitova.us", help="идентификатор голоса в протоколе")
    tts.add_argument("--output", type=Path, default=Path("response.opus"))
    asr = commands.add_parser("asr", help="распознавание с микрофона")
    asr.add_argument(
        "--duration", type=float, help="остановить запись через указанное число секунд"
    )
    asr.add_argument("--partial", action="store_true", help="промежуточные результаты в stderr")
    for command in (chat, tts, asr):
        command.add_argument("--server", default=DEFAULT_SERVER_URL, help="WebSocket URL")
        command.add_argument("--timeout", type=float, default=10, help="таймаут запроса, 10 с")
        auth = command.add_mutually_exclusive_group()
        auth.add_argument("--token-prompt", action="store_true", help="ввести токен скрыто")
        auth.add_argument("--anonymous", action="store_true", help="игнорировать все токены")
    return parser


def run_auth(args: argparse.Namespace) -> int:
    action = args.auth_action
    if action in {"check", "token"}:
        if action == "token" and not args.show:
            raise ValueError("Для вывода секрета нужен --show. В Python используйте get_token().")
        token = get_token()
        if not token:
            raise ValueError("Нет токена. Выполните auth login или auth import.")
        if action == "token":
            print(token)
            return 0
        result = check_access(token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(item["ok"] for item in result.values()) else 1
    store = TokenStore()
    if action == "login":
        wait = positive_timeout(args.wait)
        service = YandexAuth(
            args.client_id or setting("YANDEX_CLIENT_ID"), setting("YANDEX_CLIENT_SECRET")
        )
        code = service.start(args.scope)
        print(f"Откройте {code.verification_url}\nКод устройства: {code.user_code}", flush=True)
        print("Подтвердите доступ на сайте Яндекса. Ожидаю…", flush=True)
        if not args.no_browser:
            webbrowser.open(code.verification_url)
        token = service.wait(code, timeout=wait)
        store.save(token)
        print(
            "Токен сохранён в защищённом хранилище ОС. Клиент будет использовать его автоматически."
        )
    elif action == "import":
        token = getpass.getpass("OAuth-токен (ввод скрыт): ").strip()
        if not token:
            raise ValueError("Токен не может быть пустым.")
        store.save(OAuthToken(token))
        print("Токен сохранён. Проверка прав: python -m alice_client auth check")
    elif action == "logout":
        store.delete()
        print("Локальный токен удалён. Отзыв доступа: https://id.yandex.ru/security")
        if os.getenv("YANDEX_OAUTH_TOKEN"):
            print("YANDEX_OAUTH_TOKEN всё ещё задан в окружении и имеет приоритет.")
        if file_token():
            print("Токен в .env не удалён и продолжит использоваться клиентом.")
    elif action == "status":
        saved = store.load()
        saved_file = file_token()
        print(
            json.dumps(
                {
                    "environment_token": bool(os.getenv("YANDEX_OAUTH_TOKEN", "").strip()),
                    "stored_token": saved is not None,
                    "env_file_token": saved_file is not None,
                    "env_file_expired": saved_file.expired if saved_file else None,
                    "expired": saved.expired if saved else None,
                    "scope": saved.scope if saved else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif action == "refresh":
        saved = store.load()
        if not saved or not saved.client_id:
            raise ValueError("Нет токена, полученного через auth login.")
        service = YandexAuth(saved.client_id, setting("YANDEX_CLIENT_SECRET"))
        store.save(service.refresh(saved))
        print("Токен обновлён в защищённом хранилище ОС.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            return run_auth(args)
        args.timeout = positive_timeout(args.timeout)
        if args.command == "asr" and args.duration is not None:
            args.duration = positive_timeout(args.duration)
        if args.command in {"chat", "tts"} and args.text is not None and not args.text.strip():
            raise ValueError("Текст запроса не может быть пустым.")
        token = None
        if args.token_prompt:
            token = getpass.getpass("OAuth-токен (ввод скрыт): ").strip() or None
        else:
            token = get_token(anonymous=args.anonymous)
        runner = {"chat": run_chat, "tts": run_tts, "asr": run_asr}[args.command]
        asyncio.run(runner(args, token))
    except ValueError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("Остановлено.", file=sys.stderr)
        return 130
    except (YandexAliceClientError, OSError, TimeoutError) as error:
        # OS exceptions may include user-controlled paths; credentials are never attached.
        message = str(error) if isinstance(error, YandexAliceClientError) else type(error).__name__
        print(f"Ошибка: {message}", file=sys.stderr)
        return 1
    return 0
