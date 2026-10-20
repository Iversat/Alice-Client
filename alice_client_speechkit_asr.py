"""Legacy entry point for microphone recognition."""

import sys

from alice_client.asr import YandexASRClient, YandexASRClientError
from alice_client.cli import main

__all__ = ["YandexASRClient", "YandexASRClientError"]

if __name__ == "__main__":
    raise SystemExit(main(["asr", *sys.argv[1:]]))
