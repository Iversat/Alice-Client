import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from alice_client import AliceTimeoutError, YandexAliceClient, YandexAliceClientError


def directive(message_id, name="VinsResponse", payload=None, **header):
    return {
        "directive": {
            "header": {"refMessageId": message_id, "name": name, **header},
            "payload": {"response": {"text": "Ответ"}} if payload is None else payload,
        }
    }


def attached_client(handler=None, **kwargs):
    client = YandexAliceClient(**kwargs)

    async def send(raw):
        if handler is not None:
            await handler(client, raw)

    client._ws = AsyncMock(send=AsyncMock(side_effect=send))
    client._is_connected = True
    return client


async def test_immediate_response_is_registered_before_send():
    async def reply(client, raw):
        event = json.loads(raw)["event"]
        await client._on_message(json.dumps(directive(event["header"]["messageId"])))

    client = attached_client(reply)
    result = await client.send_text("Привет")
    assert result["response_payload"]["text"] == "Ответ"
    assert client._active_requests == client._active_streams == {}


async def test_dialogue_carries_server_session_serializes_turns_and_resets():
    sent = []

    async def reply(client, raw):
        event = json.loads(raw)["event"]
        sent.append(event["payload"])
        await asyncio.sleep(0)
        await client._on_message(
            json.dumps(
                directive(
                    event["header"]["messageId"],
                    payload={
                        "header": {"session_id": f"session-{len(sent)}"},
                        "response": {"text": "Ответ"},
                    },
                )
            )
        )

    client = attached_client(reply)
    await asyncio.gather(client.send_text("one"), client.send_text("two"))
    assert "header" not in sent[0]
    assert sent[1]["header"] == {"session_id": "session-1"}
    client.reset_session()
    await client.send_text("fresh")
    assert "header" not in sent[2]
    client.set_oauth_token("changed-account")
    await client.send_text("fresh-account")
    assert "header" not in sent[3]


@pytest.mark.parametrize("order", ["text-first", "audio-first"])
async def test_audio_framing_and_response_order(order):
    async def reply(client, raw):
        mid = json.loads(raw)["event"]["header"]["messageId"]
        if order == "text-first":
            await client._on_message(json.dumps(directive(mid)))
        await client._on_message(json.dumps(directive(mid, "Speak", {}, streamId=7)))
        await client._on_message((7).to_bytes(4, "big") + b"first")
        await client._on_message((7).to_bytes(4, "big") + b"second")
        await client._on_message(json.dumps({"streamcontrol": {"streamId": 7, "action": 0}}))
        if order == "audio-first":
            await client._on_message(json.dumps(directive(mid)))

    client = attached_client(reply)
    result = await client.send_text("Привет", is_tts_response=True)
    assert result["audio"] == b"firstsecond"
    assert client._active_requests == client._active_streams == {}


async def test_concurrent_requests_can_complete_out_of_order():
    sent = []

    async def reply(client, raw):
        sent.append(json.loads(raw)["event"]["header"]["messageId"])
        if len(sent) == 2:
            for number, mid in reversed(list(enumerate(sent))):
                await client._on_message(
                    json.dumps(directive(mid, payload={"response": {"text": str(number)}}))
                )

    client = attached_client(reply)
    # Raw independent requests can arrive out of order; dialogue turns are serialized.
    results = await asyncio.gather(
        client._request("Vins", "TextInput", {}, {"VinsResponse"}),
        client._request("Vins", "TextInput", {}, {"VinsResponse"}),
    )
    assert [r["directives"][0]["payload"]["response"]["text"] for r in results] == ["0", "1"]


async def test_timeout_releases_incomplete_streams():
    async def reply(client, raw):
        mid = json.loads(raw)["event"]["header"]["messageId"]
        await client._on_message(json.dumps(directive(mid, "Speak", {}, streamId=1)))
        await client._on_message(b"\0\0\0\1unfinished")

    client = attached_client(reply, request_timeout=0.01)
    with pytest.raises(AliceTimeoutError):
        await client.tts("hello")
    assert client._active_requests == client._active_streams == {}


async def test_cancellation_releases_requests_and_streams():
    sent = asyncio.Event()

    async def reply(client, raw):
        mid = json.loads(raw)["event"]["header"]["messageId"]
        await client._on_message(json.dumps(directive(mid, "Speak", {}, streamId=1)))
        sent.set()

    client = attached_client(reply)
    task = asyncio.create_task(client.tts("hello"))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._active_requests == client._active_streams == {}


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        '["directive"]',
        "42",
        '"text"',
        "{broken",
        '{"directive":[]}',
        '{"directive":{"header":[]}}',
        '{"directive":{"header":{"refMessageId":[]}}}',
        '{"streamcontrol":{"streamId":[]}}',
        b"\0\0",
        b"\0\0\0\xffignored",
    ],
)
async def test_malformed_and_unsolicited_messages_are_ignored(raw):
    client = attached_client()
    await client._on_message(raw)
    assert client._active_requests == client._active_streams == {}


