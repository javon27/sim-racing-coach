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
    download_model(MODEL_SIZE, output_dir=str(dest), revision=WHISPER_REVISION)
    # #114: verify against the pinned hashes, same as _default_kokoro_download --
    # same RuntimeError type, so ModelInstaller.status()["error"] surfaces it in
    # Settings and the Windows STT fallback applies, exactly as a Kokoro hash
    # mismatch already does.
    for name, (size, sha) in WHISPER_FILES.items():
        p = dest / name
        if not p.exists() or p.stat().st_size != size or _sha256_file(p) != sha:
            raise RuntimeError(f"{name}: hash mismatch -- download again")


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
        except Exception as e:
            self._error = str(e) or type(e).__name__

    def status(self) -> dict:
        installing = self._thread is not None and self._thread.is_alive()
        return {"engine": "whisper", "installed": (self.dest / "model.bin").exists(), "installing": installing,
                "progress_bytes": _dir_bytes(self.dest), "total_bytes": EXPECTED_MODEL_BYTES,
                "error": self._error}


# #114: pin the faster-whisper "small" model the same way as the Kokoro files
# below -- an unpinned `download_model` silently tracks the repo's default
# branch, so the model could change under the app without anyone noticing.
# Revision = the HF commit sha of Systran/faster-whisper-small looked up
# 2026-09-18 via `HfApi().model_info("Systran/faster-whisper-small").sha`
# (repo's only commit; lastModified 2023-11-23).
WHISPER_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
# (size, sha256) per file at that revision, computed once from a real
# `download_model(..., revision=WHISPER_REVISION)` in this worktree 2026-09-18.
# This is the exact file set faster-whisper's `allow_patterns` fetches that
# actually exist in the repo (no preprocessor_config.json at this revision).
WHISPER_FILES = {
    "config.json": (2370, "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"),
    "model.bin": (483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
    "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
}

KOKORO_FILES = {
    "kokoro-v1.0.onnx": (325_532_387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "voices-v1.0.bin": (28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
KOKORO_URL_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_TOTAL_BYTES = sum(size for size, _ in KOKORO_FILES.values())


def kokoro_dir(data_dir: str) -> Path:
    return Path(data_dir) / "kokoro"


def kokoro_installed(data_dir: str) -> bool:
    return all((kokoro_dir(data_dir) / name).exists() for name in KOKORO_FILES)


def _fetch_to(url: str, dest: Path) -> None:
    import urllib.request
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def _sha256_file(p: Path, chunk_size: int = 1 << 20) -> str:
    """sha256 of a file read in chunks -- `tmp.read_bytes()` loaded the whole
    325 MB ONNX file into memory just to hash it (#15). Same chunk size the
    download loop in `_fetch_to` already uses."""
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _default_kokoro_download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, (size, sha) in KOKORO_FILES.items():
        tmp = dest / (name + ".part")
        _fetch_to(KOKORO_URL_BASE + name, tmp)
        digest = _sha256_file(tmp)
        if digest != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{name}: sha256 mismatch (got {digest[:12]}…) — download again")
        tmp.replace(dest / name)


class KokoroInstaller(ModelInstaller):
    """Same state machine as the whisper installer, for the Kokoro model pair."""

    def __init__(self, data_dir: str, downloader: Callable[[Path], None] | None = None):
        super().__init__(data_dir, downloader or _default_kokoro_download)
        self.dest = kokoro_dir(data_dir)
        self.voices_fn: Callable[[], list[str]] | None = None   # set by app.py once the engine exists
        self._voices: list[str] | None = None   # memoised once populated -- loading the model just to list voices

    def start(self) -> bool:
        self._voices = None    # a fresh install may change the voice list; drop the memo
        return super().start()

    def status(self) -> dict:
        installing = self._thread is not None and self._thread.is_alive()
        installed = all((self.dest / n).exists() for n in KOKORO_FILES)
        voices: list[str] = self._voices or []
        if installed and not voices and self.voices_fn is not None:
            try:
                voices = self.voices_fn()
            except Exception:
                voices = []
            if voices:
                self._voices = voices   # avoid loading the model on every 1 Hz status poll
        return {"engine": "kokoro", "installed": installed, "installing": installing,
                "progress_bytes": _dir_bytes(self.dest), "total_bytes": KOKORO_TOTAL_BYTES,
                "error": self._error, "voices": voices}


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
