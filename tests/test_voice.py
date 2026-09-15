import io
import wave

import numpy as np
import pytest

from simcoach.voice import MAX_SECONDS, VoiceInputError, decode_wav


def _wav(rate=16000, seconds=1.0, channels=1, width=2, freq=440.0) -> bytes:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    x = (0.5 * np.sin(2 * np.pi * freq * t))
    if width == 2:
        pcm = (x * 32767).astype("<i2")
    else:
        pcm = ((x * 127) + 128).astype("u1")
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_decode_16k_mono_passthrough():
    pcm, seconds = decode_wav(_wav())
    assert pcm.dtype == np.float32 and pcm.ndim == 1
    assert len(pcm) == 16000 and seconds == pytest.approx(1.0)
    assert pcm.max() == pytest.approx(0.5, abs=0.01)


def test_decode_resamples_48k_stereo_to_16k_mono():
    pcm, seconds = decode_wav(_wav(rate=48000, seconds=0.5, channels=2))
    assert len(pcm) == 8000 and seconds == pytest.approx(0.5)
    # a 440 Hz tone survives resampling: peak stays near 0.5
    assert pcm.max() == pytest.approx(0.5, abs=0.05)


def test_decode_8bit_scales_to_unit():
    pcm, _ = decode_wav(_wav(width=1))
    assert -1.0 <= pcm.min() and pcm.max() <= 1.0 and pcm.max() > 0.4


def test_decode_rejects_garbage_and_overlong():
    with pytest.raises(VoiceInputError):
        decode_wav(b"not a wav at all")
    with pytest.raises(VoiceInputError, match="longer"):
        decode_wav(_wav(rate=8000, seconds=MAX_SECONDS + 1))
