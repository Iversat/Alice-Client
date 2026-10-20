from pathlib import Path
from unittest.mock import Mock

import pytest

from alice_client import auth, cli, env_auth


def test_atomic_save_preserves_settings_and_clears_old_refresh(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# My settings\nYANDEX_ASR_KEY='keep-me'\nYANDEX_REFRESH_TOKEN='old'\n", encoding="utf-8"
    )
    secret = "test'\\${UNEXPANDED}"
    env_auth.save_env_token(path, auth.OAuthToken(secret, client_id="test-client"))
    assert env_auth.file_token(path).access_token == secret
    assert env_auth.file_token(path).refresh_token is None
    assert env_auth.read_env(path)["YANDEX_ASR_KEY"] == "keep-me"
    assert "# My settings" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_failed_write_keeps_original_file(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    old = "YANDEX_OAUTH_TOKEN='previous'\n"
    path.write_text(old, encoding="utf-8")
    monkeypatch.setattr(env_auth, "set_key", Mock(side_effect=OSError("private")))
    with pytest.raises(OSError):
        env_auth.save_env_token(path, auth.OAuthToken("new"))
    assert path.read_text(encoding="utf-8") == old
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_env_priority_anonymous_expiration_and_no_process_mutation(monkeypatch, tmp_path):
    auth.TokenStore().save(auth.OAuthToken("vault"))
    env_auth.save_env_token(tmp_path / ".env", auth.OAuthToken("file"))
    assert auth.get_token() == "file"
    assert "YANDEX_OAUTH_TOKEN" not in env_auth.os.environ
    monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "process")
    assert auth.get_token() == "process"
    assert auth.get_token(anonymous=True) is None
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN")
    env_auth.save_env_token(tmp_path / ".env", auth.OAuthToken("expired", expires_at=1))
    with pytest.raises(ValueError, match="истёк"):
        auth.get_token()
    assert auth.get_token(anonymous=True) is None


def test_only_current_directory_env_is_loaded(tmp_path, monkeypatch):
    env_auth.save_env_token(tmp_path / ".env", auth.OAuthToken("parent"))
    child = tmp_path / "unrelated"
    child.mkdir()
    monkeypatch.chdir(child)
    assert auth.get_token() is None


def test_login_browser_to_file_to_chat(monkeypatch, tmp_path, capsys):
    from test_cli import mock_client

    service = Mock()
    service.start.return_value = auth.DeviceCode("test-device", "test-user-code", 1, 30)
    service.wait.return_value = auth.OAuthToken("test-secret", "test-refresh", client_id="test-app")
    constructor = Mock(return_value=service)
    monkeypatch.setattr(env_auth, "YandexAuth", constructor)
    browser = Mock()
    monkeypatch.setattr(env_auth.webbrowser, "open", browser)
    assert env_auth.main(["--client-id", "test-app"]) == 0
    browser.assert_called_once_with(auth.VERIFY_URL)
    assert env_auth.file_token().access_token == "test-secret"
    assert "test-secret" not in capsys.readouterr().out
    _, client = mock_client(monkeypatch)
    assert cli.main(["chat", "--text", "Привет"]) == 0
    assert client.call_args.args[1] == "test-secret"


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), auth.AuthError("access_denied")])
def test_cancelled_login_preserves_existing_env(monkeypatch, tmp_path, capsys, failure):
    path = tmp_path / ".env"
    env_auth.save_env_token(path, auth.OAuthToken("previous", client_id="app"))
    old = path.read_bytes()
    service = Mock()
    service.start.return_value = auth.DeviceCode("d", "u", 1, 30)
    service.wait.side_effect = failure
    monkeypatch.setattr(env_auth, "YandexAuth", Mock(return_value=service))
    assert env_auth.main(["--no-browser"]) in {1, 130}
    assert path.read_bytes() == old
    assert "previous" not in capsys.readouterr().out


def test_refresh_uses_selected_file_and_does_not_launch_browser(monkeypatch, tmp_path):
    path = tmp_path / "custom.env"
    env_auth.save_env_token(path, auth.OAuthToken("old", "refresh", client_id="app"))
    service = Mock()
    service.refresh.return_value = auth.OAuthToken("new", "refresh2", client_id="app")
    monkeypatch.setattr(env_auth, "YandexAuth", Mock(return_value=service))
    browser = Mock()
    monkeypatch.setattr(env_auth.webbrowser, "open", browser)
    assert env_auth.main(["--env-file", str(path), "--refresh"]) == 0
    assert env_auth.file_token(path).access_token == "new"
    browser.assert_not_called()


def test_prompt_client_id_and_secret_without_saving_secret(monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda _: "app")
    monkeypatch.setattr(env_auth.getpass, "getpass", lambda _: "application-secret")
    service = Mock()
    service.start.return_value = auth.DeviceCode("d", "u", 1, 30)
    service.wait.return_value = auth.OAuthToken("token", client_id="app")
    constructor = Mock(return_value=service)
    monkeypatch.setattr(env_auth, "YandexAuth", constructor)
    assert env_auth.main(["--secret-prompt", "--no-browser"]) == 0
    constructor.assert_called_once_with("app", "application-secret")
    assert "application-secret" not in Path(".env").read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["nan", "infinity", "broken"])
def test_bad_expiry_fails_without_echoing_value(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text(f"YANDEX_OAUTH_TOKEN=test\nYANDEX_TOKEN_EXPIRES_AT={value}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        auth.get_token()