@pytest.mark.parametrize(
    "kind",
    [
        "Error",
        "Failure",
        "invalid-stream",
        "stream-error",
        "oversized",
        "malformed-response",
        "empty-tts",
        "top-error",
    ],
)
async def test_protocol_errors_fail_and_cleanup(kind):
    async def reply(client, raw):
        mid = json.loads(raw)["event"]["header"]["messageId"]
        if kind in {"Error", "Failure"}:
            await client._on_message(json.dumps(directive(mid, kind, {})))
        elif kind == "invalid-stream":
            await client._on_message(json.dumps(directive(mid, "Speak", {}, streamId="7")))
        elif kind == "malformed-response":
            await client._on_message(json.dumps(directive(mid, payload=[])))
        elif kind == "top-error":
            await client._on_message('{"error":{"details":"private remote error"}}')
        else:
            await client._on_message(json.dumps(directive(mid, "Speak", {}, streamId=7)))
            if kind == "oversized":
                await client._on_message((7).to_bytes(4, "big") + b"x" * 8)
            await client._on_message(
                json.dumps(
                    {"streamcontrol": {"streamId": 7, "reason": 1 if kind == "stream-error" else 0}}
                )
            )

    client = attached_client(reply, max_audio_bytes=4)
    with pytest.raises(YandexAliceClientError):
        if kind == "malformed-response":
            await client.send_text("hello")
        else:
            await client.tts("hello")
    assert client._active_requests == client._active_streams == {}


async def test_send_failure_never_echoes_token_or_leaves_future():
    client = attached_client(oauth_token="local-test-secret")
    client._ws.send.side_effect = OSError("local-test-secret")
    with pytest.raises(YandexAliceClientError) as caught:
        await client.send_text("hello")
    assert "local-test-secret" not in str(caught.value)
    assert not client.connected
    assert client._active_requests == {}


async def test_close_wakes_pending_request_and_closes_socket_even_after_disconnect():
    sent = asyncio.Event()

    async def reply(client, raw):
        sent.set()

    client = attached_client(reply)
    socket = client._ws
    task = asyncio.create_task(client.send_text("hello"))
    await sent.wait()
    client._is_connected = False
    await client.close()
    with pytest.raises(YandexAliceClientError):
        await task
    socket.close.assert_awaited_once()
    assert client._active_requests == {}


async def test_clean_iterator_end_marks_disconnected():
    client = attached_client()
    client._ws.__aiter__.return_value = []
    await client._receive_loop()
    assert not client.connected


async def test_anonymous_requests_omit_credentials_and_use_stable_identity():
    events = []

    async def reply(client, raw):
        event = json.loads(raw)["event"]
        events.append(event)
        if event["header"]["name"] == "TextInput":
            await client._on_message(json.dumps(directive(event["header"]["messageId"])))

    client = attached_client(reply)
    await client.synchronize_state()
    await client.send_text("one")
    await client.send_text("two")
    assert "auth_token" not in events[0]["payload"]
    assert "additional_options" not in events[1]["payload"]["request"]
    assert (
        events[1]["payload"]["application"]["uuid"] == events[2]["payload"]["application"]["uuid"]
    )


async def test_token_goes_only_in_expected_payload_fields():
    events = []

    async def reply(client, raw):
        event = json.loads(raw)["event"]
        events.append(event)
        if event["header"]["name"] == "TextInput":
            await client._on_message(json.dumps(directive(event["header"]["messageId"])))

    client = attached_client(reply, oauth_token="local-test-secret")
    await client.synchronize_state()
    await client.send_text("one")
    assert events[0]["payload"]["auth_token"] == "local-test-secret"
    assert (
        events[1]["payload"]["request"]["additional_options"]["oauth_token"] == "local-test-secret"
    )
    client.set_oauth_token(None)
    assert client.oauth_token is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_validation(timeout):
    with pytest.raises(ValueError):
        YandexAliceClient(request_timeout=timeout)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "ws://example.com",
        "wss://user:password@example.com",
        "wss://example.com/#fragment",
    ],
)
def test_url_validation(url):
    with pytest.raises(ValueError):
        YandexAliceClient(url)


@pytest.mark.parametrize("text", ["", "   ", None, 42])
async def test_empty_or_nontext_request_rejected_without_send(text):
    client = attached_client()
    for call in (client.send_text, client.tts):
        with pytest.raises(ValueError):
            await call(text)
    client._ws.send.assert_not_awaited()


async def test_request_while_disconnected():
    with pytest.raises(YandexAliceClientError):
        await YandexAliceClient().send_text("hello")
