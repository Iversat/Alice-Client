"""Independent research client for the observed Alice WebSocket protocol."""

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
]
__version__ = "0.2.0"
