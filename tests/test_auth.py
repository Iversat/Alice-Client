import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from alice_client import auth, cli


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(auth.time, "sleep", sleep)
    return now, sleeps


def test_device_flow_pending_slow_down_then_token(monkeypatch, clock):
    request = Mock(
        side_effect=[
            {
                "device_code": "test-device",
                "user_code": "test-code",
                "interval": 2,
                "expires_in": 60,
                "verification_url": auth.VERIFY_URL,
            },
            auth.AuthError("authorization_pending"),
            auth.AuthError("slow_down"),
            {"access_token": "test-token", "refresh_token": "test-refresh", "expires_in": 3600},
        ]
    )
    monkeypatch.setattr(auth, "request_json", request)
    service = auth.YandexAuth("test-client", "test-client-secret")
    code = service.start("iot:view")
    token = service.wait(code)
    assert clock[1] == [2, 2, 7]
    assert token.access_token == "test-token"
    assert token.client_id == "test-client"
    assert not token.expired
    assert request.call_args.kwargs["data"]["grant_type"] == "device_code"
    assert request.call_args.kwargs["data"]["code"] == "test-device"
    assert "test-token" not in repr(token)
    assert "test-device" not in repr(code)


@pytest.mark.parametrize("error", ["access_denied", "invalid_client", "expired_token"])
def test_terminal_errors_stop_polling(monkeypatch, clock, error):
    request = Mock(side_effect=auth.AuthError(error))
    monkeypatch.setattr(auth, "request_json", request)
    with pytest.raises(auth.AuthError, match=error):
        auth.YandexAuth("test").wait(auth.DeviceCode("d", "u", 2, 60))
    assert request.call_count == 1


def test_flow_expiry_never_polls_after_deadline(monkeypatch, clock):
    request = Mock(side_effect=auth.AuthError("authorization_pending"))
    monkeypatch.setattr(auth, "request_json", request)
    with pytest.raises(auth.AuthError, match="expired_token"):
        auth.YandexAuth("test").wait(auth.DeviceCode("d", "u", 2, 5))
    assert request.call_count == 2
    assert clock[0][0] == 4


@pytest.mark.parametrize(
    "change",
    [
        {"verification_url": "https://example.com"},
        {"interval": 0},
        {"expires_in": "nan"},
        {"device_code": None},
        {"user_code": []},
    ],
)
def test_untrusted_device_response_rejected(monkeypatch, change):
    result = {
        "device_code": "d",
        "user_code": "u",
        "interval": 2,
        "expires_in": 60,
        "verification_url": auth.VERIFY_URL,
    } | change
    monkeypatch.setattr(auth, "request_json", Mock(return_value=result))
    with pytest.raises(auth.AuthError, match="malformed_response"):
        auth.YandexAuth("test").start()


def test_refresh_retains_old_refresh_token_when_omitted(monkeypatch):
    old = auth.OAuthToken("old", "refresh", client_id="test")
    request = Mock(return_value={"access_token": "new", "expires_in": 100})
    monkeypatch.setattr(auth, "request_json", request)
    result = auth.YandexAuth("test").refresh(old)
    assert result.access_token == "new"
    assert result.refresh_token == "refresh"
    assert request.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    with pytest.raises(ValueError):
        auth.YandexAuth("different-app").refresh(old)


def test_store_resolution_and_expiry(monkeypatch):
    store = auth.TokenStore()
    store.save(auth.OAuthToken("vault-secret", expires_at=auth.time.time() + 60))
    assert auth.get_token() == "vault-secret"
    monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "env-secret")
    assert auth.get_token() == "env-secret"
    assert auth.get_token(anonymous=True) is None
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN")
    store.save(auth.OAuthToken("expired", expires_at=1))
    with pytest.raises(ValueError, match="истёк"):
        auth.get_token()
    store.delete()
    store.delete()
    assert auth.get_token() is None


def test_vault_failure_never_leaks_secret():
    backend = Mock()
    backend.get_password.side_effect = RuntimeError("private-token")
    backend.set_password.side_effect = RuntimeError("private-token")
    store = auth.TokenStore(backend)
    for operation in (
        store.load,
        lambda: store.save(auth.OAuthToken("private-token")),
        store.delete,
    ):
        with pytest.raises(auth.AuthError) as error:
            operation()
        assert "private-token" not in str(error.value)


