"""Offline, language-selected Vosk recognition; independent of Yandex ASR."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .audio import RATE
from .client import YandexAliceClientError

MODELS = {"ru": "vosk-model-small-ru-0.22", "en": "vosk-model-small-en-us-0.15"}
HASHES = {
    "ru": "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4",
    "en": "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498",
}


def model_root() -> Path:
    # A user-home cache is also visible from normal PowerShell outside MSIX virtualization.
    default = Path.home() / ".cache" / "alice-client" / "models"
    return Path(os.environ.get("ALICE_MODEL_DIR", default)).resolve()


def install_model(language: str, mirror: str = "official") -> Path:
    """Download only an explicitly selected official model; validate archive paths."""
    name = MODELS[language]
    root = model_root()
    target = root / name
    if (target / "am" / "final.mdl").is_file():
        return target
    if target.exists():
        raise ValueError("Папка модели уже существует, но не содержит am/final.mdl.")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=root) as tmp:
        directory = Path(tmp)
        archive = directory / "model.zip"
        url = f"https://alphacephei.com/vosk/models/{name}.zip"
        if mirror == "rhasspy":
            revision = "e7ac2109d134b5f2404ba95389b2fb51916d4cab"
            url = f"https://huggingface.co/rhasspy/vosk-models/resolve/{revision}/{language}/{name}.zip"
        elif mirror != "official":
            raise ValueError("Неизвестный источник модели.")
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response:
            with archive.open("wb") as output:
                total = 0
                while data := response.read(1024 * 1024):
                    total += len(data)
                    if total > 256 * 1024 * 1024:
                        raise ValueError("Модель превышает допустимый размер загрузки.")
                    digest.update(data)
                    output.write(data)
        if digest.hexdigest() != HASHES[language]:
            raise ValueError("Контрольная сумма модели не совпадает.")
        with zipfile.ZipFile(archive) as zipped:
            if sum(item.file_size for item in zipped.infolist()) > 512 * 1024 * 1024:
                raise ValueError("Распакованная модель слишком велика.")
            for item in zipped.infolist():
                path = (directory / item.filename).resolve()
                if not path.is_relative_to((directory / name).resolve()):
                    raise ValueError("Некорректный путь в архиве модели.")
            zipped.extractall(directory)
        if not (directory / name / "am" / "final.mdl").is_file():
            raise ValueError("Архив не содержит модель Vosk.")
        shutil.move(str(directory / name), str(target))
    return target


class LocalRecognizer:
    def __init__(self, language: str = "ru", model_path: Path | None = None):
        self.automatic = None
        if language == "auto":
            if model_path is not None:
                raise ValueError(
                    "--model требует --lang ru или --lang en; auto использует многоязычную модель."
                )
            from .multilingual_asr import MultilingualRecognizer

            self.automatic = MultilingualRecognizer()
            return
        try:
            import vosk
        except ImportError:
            raise YandexAliceClientError(
                'Для локального ASR установите pip install ".[voice]".'
            ) from None
        path = model_path or model_root() / MODELS[language]
        if not (path / "am" / "final.mdl").is_file():
            raise YandexAliceClientError(
                f"Нет модели {language}. Выполните: python -m alice_client models --lang {language}"
            )
        vosk.SetLogLevel(-1)
        self.model = vosk.Model(str(path))
        self.recognizer = vosk.KaldiRecognizer(self.model, RATE)

    def accept(self, chunk: bytes) -> str | None:
        if self.automatic is not None:
            return self.automatic.accept(chunk)
        if self.recognizer.AcceptWaveform(chunk):
            return json.loads(self.recognizer.Result()).get("text", "")
        return None

    def partial(self) -> str:
        if self.automatic is not None:
            return self.automatic.partial()
        return json.loads(self.recognizer.PartialResult()).get("partial", "")

    def finish(self) -> str:
        if self.automatic is not None:
            return self.automatic.finish()
        return json.loads(self.recognizer.FinalResult()).get("text", "")

    def reset(self) -> None:
        if self.automatic is not None:
            self.automatic.reset()
            return
        self.recognizer.Reset()
