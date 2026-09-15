import io
import wave
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.voice import FakeTranscriber, ModelInstaller


def _wav(rate=16000, seconds=1.0) -> bytes:
    n = int(rate * seconds)
    pcm = (0.3 * np.sin(np.arange(n) * 0.1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _app(tmp_path, monkeypatch, transcriber=None, installer=None):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), transcriber=transcriber, installer=installer)
    return TestClient(app, base_url="http://127.0.0.1"), store


def _post_wav(c, data: bytes):
    return c.post("/voice/transcribe", content=data, headers={"content-type": "audio/wav"})


def test_transcribe_returns_text_and_seconds(tmp_path, monkeypatch):
    t = FakeTranscriber(text="where am I losing time")
    c, _ = _app(tmp_path, monkeypatch, transcriber=t)
    r = _post_wav(c, _wav(seconds=1.5))
    assert r.status_code == 200
    assert r.json() == {"text": "where am I losing time", "seconds": 1.5}
    assert len(t.calls) == 1 and len(t.calls[0]) == 24000   # decoded to 16 kHz before the seam


def test_transcribe_409_before_setup(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(installed=False))
    r = _post_wav(c, _wav())
    assert r.status_code == 409 and "Settings" in r.json()["detail"]


def test_transcribe_422_on_garbage_and_overlong(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(text="x"))
    assert _post_wav(c, b"nope").status_code == 422
    assert _post_wav(c, _wav(rate=8000, seconds=31)).status_code == 422


def test_transcribe_413_on_oversize_body(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(text="x"))
    assert _post_wav(c, b"\0" * 8_000_001).status_code == 413


def test_default_transcriber_is_whisper_and_409s_without_a_model(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch)          # no fake: the real WhisperTranscriber
    assert _post_wav(c, _wav()).status_code == 409


def test_setup_starts_download_and_status_tracks_it(tmp_path, monkeypatch):
    def fake_download(dest: Path):
        dest.mkdir(parents=True, exist_ok=True); (dest / "model.bin").write_bytes(b"0" * 10)
    inst = ModelInstaller(str(tmp_path), downloader=fake_download)
    c, _ = _app(tmp_path, monkeypatch, installer=inst)
    assert c.get("/voice/status").json()["installed"] is False
    r = c.post("/voice/setup", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    import time
    for _ in range(50):
        if not c.get("/voice/status").json()["installing"]:
            break
        time.sleep(0.05)
    assert c.get("/voice/status").json()["installed"] is True


def test_setup_requires_json_body(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, installer=ModelInstaller(str(tmp_path), downloader=lambda d: None))
    assert c.post("/voice/setup").status_code == 422    # CSRF guard, same as /ingest
