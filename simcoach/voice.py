"""Voice input for the sidecar: WAV decode + a swappable speech-to-text
`Transcriber` (local faster-whisper now; a cloud provider later, same seam as
`simcoach.model.Model`). Nothing here touches the coach or the prompt."""
from __future__ import annotations

import io
import wave

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
        m = int(round(len(x) * TARGET_RATE / rate))
        x = np.interp(np.linspace(0.0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
    return np.ascontiguousarray(x, dtype=np.float32), float(seconds)
