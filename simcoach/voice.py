"""Voice input for the sidecar: WAV decode + a swappable speech-to-text
`Transcriber` (local faster-whisper now; a cloud provider later, same seam as
`simcoach.model.Model`). Nothing here touches the coach or the prompt."""
from __future__ import annotations

import io
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

TARGET_RATE = 16000
MAX_SECONDS = 30.0


class VoiceInputError(ValueError):
    """The audio body is not something we can transcribe (maps to HTTP 422)."""


def decode_wav(data: bytes) -> tuple[np.ndarray, float]:
    """PCM WAV bytes -> (float32 mono 16 kHz in [-1, 1], seconds of audio).

    Linear-interpolation resample: the input is speech from a headset mic, and
    whisper is tolerant; a windowed-sinc is not worth a dependency here."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, ValueError) as e:
        raise VoiceInputError(f"unreadable WAV: {e}") from e
    if rate <= 0 or channels <= 0:
        raise VoiceInputError("unreadable WAV: bad header")
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128.0) / 128.0
    else:
        raise VoiceInputError(f"unsupported sample width {width}")
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    seconds = len(x) / rate
    if seconds > MAX_SECONDS:
        raise VoiceInputError(f"audio longer than {MAX_SECONDS:.0f} s")
    if rate != TARGET_RATE and len(x) > 1:
        m = round(len(x) * TARGET_RATE / rate)
        x = np.interp(np.linspace(0.0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
    return np.ascontiguousarray(x, dtype=np.float32), float(seconds)


MODEL_SIZE = "small"
EXPECTED_MODEL_BYTES = 250_000_000   # ponytail: bar estimate; the hub has no cheap total

# Biases whisper's decoder toward sim-racing vocabulary it otherwise mishears
# (e.g. "mic check" -> "check my check my check"). Natural sentences, not a
# bare word list -- faster-whisper conditions on it as prior text.
WHISPER_PROMPT = (
    "Mic check. Nestor, where am I losing time? Brake bias, brake balance, "
    "understeer, oversteer, trail braking, lockup, apex, Eau Rouge, "
    "Mulsanne, Turn 4, T4, lap time, tire temps, pit stop, fuel."
)


class NotInstalled(RuntimeError):
    """The STT model is not on disk yet (maps to HTTP 409)."""


class Transcriber(Protocol):
    def transcribe(self, pcm16k: np.ndarray) -> str: ...


class FakeTranscriber:
    def __init__(self, text: str = "", installed: bool = True):
        self.text, self.installed, self.calls = text, installed, []

    def transcribe(self, pcm16k: np.ndarray) -> str:
        if not self.installed:
            raise NotInstalled("voice isn't set up yet — enable it in Settings")
        self.calls.append(pcm16k)
        return self.text


def model_dir(data_dir: str) -> Path:
    return Path(data_dir) / "whisper" / MODEL_SIZE


def model_installed(data_dir: str) -> bool:
    return (model_dir(data_dir) / "model.bin").exists()


def migrate_model_dir(old_root: str, new_root: str) -> bool:
    """One-time move of `<old_root>/whisper` to `<new_root>/whisper`. The first
    release keyed the model off the telemetry dir by accident; the model belongs
    at the app-data root next to the config. Best-effort: returns True only when a
    move happened; never raises (a failed move just means a re-download)."""
    old, new = Path(old_root) / "whisper", Path(new_root) / "whisper"
    if old == new or not old.is_dir() or new.exists():
        return False
    try:
        import shutil
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        return True
    except OSError:
        return False


def _default_download(dest: Path) -> None:
    from faster_whisper import download_model  # optional extra; imported only here
    dest.mkdir(parents=True, exist_ok=True)
    download_model(MODEL_SIZE, output_dir=str(dest))


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


class ModelInstaller:
    """One background download at a time; status is polled by the UI."""

    def __init__(self, data_dir: str, downloader: Callable[[Path], None] | None = None):
        self.dest = model_dir(data_dir)
        self._download = downloader or _default_download
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._error = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            self._download(self.dest)
        except Exception as e:      # noqa: BLE001 -- surfaced verbatim to Settings; never crashes the sidecar
            self._error = str(e) or type(e).__name__

    def status(self) -> dict:
        installing = self._thread is not None and self._thread.is_alive()
        return {"installed": (self.dest / "model.bin").exists(), "installing": installing,
                "progress_bytes": _dir_bytes(self.dest), "total_bytes": EXPECTED_MODEL_BYTES,
                "error": self._error}


class WhisperTranscriber:
    """faster-whisper `small`, int8, CPU. The model loads on first use (~2 s) and
    stays resident; one transcription at a time."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._model = None
        self._lock = threading.Lock()

    def transcribe(self, pcm16k: np.ndarray) -> str:
        if not model_installed(self.data_dir):
            raise NotInstalled("voice isn't set up yet — enable it in Settings")
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(str(model_dir(self.data_dir)), device="cpu", compute_type="int8")
            segments, _info = self._model.transcribe(
                pcm16k, language="en", beam_size=5, vad_filter=True, initial_prompt=WHISPER_PROMPT
            )
            return " ".join(s.text.strip() for s in segments).strip()
