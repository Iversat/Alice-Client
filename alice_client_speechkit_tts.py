"""Legacy filename for synthesis via the observed protocol, not the public SDK."""

import sys

from alice_client import YandexAliceClient, YandexAliceClientError
from alice_client.cli import main, save_audio_to_file

__all__ = ["YandexAliceClient", "YandexAliceClientError", "save_audio_to_file"]

if __name__ == "__main__":
    raise SystemExit(main(["tts", *sys.argv[1:]]))
