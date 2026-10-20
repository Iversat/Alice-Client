import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from alice_client import auth, env_auth, passport_auth

LINK = "https://passport.yandex.ru/am/push/qrsecure?track_id=test-track&magic=test-magic"


def test_start_returns_yandex_link_without_client_configuration(monkeypatch):
    service = passport_auth.PassportLogin()
    remote = Mock(
        side_effect=[
            'window.__CSRF__ = "test-csrf";',
            {"track_id": "test-track", "csrf_token": "other"},
            {"link": LINK},
        ]
    )
    monkeypatch.setattr(service, "_request", remote)
    assert service.start() == LINK
    assert service._csrf == "test-csrf"
    assert remote.call_args.kwargs["data"]["magic_track_id"] == "test-track"
    service.close()
    assert service._track is None


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com/am/push/qrsecure?track_id=t&magic=m",
        "http://passport.yandex.ru/am/push/qrsecure?track_id=t&magic=m",
        "https://passport.yandex.ru.evil.test/am/push/qrsecure?track_id=t&magic=m",
        "https://passport.yandex.ru/other?track_id=t&magic=m",
        "https://user:pass@passport.yandex.ru/am/push/qrsecure?track_id=t&magic=m",
        "https://passport.yandex.ru/am/push/qrsecure?track_id=t",
    ],
)
def test_only_expected_yandex_confirmation_links_are_opened(link):
    with pytest.raises(auth.AuthError):
        passport_auth.validate_login_link(link)


def test_pending_does_not_exchange_token(monkeypatch):
    service = passport_auth.PassportLogin()
    service._track = {"track_id": "test"}
    remote = Mock(return_value={"state": "pending"})
    monkeypatch.setattr(service, "_request", remote)
    assert service.poll() is None
    assert remote.call_count == 1


def test_confirmed_session_token_is_validated_and_only_own_yandex_cookies_used(monkeypatch):
    service = passport_auth.PassportLogin()
    service._track = {"track_id": "test"}
    service.cookies = [
        SimpleNamespace(domain=".yandex.ru", name="Session_id", value="own-session"),
        SimpleNamespace(domain="evilyandex.ru", name="bad", value="exclude"),
    ]
    remote = Mock(
        side_effect=[
            {"state": "otp_auth_finished", "trackId": "finished-track"},
            {},
            {"access_token": "test-account-token"},
            {"status": "ok", "uid": "private"},
        ]
    )
    monkeypatch.setattr(service, "_request", remote)
    token = service.poll()
    assert token.access_token == "test-account-token"
    assert "test-account-token" not in repr(token)
    headers = remote.call_args_list[2].kwargs["headers"]
    assert headers["Ya-Client-Cookie"] == "Session_id=own-session"
    assert remote.call_args.kwargs["headers"]["Authorization"] == "OAuth test-account-token"


def test_unvalidated_token_is_not_returned(monkeypatch):
    service = passport_auth.PassportLogin()
    service._track = {"track_id": "test"}
    service.cookies = [SimpleNamespace(domain=".yandex.ru", name="Session_id", value="own")]
    monkeypatch.setattr(
        service,
        "_request",
        Mock(
            side_effect=[
                {"state": "otp_auth_finished", "trackId": "finished"},
                {},
                {"access_token": "secret"},
                {"status": "error"},
            ]
        ),
    )
    with pytest.raises(auth.AuthError):
        service.poll()


def test_direct_script_opens_yandex_and_saves_env_automatically(monkeypatch, tmp_path, capsys):
    service = Mock()
    service.start.return_value = LINK
    service.wait.return_value = auth.OAuthToken("test-account-token")
    monkeypatch.setattr(passport_auth, "PassportLogin", Mock(return_value=service))
    browser = Mock(return_value=True)
    monkeypatch.setattr(passport_auth.webbrowser, "open", browser)
    path = tmp_path / ".env"
    assert passport_auth.main([], default_env=path) == 0
    browser.assert_called_once_with(LINK)
    assert env_auth.file_token(path).access_token == "test-account-token"
    assert auth.get_token() == "test-account-token"
    output = capsys.readouterr()
    assert "test-account-token" not in output.out + output.err
    service.close.assert_called_once()


def test_cancel_leaves_old_env_and_clears_session(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    env_auth.save_env_token(path, auth.OAuthToken("old"))
    previous = path.read_bytes()
    service = Mock()
    service.start.return_value = LINK
    service.wait.side_effect = KeyboardInterrupt
    monkeypatch.setattr(passport_auth, "PassportLogin", Mock(return_value=service))
    assert passport_auth.main(["--no-browser"], default_env=path) == 130
    assert path.read_bytes() == previous
    service.close.assert_called_once()


def test_wait_cancelled_before_poll(monkeypatch):
    service = passport_auth.PassportLogin()
    poll = Mock()
    monkeypatch.setattr(service, "poll", poll)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(auth.AuthError, match="access_denied"):
        service.wait(cancel=cancelled)
    poll.assert_not_called()


def test_wait_has_deadline(monkeypatch):
    service = passport_auth.PassportLogin()
    poll = Mock(return_value=None)
    monkeypatch.setattr(service, "poll", poll)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(auth.AuthError, match="expired_token"):
        service.wait(timeout=5)
    assert now[0] == 5
    assert poll.call_count == 2


def test_http_errors_never_echo_response_tokens():
    service = passport_auth.PassportLogin()
    service.opener = Mock()
    service.opener.open.side_effect = HTTPError("url", 403, "secret", {}, io.BytesIO(b"secret"))
    with pytest.raises(auth.AuthError) as caught:
        service._request(passport_auth.PASSPORT + "/pwl-yandex")
    assert "secret" not in str(caught.value)


def test_redirect_cannot_forward_authenticated_request():
    redirect = passport_auth._PassportRedirect()
    for request, target in (
        (
            Request(passport_auth.MOBILE, headers={"Authorization": "OAuth secret"}),
            passport_auth.MOBILE,
        ),
        (Request(passport_auth.PASSPORT, data=b"private"), passport_auth.PASSPORT),
        (Request(passport_auth.PASSPORT), "https://example.com"),
    ):
        with pytest.raises(auth.AuthError):
            redirect.redirect_request(request, None, 302, "", {}, target)


def test_http_parser_rejects_nonobject_and_secret_errors():
    service = passport_auth.PassportLogin()
    for payload in ([], {"status": "error", "errors": ["secret"]}):
        service.opener = Mock(open=Mock(return_value=io.BytesIO(json.dumps(payload).encode())))
        with pytest.raises(auth.AuthError) as caught:
            service._request(passport_auth.PASSPORT)
        assert "secret" not in str(caught.value)
