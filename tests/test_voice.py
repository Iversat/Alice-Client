import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from alice_client import cli, voice
from alice_client.voice import WakeGate


def test_trigger_word_boundaries_and_activation_timeout():
    gate = WakeGate(window=10)
    assert gate.consume("сколько времени", 1) is None
    assert gate.consume("алисана привет", 1) is None
    assert gate.consume("расскажи про алису", 1) is None
    assert gate.consume("Алиса, привет", 2) == "привет"
    assert gate.consume("продолжай", 3) == "продолжай"
    assert gate.consume("продолжай", 13) is None
    assert gate.consume("эй алиса", 14) == ""
    assert gate.consume("сколько времени", 15) == "сколько времени"


@pytest.mark.parametrize("command", ["asr", "voice"])
def test_file_duration_are_exclusive(command):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command, "--file", "test.wav", "--duration", "10"])


async def test_voice_gate_only_sends_activated_text_and_speaks_real_reply(monkeypatch, tmp_path):
    state = {"capture_open": False}
    utterances = iter(["посторонняя речь", "алиса сколько будет два плюс два"])

    class Recognizer:
        def __init__(self, *args):
            pass

        def accept(self, chunk):
            return next(utterances)

        def finish(self):
            return ""

        def reset(self):
            pass

    async def source(args):
        state["capture_open"] = True
        try:
            yield b"background"
            yield b"command"
        finally:
            state["capture_open"] = False

    async def speak(audio, device):
        assert not state["capture_open"]
        assert audio == b"real-answer-audio"

    client = Mock(
        connected=False,
        connect=AsyncMock(),
        synchronize_state=AsyncMock(),
        close=AsyncMock(),
        send_text=AsyncMock(return_value={"response_payload": {"text": "Четыре"}}),
        tts=AsyncMock(return_value=b"real-answer-audio"),
    )
    monkeypatch.setattr(voice, "LocalRecognizer", Recognizer)
    monkeypatch.setattr(voice, "input_chunks", source)
    monkeypatch.setattr(voice, "YandexAliceClient", Mock(return_value=client))
    playback = AsyncMock(side_effect=speak)
    monkeypatch.setattr(voice, "play_audio", playback)
    args = cli.build_parser().parse_args(["voice", "--once", "--output", str(tmp_path)])
    args.wake_word = "алиса"
    await voice.run_voice(args, "test-token")
    client.send_text.assert_awaited_once_with("сколько будет два плюс два")
    client.tts.assert_awaited_once_with("Четыре")
    playback.assert_awaited_once()
    client.close.assert_awaited_once()
    assert (tmp_path / "reply-001.opus").read_bytes() == b"real-answer-audio"


