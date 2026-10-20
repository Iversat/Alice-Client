"""Explicit live checks. Run from the project folder; credentials are never reported."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from alice_client import YandexAliceClient, get_token
from alice_client.cli import response_text


async def verify(output):
    report = {"checked_at": datetime.now(UTC).isoformat(), "checks": []}
    output.mkdir(parents=True, exist_ok=True)
    for mode in ["anonymous", "token"]:
        token = get_token(anonymous=mode == "anonymous")
        if mode == "token" and not token:
            report["checks"].append({"mode": mode, "skipped": "No account token"})
            continue
        for kind in ["single", "session", "tts"]:
            entry = {"mode": mode, "kind": kind, "ok": False}
            try:
                async with YandexAliceClient(oauth_token=token, request_timeout=20) as client:
                    if kind == "single":
                        result = await client.send_text("Привет")
                        entry["answer"] = response_text(
                            result["response_payload"], require_text=True
                        )
                    elif kind == "session":
                        entry["turns"] = []
                        session = None
                        for question in ["Давай сыграем в города", "Москва", "Хватит"]:
                            result = await client.send_text(question)
                            entry["turns"].append(
                                {
                                    "question": question,
                                    "answer": response_text(
                                        result["response_payload"], require_text=True
                                    ),
                                    "same_server_session": bool(
                                        session and session == client._session_id
                                    ),
                                }
                            )
                            session = client._session_id
                    else:
                        entry["files"] = []
                        for number, text in enumerate(["Привет", "Это проверка синтеза речи."]):
                            audio = await client.tts(text)
                            name = f"{mode}-tts-{number}.opus"
                            (output / name).write_bytes(audio)
                            entry["files"].append(
                                {
                                    "file": name,
                                    "bytes": len(audio),
                                    "ogg": audio.startswith(b"OggS"),
                                }
                            )
                    entry["ok"] = True
            except Exception as error:
                entry["error_type"] = type(error).__name__
            report["checks"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            await asyncio.sleep(1)
    (output / "services.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if all(check.get("ok") for check in report["checks"]) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("responses/verification"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(verify(args.output)))
