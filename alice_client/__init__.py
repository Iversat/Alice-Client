"""Independent research client for the observed Alice WebSocket protocol."""

from .auth import YandexAuth, get_token
from .client import (
    DEFAULT_SERVER_URL,
    AliceTimeoutError,
    YandexAliceClient,
    YandexAliceClientError,
)

__all__ = [
    "DEFAULT_SERVER_URL",
    "AliceTimeoutError",
    "YandexAliceClient",
    "YandexAliceClientError",
    "YandexAuth",
    "get_token",
]
__version__ = "0.4.3"
