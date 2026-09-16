import io
import sys
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from simcoach.voice import (
    MAX_SECONDS,
    WHISPER_PROMPT,
    FakeTranscriber,
    KokoroInstaller,
    ModelInstaller,
    NotInstalled,
    VoiceInputError,
    WhisperTranscriber,
    decode_wav,
    model_dir,
    model_installed,
)


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


def test_fake_transcriber_records_and_can_be_uninstalled():
    t = FakeTranscriber(text="brake later")
    assert t.transcribe(np.zeros(16000, np.float32)) == "brake later" and len(t.calls) == 1
    with pytest.raises(NotInstalled):
        FakeTranscriber(installed=False).transcribe(np.zeros(10, np.float32))


def test_migrate_model_dir_moves_once_and_never_clobbers(tmp_path):
    from simcoach.voice import migrate_model_dir
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "whisper" / "small").mkdir(parents=True)
    (old / "whisper" / "small" / "model.bin").write_bytes(b"x")
    assert migrate_model_dir(str(old), str(new)) is True
    assert (new / "whisper" / "small" / "model.bin").exists() and not (old / "whisper").exists()
    assert migrate_model_dir(str(old), str(new)) is False          # nothing left to move
    (old / "whisper").mkdir()
    assert migrate_model_dir(str(old), str(new)) is False          # target exists: untouched
    assert migrate_model_dir(str(new), str(new)) is False          # same dir: no-op


def test_model_installed_means_model_bin_exists(tmp_path):
    assert model_installed(str(tmp_path)) is False
    d = model_dir(str(tmp_path)); d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")
    assert model_installed(str(tmp_path)) is True


def test_whisper_transcriber_raises_not_installed_without_importing_whisper(tmp_path):
    # No faster_whisper on the test box may be the case; the 409 path must not need it.
    with pytest.raises(NotInstalled):
        WhisperTranscriber(str(tmp_path)).transcribe(np.zeros(160, np.float32))


def test_installer_runs_downloader_and_reports_progress(tmp_path):
    def fake_download(dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "model.bin").write_bytes(b"0" * 1000)
    inst = ModelInstaller(str(tmp_path), downloader=fake_download)
    assert inst.status()["installed"] is False
    assert inst.start() is True
    for _ in range(50):
        if not inst.status()["installing"]:
            break
        time.sleep(0.05)
    s = inst.status()
    assert s["installed"] is True and s["installing"] is False and s["error"] is None
    assert s["progress_bytes"] >= 1000 and s["total_bytes"]


def test_installer_reports_error_and_is_restartable(tmp_path):
    calls = []
    def bad(dest: Path):
        calls.append(1)
        raise OSError("no network")
    inst = ModelInstaller(str(tmp_path), downloader=bad)
    inst.start()
    for _ in range(50):
        if not inst.status()["installing"]:
            break
        time.sleep(0.05)
    assert inst.status()["error"] == "no network" and inst.status()["installed"] is False
    assert inst.start() is True and len(calls) >= 1  # a failed install can be retried


def test_whisper_transcriber_uses_beam_size_and_racing_prompt(tmp_path, monkeypatch):
    d = model_dir(str(tmp_path))
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")

    captured = {}

    class Segment:
        text = "mic check"

    class StubWhisperModel:
        def __init__(self, *_a, **_kw):
            pass

        def transcribe(self, _pcm16k, **kw):
            captured.update(kw)
            return [Segment()], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=StubWhisperModel))

    result = WhisperTranscriber(str(tmp_path)).transcribe(np.zeros(160, np.float32))

    assert result == "mic check"
    assert captured["beam_size"] == 5
    assert captured["initial_prompt"] == WHISPER_PROMPT
    assert captured["vad_filter"] is True
    assert captured["language"] == "en"


def test_installer_start_is_idempotent_while_running(tmp_path):
    gate = threading.Event()
    def slow(dest: Path):
        gate.wait(2)
    inst = ModelInstaller(str(tmp_path), downloader=slow)
    assert inst.start() is True
    assert inst.start() is False   # already running
    gate.set()


def test_kokoro_installer_downloads_both_files_and_reports(tmp_path):
    def fake_download(dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kokoro-v1.0.onnx").write_bytes(b"0" * 100)
        (dest / "voices-v1.0.bin").write_bytes(b"0" * 10)
    inst = KokoroInstaller(str(tmp_path), downloader=fake_download)
    assert inst.status()["installed"] is False and inst.status()["engine"] == "kokoro"
    assert inst.start() is True
    for _ in range(50):
        if not inst.status()["installing"]:
            break
        time.sleep(0.05)
    s = inst.status()
    assert s["installed"] is True and s["error"] is None and s["total_bytes"] == 325_532_387 + 28_214_398


def test_kokoro_default_download_verifies_sha256(tmp_path, monkeypatch):
    from simcoach import voice as v
    served = {"kokoro-v1.0.onnx": b"abc", "voices-v1.0.bin": b"def"}
    monkeypatch.setattr(v, "_fetch_to", lambda url, dest: dest.write_bytes(served[url.rsplit("/", 1)[1]]))
    with pytest.raises(RuntimeError, match="sha256"):
        v._default_kokoro_download(tmp_path / "kokoro")
