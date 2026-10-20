"""Tests must never read the developer's credential vault or token environment."""

import pytest

from alice_client import auth


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    values = {}

    class MemoryVault:
        def get_password(self, service, account):
            return values.get((service, account))

        def set_password(self, service, account, value):
            values[service, account] = value

        def delete_password(self, service, account):
            values.pop((service, account), None)

    original = auth.TokenStore.__init__
    monkeypatch.setattr(
        auth.TokenStore,
        "__init__",
        lambda self, backend=None: original(self, backend or MemoryVault()),
    )
    for name in (
        "YANDEX_OAUTH_TOKEN",
        "YANDEX_CLIENT_ID",
        "YANDEX_CLIENT_SECRET",
        "YANDEX_ASR_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
