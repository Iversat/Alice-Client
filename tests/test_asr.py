import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from alice_client import AliceTimeoutError, YandexAliceClientError
from alice_client.asr import FRAMES_PER_BUFFER, YandexASRClient, microphone_worker


def result(message_id, final=False, code="OK"):
    return {
        "header": {"namespace": "ASR", "name": "Result", "refMessageId": message_id},
        "payload": {
            "responseCode": code,
            "endOfUtt": final,
            "recognition": [{"words": [{"value": "проверка"}], "normalized": "Проверка."}],
        },
    }


def asr_client(mode="end"):
    client = YandexASRClient(
        oauth_token="local-test-token", asr_key="local-test-key", request_timeout=0.02
    )
    events, frames, controls = [], [], []

    async def send(raw):
        if isinstance(raw, bytes):
            frames.append(raw)
            if mode == "audio":
                await client._on_directive(result(client._current_asr_message_id))
                await client._on_directive(result(client._current_asr_message_id, final=True))
        else:
            data = json.loads(raw)
            if "event" in data:
                events.append(data["event"])
            else:
                controls.append(data["streamcontrol"])
                if mode in {"end", "error"}:
                    await client._on_directive(
                        result(
                            client._current_asr_message_id,
                            final=True,
                            code="OK" if mode == "end" else "DENIED",
                        )
                    )

    client._ws = AsyncMock(send=AsyncMock(side_effect=send))
    client._is_connected = True
    return client, events, frames, controls


@pytest.mark.parametrize("mode", ["end", "audio"])
async def test_pcm_framing_results_eof_and_no_empty_extra_utterance(mode):
    client, events, frames, controls = asr_client(mode)
    final, partial = [], []
    client.final_result_callback = lambda text, mid: final.append(text)
    client.partial_result_callback = lambda text, mid: partial.append(text)
    queue = asyncio.Queue()
    await queue.put(b"\x01\x02" * 8)
    await queue.put(None)
    await client.run_realtime_asr(queue)
    await queue.join()
    requests = [event for event in events if event["header"]["name"] == "Recognize"]
    assert len(requests) == 1
    sid = requests[0]["header"]["streamId"]
    assert frames == [sid.to_bytes(4, "big") + b"\x01\x02" * 8]
    assert requests[0]["payload"]["asr"]["format"] == "audio/x-pcm;bit=16;rate=16000"
    assert final == ["Проверка."]
    assert partial == (["Проверка."] if mode == "audio" else [])
    assert len(controls) == 1
    assert not client._running


async def test_empty_input_creates_no_recognize_event():
    client, events, frames, _ = asr_client()
    queue = asyncio.Queue()
    await queue.put(None)
    await client.run_realtime_asr(queue)
    assert [event["header"]["name"] for event in events] == ["SynchronizeState"]
    assert frames == []


@pytest.mark.parametrize(
    "mode,error", [("timeout", AliceTimeoutError), ("error", YandexAliceClientError)]
)
async def test_final_timeout_or_service_error_stops_session(mode, error):
    client, _, _, controls = asr_client(mode)
    queue = asyncio.Queue()
    await queue.put(b"\0\0")
    await queue.put(None)
    with pytest.raises(error):
        await client.run_realtime_asr(queue)
    assert len(controls) == 1
    assert not client._running
    assert client._current_asr_message_id is None


async def test_stale_result_is_ignored_even_between_utterances():
    client, _, _, controls = asr_client()
    callback = Mock()
    client.final_result_callback = callback
    client._current_asr_message_id = "current"
    for active in (True, False):
        client._utterance_in_progress = active
        await client._on_directive(result("old", final=True))
    callback.assert_not_called()
    assert controls == []


@pytest.mark.parametrize("chunk", [b"odd", "not-bytes"])
async def test_invalid_pcm_is_rejected_before_recognize(chunk):
    client, events, _, _ = asr_client()
    queue = asyncio.Queue()
    await queue.put(chunk)
    with pytest.raises(ValueError):
        await client.run_realtime_asr(queue)
    assert all(event["header"]["name"] != "Recognize" for event in events)


async def test_cancellation_sends_end_control_once():
    client, _, frames, controls = asr_client("timeout")
    queue = asyncio.Queue()
    await queue.put(b"\0\0")
    task = asyncio.create_task(client.run_realtime_asr(queue))
    while not frames:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(controls) == 1
    assert not client._running


async def test_missing_asr_credentials_fail_without_connection():
    with pytest.raises(ValueError):
        await YandexASRClient().run_realtime_asr(asyncio.Queue())


async def test_microphone_cleanup_on_read_error(monkeypatch):
    stream = Mock(read=Mock(side_effect=OSError("unplugged")))
    audio = Mock(open=Mock(return_value=stream))
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=lambda: audio, paInt16=8))
    with pytest.raises(OSError):
        await microphone_worker(asyncio.Queue(), asyncio.Event())
    stream.stop_stream.assert_called_once()
    stream.close.assert_called_once()
    audio.terminate.assert_called_once()


async def test_microphone_cancellation_while_queue_full_closes_driver(monkeypatch):
    loop, read = asyncio.get_running_loop(), asyncio.Event()

    def read_audio(*args, **kwargs):
        loop.call_soon_threadsafe(read.set)
        return bytes(FRAMES_PER_BUFFER * 2)

    stream = Mock(read=Mock(side_effect=read_audio))
    audio = Mock(open=Mock(return_value=stream))
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=lambda: audio, paInt16=8))
    queue = asyncio.Queue(maxsize=1)
    await queue.put(b"already-full")
    task = asyncio.create_task(microphone_worker(queue, asyncio.Event()))
    await read.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    stream.close.assert_called_once()
    audio.terminate.assert_called_once()


async def test_optional_audio_dependency_has_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyaudio", None)
    with pytest.raises(YandexAliceClientError, match="audio"):
        await microphone_worker(asyncio.Queue(), asyncio.Event())
