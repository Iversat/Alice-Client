"""Legacy entry point for text chat with an audio response."""

import sys

from alice_client import YandexAliceClient, YandexAliceClientError
from alice_client.cli import main, save_audio_to_file

__all__ = ["YandexAliceClient", "YandexAliceClientError", "save_audio_to_file"]

if __name__ == "__main__":
    raise SystemExit(main(["chat", "--audio", *sys.argv[1:]]))