def test_check_access_only_returns_counts(monkeypatch):
    request = Mock(
        side_effect=[
            {"id": "private-user"},
            {
                "status": "ok",
                "devices": [{"name": "private-room", "id": "private-device"}],
                "groups": [],
                "scenarios": [],
            },
        ]
    )
    monkeypatch.setattr(auth, "request_json", request)
    result = auth.check_access("private-token")
    assert result["identity"]["ok"]
    assert result["smart_home"] == {"ok": True, "devices": 1, "groups": 0, "scenarios": 0}
    assert "private" not in json.dumps(result)


def test_identity_success_does_not_claim_smart_home_access(monkeypatch):
    monkeypatch.setattr(
        auth, "request_json", Mock(side_effect=[{"id": "id"}, auth.AuthError("http_403")])
    )
    result = auth.check_access("token")
    assert result["identity"]["ok"]
    assert not result["smart_home"]["ok"]


@pytest.mark.parametrize("body", [b"[]", b"invalid", b'{"error":"secret-token"}'])
def test_http_rejects_malformed_and_sanitizes_server_errors(monkeypatch, body):
    monkeypatch.setattr(
        auth, "build_opener", Mock(return_value=Mock(open=Mock(return_value=io.BytesIO(body))))
    )
    with pytest.raises(auth.AuthError) as caught:
        auth.request_json(auth.OAUTH + "/token", data={"code": "secret-token"})
    assert "secret-token" not in str(caught.value)


def test_network_failure_and_redirect_do_not_leak(monkeypatch):
    monkeypatch.setattr(
        auth, "build_opener", Mock(return_value=Mock(open=Mock(side_effect=URLError("secret"))))
    )
    with pytest.raises(auth.AuthError) as caught:
        auth.request_json(auth.OAUTH + "/token")
    assert "secret" not in str(caught.value)
    assert (
        auth._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com") is None
    )
    with pytest.raises(ValueError):
        auth.request_json("https://example.com", token="secret")


def test_http_unauthorized(monkeypatch):
    error = HTTPError(auth.OAUTH + "/token", 401, "", {}, io.BytesIO(b""))
    monkeypatch.setattr(auth, "build_opener", Mock(return_value=Mock(open=Mock(side_effect=error))))
    with pytest.raises(auth.AuthError, match="http_401"):
        auth.request_json(auth.OAUTH + "/token")


def test_cli_login_stores_without_printing_token(monkeypatch, capsys):
    service = Mock()
    service.start.return_value = auth.DeviceCode("device", "code", 1, 60)
    service.wait.return_value = auth.OAuthToken("private-token")
    monkeypatch.setattr(cli, "YandexAuth", Mock(return_value=service))
    browser = Mock()
    monkeypatch.setattr(cli.webbrowser, "open", browser)
    assert cli.main(["auth", "login", "--client-id", "test", "--no-browser"]) == 0
    assert auth.get_token() == "private-token"
    assert "private-token" not in capsys.readouterr().out
    browser.assert_not_called()


def test_cli_status_logout_and_explicit_export(capsys):
    auth.TokenStore().save(auth.OAuthToken("private-token"))
    assert cli.main(["auth", "status"]) == 0
    assert "private-token" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["auth", "token"])
    assert "private-token" not in capsys.readouterr().out
    assert cli.main(["auth", "token", "--show"]) == 0
    assert capsys.readouterr().out.strip() == "private-token"
    assert cli.main(["auth", "logout"]) == 0
    assert auth.get_token() is None


def test_cli_chat_uses_saved_token(monkeypatch):
    from test_cli import mock_client

    _, constructor = mock_client(monkeypatch)
    auth.TokenStore().save(auth.OAuthToken("private-token"))
    assert cli.main(["chat", "--text", "Привет"]) == 0
    assert constructor.call_args.args[1] == "private-token"
    assert cli.main(["chat", "--text", "Привет", "--anonymous"]) == 0
    assert constructor.call_args.args[1] is None
