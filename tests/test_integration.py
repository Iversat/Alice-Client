"""Real sockets against a local test server; no external service or credentials."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from alice_client import YandexAliceClient, YandexAliceClientError
from alice_client.sync import YandexAliceClientPython


async def echo_handler(socket):
    async for raw in socket:
        event = json.loads(raw)["event"]
        if event["header"]["name"] == "SynchronizeState":
            continue
        mid = event["header"]["messageId"]
        await socket.send(
            json.dumps(
                {
                    "directive": {
                        "header": {"name": "VinsResponse", "refMessageId": mid},
                        "payload": {
                            "response": {"text": event["payload"]["request"]["event"]["text"]}
                        },
                    }
                }
            )
        )


@pytest.fixture
async def local_server():
    async with serve(echo_handler, "127.0.0.1", 0) as server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def test_real_websocket_context_and_reconnect(local_server):
    client = YandexAliceClient(local_server)
    for _ in range(2):
        async with client:
            await client.connect()  # idempotent
            result = await client.send_text("Привет из локального теста")
            assert result["response_payload"]["text"] == "Привет из локального теста"
        assert not client.connected
        assert client._receive_loop_task is None


async def test_real_remote_clean_close_fails_pending_promptly():
    async def close_handler(socket):
        await socket.recv()
        await socket.close()

    async with serve(close_handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        client = YandexAliceClient(url, request_timeout=3)
        await client.connect()
        try:
            with pytest.raises(YandexAliceClientError):
                await asyncio.wait_for(client.send_text("hello"), 1)
        finally:
            await client.close()


async def test_synchronous_adapter_uses_real_websocket_and_stops_thread(local_server):
    def call_sync():
        with YandexAliceClientPython(local_server) as client:
            result = client.send_text("sync")
            assert result["response"]["text"] == "sync"
        client.close()
        assert not client._thread.is_alive()
        with pytest.raises(RuntimeError):
            client.send_text("closed")

    await asyncio.to_thread(call_sync)
