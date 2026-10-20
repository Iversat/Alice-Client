"""Local RU/EN transcription with Whisper; language is selected per utterance."""

from __future__ import annotations

import sys
from array import array

from .audio import RATE
from .client import YandexAliceClientError
from .local_asr import model_root

REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]


def install_multilingual():
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise YandexAliceClientError(
            'Установите pip install ".[voice]" для режима RU/EN.'
        ) from None
    path = model_root() / "whisper-base"
    snapshot_download(
        "Systran/faster-whisper-base",
        revision=REVISION,
        local_dir=path,
        allow_patterns=FILES,
        token=False,
    )
    return path


class MultilingualRecognizer:
    def __init__(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise YandexAliceClientError(
                'Обновите голосовые зависимости: pip install ".[voice]".'
            ) from None
        path = model_root() / "whisper-base"
        if not all((path / file).is_file() for file in FILES):
            raise YandexAliceClientError(
                "Нет многоязычной модели. Выполните: python -m alice_client models --lang auto"
            )
        self.model = WhisperModel(
            str(path), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True
        )
        self.last_language = None
        self.reset()

    def reset(self):
        self.buffer = bytearray()
        self.voiced = 0
        self.quiet = 0

    def accept(self, chunk: bytes) -> str | None:
        if len(chunk) % 2:
            raise ValueError("Ожидается PCM16 с чётной длиной.")
        pcm = array("h", chunk)
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm:
            return None
        self.buffer.extend(chunk)
        if sum(sample * sample for sample in pcm) / len(pcm) >= 120**2:
            self.voiced += len(pcm)
            self.quiet = 0
        else:
            self.quiet += len(pcm)
        if not self.voiced:
            # Keep a short lead-in, not an ever-growing recording of room silence.
            self.buffer = self.buffer[-int(RATE * 0.4) * 2 :]
        if (self.voiced >= RATE * 0.1 and self.quiet >= RATE * 0.9) or len(
            self.buffer
        ) >= RATE * 20 * 2:
            return self.finish()
        return None

    def partial(self):
        # Whisper produces a final phrase after a pause, not Vosk-style partials.
        return ""

    def finish(self):
        if self.voiced < RATE * 0.1:
            self.reset()
            return ""
        import numpy as np

        audio = np.frombuffer(bytes(self.buffer), dtype="<i2").astype(np.float32) / 32768
        self.reset()
        _, _, probabilities = self.model.detect_language(audio)
        language = max((p for p in probabilities if p[0] in {"ru", "en"}), key=lambda p: p[1])[0]
        segments, _ = self.model.transcribe(
            audio,
            language=language,
            beam_size=3,
            condition_on_previous_text=False,
            temperature=0,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        self.last_language = language
        return text
