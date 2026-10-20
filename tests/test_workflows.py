"""Exercise CLI orchestration with synthetic audio and actual local WebSockets."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.server import serve

from alice_client import YandexAliceClientError, cli


async def test_asr_duration_with_real_socket_synthetic_pcm_and_final_result(monkeypatch, capsys):
    seen_frames = []

    async def handler(socket):
        current = None
        async for raw in socket:
            if isinstance(raw, bytes):
                seen_frames.append(raw)
                continue
            data = json.loads(raw)
            if "event" in data and data["event"]["header"]["name"] == "Recognize":
                current = data["event"]["header"]["messageId"]
            if "streamcontrol" in data:
                await socket.send(
                    json.dumps(
                        {
                            "directive": {
                                "header": {
                                    "namespace": "ASR",
                                    "name": "Result",
                                    "refMessageId": current,
                                },
                                "payload": {
                                    "responseCode": "OK",
                                    "endOfUtt": True,
                                    "recognition": [{"normalized": "Тестовая запись."}],
                                },
                            }
                        }
                    )
                )

    async def synthetic_microphone(queue, stop):
        await queue.put(b"\0\0" * 1024)
        await stop.wait()

    monkeypatch.setenv("YANDEX_ASR_KEY", "local-test-key")
    monkeypatch.setattr(cli, "microphone_worker", synthetic_microphone)
    async with serve(handler, "127.0.0.1", 0) as server:
        args = cli.build_parser().parse_args(
            [
                "asr",
                "--server",
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                "--duration",
                "0.02",
                "--timeout",
                "1",
                "--partial",
            ]
        )
        await asyncio.wait_for(cli.run_asr(args, "local-test-token"), 2)
    assert len(seen_frames) == 1
    assert "Тестовая запись." in capsys.readouterr().out
    assert not any(
        task.get_name() in {"alice-microphone", "alice-asr", "alice-receiver"}
        for task in asyncio.all_tasks()
    )


async def test_asr_microphone_failure_cancels_recognition_and_closes_client(monkeypatch):
    client = Mock(connect=AsyncMock(), close=AsyncMock())
    client.run_realtime_asr = AsyncMock(side_effect=lambda queue: None)

    async def wait_forever(queue):
        await asyncio.Event().wait()

    client.run_realtime_asr.side_effect = wait_forever
    monkeypatch.setattr(cli, "YandexASRClient", Mock(return_value=client))
    monkeypatch.setattr(
        cli, "microphone_worker", AsyncMock(side_effect=YandexAliceClientError("Microphone failed"))
    )
    monkeypatch.setenv("YANDEX_ASR_KEY", "local-test-key")
    args = cli.build_parser().parse_args(["asr"])
    with pytest.raises(YandexAliceClientError, match="Microphone"):
        await cli.run_asr(args, "local-test-token")
    client.close.assert_awaited_once()


def test_legacy_style_interactive_tts(monkeypatch, tmp_path):
    client = Mock(tts=AsyncMock(return_value=b"audio"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "YandexAliceClient", Mock(return_value=client))
    monkeypatch.setattr(cli, "read_line", AsyncMock(side_effect=["", "Привет", "exit"]))
    assert cli.main(["tts", "--output", str(tmp_path / "speech.opus")]) == 0
    client.tts.assert_awaited_once_with("Привет", voice="shitova.us")


def test_interactive_chat_skips_blank_and_exits(monkeypatch):
    client = Mock(
        send_text=AsyncMock(return_value={"response_payload": {"text": "Привет"}, "audio": None})
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "YandexAliceClient", Mock(return_value=client))
    monkeypatch.setattr(cli, "read_line", AsyncMock(side_effect=["", "Привет", "exit"]))
    assert cli.main(["chat"]) == 0
    client.send_text.assert_awaited_once_with("Привет", is_tts_response=False)
