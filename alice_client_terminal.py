"""Legacy entry point for text chat; use python -m alice_client chat."""

import sys

from alice_client.cli import main
from alice_client.sync import YandexAliceClientPython

__all__ = ["YandexAliceClientPython", "main_terminal_chat"]


def main_terminal_chat() -> int:
    return main(["chat", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main_terminal_chat())