async def test_voice_cancellation_closes_capture_and_connection(monkeypatch):
    started = asyncio.Event()
    closed = asyncio.Event()

    async def source(args):
        try:
            started.set()
            await asyncio.Event().wait()
            yield b""
        finally:
            closed.set()

    monkeypatch.setattr(voice, "input_chunks", source)
    monkeypatch.setattr(voice, "LocalRecognizer", Mock())
    client = Mock(close=AsyncMock())
    monkeypatch.setattr(voice, "YandexAliceClient", Mock(return_value=client))
    args = cli.build_parser().parse_args(["voice"])
    args.wake_word = "алиса"
    task = asyncio.create_task(voice.run_voice(args, None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    client.close.assert_awaited_once()


def test_local_recognizer_missing_model_never_downloads(monkeypatch, tmp_path):
    import sys

    from alice_client import YandexAliceClientError
    from alice_client.local_asr import LocalRecognizer

    monkeypatch.setitem(sys.modules, "vosk", Mock())
    with pytest.raises(YandexAliceClientError, match="models --lang en"):
        LocalRecognizer("en", tmp_path / "absent")


async def test_microphone_cancel_closes_device(monkeypatch):
    from alice_client import audio

    stream = Mock()
    fake = SimpleNamespace(RawInputStream=Mock(return_value=stream), PortAudioError=RuntimeError)
    monkeypatch.setattr(audio, "dependencies", lambda: (None, fake, None, None))
    source = audio.microphone_chunks()
    task = asyncio.create_task(anext(source))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream.abort.assert_called_once()
    stream.close.assert_called_once()


async def test_microphone_overflow_is_reported(monkeypatch):
    from alice_client import YandexAliceClientError, audio

    def create(**kwargs):
        def start():
            for _ in range(102):
                kwargs["callback"](bytes(1600), 800, None, False)

        return Mock(start=start)

    fake = SimpleNamespace(RawInputStream=create, PortAudioError=RuntimeError)
    monkeypatch.setattr(audio, "dependencies", lambda: (None, fake, None, None))
    source = audio.microphone_chunks()
    await anext(source)
    with pytest.raises(YandexAliceClientError, match="Переполнение"):
        await anext(source)


def test_file_conversion_preserves_duration_and_mono(tmp_path):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("soxr")
    pytest.importorskip("sounddevice")
    from alice_client.audio import file_chunks

    path = tmp_path / "stereo.wav"
    source = np.full((48000, 2), 0.1, dtype=np.float32)
    sf.write(path, source, 48000)
    raw = b"".join(file_chunks(path))
    assert len(raw) == 32000  # One second, mono PCM16 at 16 kHz.
    assert abs(np.frombuffer(raw, "<i2").mean() - 3276) < 10


def test_windows_wasapi_selection_keeps_the_same_endpoint(monkeypatch):
    from alice_client import audio

    devices = [
        {"name": "Station", "index": 3, "hostapi": 0, "max_output_channels": 2},
        {"name": "TV", "index": 7, "hostapi": 1, "max_output_channels": 2},
        {"name": "Station", "index": 14, "hostapi": 1, "max_output_channels": 2},
    ]
    sd = SimpleNamespace(
        query_devices=lambda *args: devices[0] if args else devices,
        query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(audio.sys, "platform", "win32")
    assert audio.output_device(sd)[0] == 14
    assert audio.output_device(sd, 3)[0] == 3  # Explicit choice is honored.


async def test_playback_converts_rate_and_void_callback_returns_none(monkeypatch):
    np = pytest.importorskip("numpy")
    from io import BytesIO

    from alice_client import audio

    data = np.full((4800, 1), 0.1, dtype=np.float32)
    sf = SimpleNamespace(read=lambda *a, **k: (data, 48000), LibsndfileError=OSError)
    soxr = SimpleNamespace(resample=Mock(return_value=data))

    class CallbackStop(Exception):
        pass

    finished_result = []
    stream = Mock()

    def create(**kwargs):
        assert kwargs["samplerate"] == 44100

        def start():
            with pytest.raises(CallbackStop):
                kwargs["callback"](np.zeros((4800, 1)), 4800, None, None)
            finished_result.append(kwargs["finished_callback"]())

        stream.start.side_effect = start
        return stream

    sd = SimpleNamespace(
        OutputStream=create, CallbackStop=CallbackStop, PortAudioError=RuntimeError
    )
    monkeypatch.setattr(audio, "dependencies", lambda: (np, sd, sf, soxr))
    monkeypatch.setattr(
        audio,
        "output_device",
        lambda *args: (14, {"default_samplerate": 44100, "max_output_channels": 2}),
    )
    await audio.play_audio(BytesIO(b"audio").getvalue())
    assert finished_result == [None]
    soxr.resample.assert_called_once()
    stream.close.assert_called_once()


def test_local_asr_ignores_even_an_expired_account_token(monkeypatch):
    monkeypatch.setattr(cli, "get_token", Mock(side_effect=AssertionError("should not load token")))
    runner = AsyncMock()
    monkeypatch.setattr(cli, "run_asr", runner)
    assert cli.main(["asr", "--duration", "10"]) == 0
    assert runner.call_args.args[1] is None


@pytest.mark.parametrize("malformed", ["hash", "traversal"])
def test_model_download_rejects_bad_hash_or_unsafe_archive(monkeypatch, tmp_path, malformed):
    import hashlib
    import io
    import zipfile

    from alice_client import local_asr

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside", b"bad")
    content = buffer.getvalue()
    monkeypatch.setattr(local_asr, "model_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(local_asr.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(content))
    if malformed == "traversal":
        monkeypatch.setitem(local_asr.HASHES, "ru", hashlib.sha256(content).hexdigest())
    with pytest.raises(ValueError):
        local_asr.install_model("ru")
    assert not (tmp_path / "outside").exists()
    assert list((tmp_path / "cache").iterdir()) == []
