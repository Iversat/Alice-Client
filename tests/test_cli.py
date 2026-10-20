import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from alice_client import YandexAliceClientError, cli


def mock_client(monkeypatch):
    client = Mock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.send_text = AsyncMock(
        return_value={"response_payload": {"text": "Локальный ответ"}, "audio": b"audio"}
    )
    client.tts = AsyncMock(return_value=b"audio")
    constructor = Mock(return_value=client)
    monkeypatch.setattr(cli, "YandexAliceClient", constructor)
    return client, constructor


def test_one_shot_json_and_audio_file(monkeypatch, tmp_path, capsys):
    client, constructor = mock_client(monkeypatch)
    monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "local-test-secret")
    path = tmp_path / "reply.opus"
    assert cli.main(["chat", "--text", "Привет", "--json", "--audio", "--output", str(path)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"text": "Локальный ответ"}
    assert "local-test-secret" not in output.out + output.err
    assert path.read_bytes() == b"audio"
    assert constructor.call_args.args[1] == "local-test-secret"
    client.__aexit__.assert_awaited_once()


def test_anonymous_ignores_environment_and_tts_saves_nested_path(monkeypatch, tmp_path):
    _, constructor = mock_client(monkeypatch)
    monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "local-test-secret")
    path = tmp_path / "nested" / "speech.opus"
    assert cli.main(["tts", "Проверка", "--anonymous", "--output", str(path)]) == 0
    assert constructor.call_args.args[1] is None
    assert path.read_bytes() == b"audio"


def test_hidden_token_prompt(monkeypatch):
    _, constructor = mock_client(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", Mock(return_value="local-test-secret"))
    assert cli.main(["chat", "--text", "hi", "--token-prompt"]) == 0
    assert constructor.call_args.args[1] == "local-test-secret"


def test_cli_server_error_has_nonzero_status(monkeypatch, capsys):
    client, _ = mock_client(monkeypatch)
    client.send_text.side_effect = YandexAliceClientError("Сервис недоступен.")
    assert cli.main(["chat", "--text", "hi"]) == 1
    assert "недоступен" in capsys.readouterr().err
    client.__aexit__.assert_awaited_once()


def test_spoken_reply_synthesizes_actual_response_when_inline_audio_is_absent(
    monkeypatch, tmp_path
):
    client, _ = mock_client(monkeypatch)
    client.send_text.return_value["audio"] = None
    path = tmp_path / "spoken.opus"
    assert cli.main(["chat", "--text", "Привет", "--audio", "--output", str(path)]) == 0
    client.send_text.assert_awaited_once_with("Привет", is_tts_response=False)
    client.tts.assert_awaited_once_with("Локальный ответ")
    assert path.read_bytes() == b"audio"


def test_spoken_reply_does_not_synthesize_placeholder_for_missing_response_text(
    monkeypatch, tmp_path
):
    client, _ = mock_client(monkeypatch)
    client.send_text.return_value = {"response_payload": {}, "audio": None}
    assert (
        cli.main(["chat", "--text", "hi", "--audio", "--output", str(tmp_path / "empty.opus")]) == 1
    )
    client.tts.assert_not_awaited()


@pytest.mark.parametrize(
    "args", [["chat", "--text", "  "], ["chat", "--timeout", "nan"], ["asr", "--duration", "-1"]]
)
def test_invalid_arguments_do_not_open_connection(monkeypatch, args):
    _, constructor = mock_client(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2
    constructor.assert_not_called()


def test_asr_without_key_does_not_activate_microphone(monkeypatch):
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_ASR_KEY", raising=False)
    mic = AsyncMock()
    monkeypatch.setattr(cli, "microphone_worker", mic)
    with pytest.raises(SystemExit) as caught:
        cli.main(["asr"])
    assert caught.value.code == 2
    mic.assert_not_awaited()


@pytest.mark.parametrize(
    "payload,text",
    [
        ({"card": {"text": "Карточка"}}, "Карточка"),
        ({"text": "Текст"}, "Текст"),
        ({}, "Ответ получен без текстового содержимого."),
    ],
)
def test_response_rendering(payload, text):
    assert cli.response_text(payload) == text


def test_response_error_does_not_echo_remote_private_message():
    with pytest.raises(YandexAliceClientError) as caught:
        cli.response_text({"error": "local-test-secret"})
    assert "local-test-secret" not in str(caught.value)


def test_no_audio_does_not_create_empty_file(tmp_path):
    path = tmp_path / "empty.opus"
    with pytest.raises(ValueError):
        cli.save_audio_to_file(None, path)
    assert not path.exists()


async def test_interactive_input_does_not_block_event_loop(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "Привет")
    assert await asyncio.wait_for(cli.read_line("test"), 1) == "Привет"


async def test_interactive_eof_is_delivered(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(EOFError):
        await asyncio.wait_for(cli.read_line("test"), 1)
