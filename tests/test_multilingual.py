import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alice_client import cli
from alice_client.multilingual_asr import MultilingualRecognizer
from alice_client.voice import WakeGate, recognize


@pytest.mark.parametrize(
    "text,command",
    [
        ("Алиса, привет!", "привет!"),
        ("Alice, hello!", "hello!"),
        ("Hey Alice what time is it?", "what time is it?"),
        ("эй алиса сколько времени", "сколько времени"),
    ],
)
def test_both_triggers_active_together(text, command):
    assert WakeGate().consume(text, 100) == command


@pytest.mark.parametrize(
    "text", ["aliceblue hello", "алисана привет", "I mentioned Alice", "лисица"]
)
def test_trigger_substrings_and_mentions_do_not_activate(text):
    assert WakeGate().consume(text, 100) is None


def test_repeated_custom_triggers_and_default_auto():
    args = cli.build_parser().parse_args(["voice", "--wake-word", "алиса", "--wake-word", "alice"])
    assert args.wake_word == ["алиса", "alice"]
    assert args.lang == "auto"


def bare_recognizer():
    r = object.__new__(MultilingualRecognizer)
    r.model = Mock()
    r.last_language = None
    r.reset()
    return r


def test_silence_is_bounded_and_never_transcribed():
    r = bare_recognizer()
    for _ in range(1000):
        assert r.accept(bytes(1600)) is None
    assert len(r.buffer) <= 12800
    assert r.finish() == ""
    r.model.detect_language.assert_not_called()


def test_shared_endpoint_emits_once_after_pause_and_flush_is_empty(monkeypatch):
    r = bare_recognizer()

    def finish():
        r.reset()
        return "Alice, hello!"

    mock = Mock(side_effect=finish)
    monkeypatch.setattr(r, "finish", mock)
    for _ in range(10):
        assert r.accept(b"\xe8\x03" * 800) is None
    results = [r.accept(bytes(1600)) for _ in range(30)]
    assert [x for x in results if x is not None] == ["Alice, hello!"]
    assert mock.call_count == 1


def test_auto_chooses_only_ru_en_and_resets_language_for_next_phrase():
    pytest.importorskip("numpy")
    r = bare_recognizer()
    r.model.detect_language.side_effect = [
        ("ja", 0.7, [("ja", 0.7), ("ru", 0.2), ("en", 0.1)]),
        ("en", 0.8, [("en", 0.8), ("ru", 0.1)]),
    ]
    r.model.transcribe.return_value = ([SimpleNamespace(text="sample")], None)
    for language in ["ru", "en"]:
        r.buffer = bytearray(b"\xe8\x03" * 3200)
        r.voiced = 3200
        assert r.finish() == "sample"
        assert r.last_language == language
        assert r.model.transcribe.call_args.kwargs["language"] == language
        assert not r.buffer


async def test_inference_runs_off_event_loop_and_cancellation_waits_for_cleanup():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work():
        entered.set()
        release.wait(2)
        finished.set()
        return "test"

    r = SimpleNamespace(finish=work)
    task = asyncio.create_task(recognize(r, "finish"))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_server_auto_fails_before_loading_account(monkeypatch):
    token = Mock(side_effect=AssertionError("must not read credentials"))
    monkeypatch.setattr(cli, "get_token", token)
    with pytest.raises(SystemExit):
        cli.main(["asr", "--backend", "yandex", "--lang", "auto"])
    token.assert_not_called()
