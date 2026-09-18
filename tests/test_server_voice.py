import io
import wave
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.tts_kokoro import EngineUnavailable
from simcoach.voice import FakeTranscriber, ModelInstaller


def _wav(rate=16000, seconds=1.0) -> bytes:
    n = int(rate * seconds)
    pcm = (0.3 * np.sin(np.arange(n) * 0.1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _app(tmp_path, monkeypatch, transcriber=None, installer=None, kokoro_installer=None, kokoro_engine=None):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()), transcriber=transcriber, installer=installer,
                      kokoro_installer=kokoro_installer, kokoro_engine=kokoro_engine)
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


def test_transcribe_413_from_declared_content_length_before_reading(tmp_path, monkeypatch):
    # #12: the old code read the whole body THEN compared it to VOICE_MAX_BODY --
    # the cap bounded what got decoded, not what got allocated. Declare a
    # Content-Length above the cap while the actual body sent is small and
    # unreadable as WAV: if the check runs before the read, this 413s; if it
    # (still) reads first, the small body decodes to a 422 instead.
    c, _ = _app(tmp_path, monkeypatch, transcriber=FakeTranscriber(text="x"))
    r = c.post("/voice/transcribe", content=b"nope",
              headers={"content-type": "audio/wav", "content-length": "9000000"})
    assert r.status_code == 413


def test_default_transcriber_is_whisper_and_409s_without_a_model(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch)          # no fake: the real WhisperTranscriber
    assert _post_wav(c, _wav()).status_code == 409


def test_voice_model_lives_at_the_app_data_root_and_migrates_from_telemetry(tmp_path, monkeypatch):
    # First release keyed the model off the store's telemetry dir (<data>/whisper);
    # it belongs next to the config. An existing install is moved, not re-downloaded.
    old = tmp_path / "data" / "whisper" / "small"
    old.mkdir(parents=True)
    (old / "model.bin").write_bytes(b"x")
    c, _ = _app(tmp_path, monkeypatch)          # SIMCOACH_DATA_DIR = tmp_path
    assert (tmp_path / "whisper" / "small" / "model.bin").exists()
    assert not old.exists()
    assert c.get("/voice/status").json()["installed"] is True


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


class FakeEngine:
    def __init__(self, ok=True): self.ok, self.calls = ok, []
    def available(self): return self.ok
    def voices(self): return ["af_heart", "am_michael"]
    def synthesize(self, text, voice, speed, table=None):
        if not self.ok:
            raise EngineUnavailable("model missing")
        self.calls.append((text, voice, speed, table))
        return b"RIFF" + b"\0" * 40


def test_speak_returns_wav_and_passes_voice_speed_and_table(tmp_path, monkeypatch):
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    (Path(c.app.state.tracks_dir) / "circuit_de_spa_francorchamps.json")  # shipped file has no block yet
    r = c.post("/voice/speak", json={"text": "Turn 10", "voice": "am_michael", "speed": 1.2,
                                     "track_slug": "circuit_de_spa_francorchamps"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert eng.calls[0][:3] == ("Turn 10", "am_michael", 1.2)


def test_speak_track_display_name_also_resolves_the_table(tmp_path, monkeypatch):
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    r = c.post("/voice/speak", json={"text": "Turn 10", "track": "Circuit de Spa-Francorchamps"})
    assert r.status_code == 200
    assert eng.calls[0][:2] == ("Turn 10", "af_heart")   # default voice


def test_speak_400_on_empty_409_when_not_installed_503_when_engine_fails(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=FakeEngine(ok=False))
    assert c.post("/voice/speak", json={"text": "  "}).status_code == 400
    assert c.post("/voice/speak", json={"text": "hi"}).status_code == 409
    bad = FakeEngine(); bad.synthesize = lambda *a, **k: (_ for _ in ()).throw(EngineUnavailable("onnx failed"))
    c2, _ = _app(tmp_path, monkeypatch, kokoro_engine=bad)
    r = c2.post("/voice/speak", json={"text": "hi"})
    assert r.status_code == 503 and "onnx failed" in r.json()["detail"]


def test_speak_422_on_non_numeric_speed_not_500(tmp_path, monkeypatch):
    # #13: float(body["speed"]) on a non-numeric string raised ValueError, which
    # FastAPI turns into an uncaught 500 rather than a curated 422.
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=FakeEngine())
    r = c.post("/voice/speak", json={"text": "hi", "speed": "fast"})
    assert r.status_code == 422


def test_speak_second_call_with_same_body_hits_the_disk_cache(tmp_path, monkeypatch):
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    body = {"text": "Turn 10", "voice": "am_michael", "speed": 1.2}
    r1 = c.post("/voice/speak", json=body)
    r2 = c.post("/voice/speak", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    assert len(eng.calls) == 1


def test_speak_cache_disabled_when_tts_cache_mb_is_zero(tmp_path, monkeypatch):
    from simcoach import config
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    config.save_config({"voice": {"tts_cache_mb": 0}})
    body = {"text": "Turn 10"}
    c.post("/voice/speak", json=body)
    c.post("/voice/speak", json=body)
    assert len(eng.calls) == 2


def test_tts_cache_stats_and_clear_endpoints(tmp_path, monkeypatch):
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    assert c.get("/voice/tts-cache").json() == {"files": 0, "bytes": 0}
    c.post("/voice/speak", json={"text": "Turn 10"})
    stats = c.get("/voice/tts-cache").json()
    assert stats["files"] == 1 and stats["bytes"] > 0
    r = c.request("DELETE", "/voice/tts-cache", json={"confirm": True})
    assert r.status_code == 200 and r.json() == {"removed": 1}
    assert c.get("/voice/tts-cache").json() == {"files": 0, "bytes": 0}


def test_tts_cache_delete_without_confirm_400s(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=FakeEngine())
    assert c.request("DELETE", "/voice/tts-cache", json={}).status_code == 400


def test_speak_still_returns_wav_with_the_fake_engine(tmp_path, monkeypatch):
    # The executor-moved prelude (load_config/_live_track/_track_file_data) must
    # still resolve a valid speed and reach synthesize normally.
    eng = FakeEngine()
    c, _ = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    r = c.post("/voice/speak", json={"text": "hi", "speed": 1.1})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert eng.calls[0][2] == 1.1


def test_kokoro_setup_and_status_endpoints(tmp_path, monkeypatch):
    class Inst:
        started = False
        def start(self): self.started = True; return True
        def status(self): return {"engine": "kokoro", "installed": False, "installing": self.started,
                                  "progress_bytes": 0, "total_bytes": 1, "error": None, "voices": []}
    c, _ = _app(tmp_path, monkeypatch, kokoro_installer=Inst())
    assert c.post("/voice/kokoro/setup", json={}).json()["started"] is True
    assert c.get("/voice/kokoro/status").json()["engine"] == "kokoro"


class _StatusInst:
    def start(self): return True
    def status(self): return {"engine": "kokoro", "installed": False, "installing": False,
                              "progress_bytes": 0, "total_bytes": 1, "error": None, "voices": []}


def test_kokoro_status_reports_gpl_free_true_in_a_clean_env(tmp_path, monkeypatch):
    c, _ = _app(tmp_path, monkeypatch, kokoro_installer=_StatusInst())
    assert c.get("/voice/kokoro/status").json()["gpl_free"] is True


def test_kokoro_status_gpl_free_false_when_a_real_gpl_module_is_loaded(tmp_path, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("phonemizer")   # no __simcoach_stub__ marker -> looks real
    monkeypatch.setitem(sys.modules, "phonemizer", fake)
    c, _ = _app(tmp_path, monkeypatch, kokoro_installer=_StatusInst())
    assert c.get("/voice/kokoro/status").json()["gpl_free"] is False


def _ask(c, body):
    with c.websocket_connect("/coach", headers={"host": "127.0.0.1",
                                                 "origin": "http://127.0.0.1:8765"}) as ws:
        ws.send_json(body)
        while True:
            ev = ws.receive_json()
            if ev["type"] in ("done", "error"):
                return ev


def test_ptt_strips_spoken_line_and_sends_it_on_done(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Brake later.\n\nMore detail.\n\n[spoken] Brake later into four.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "where am I losing time", "voice": "ptt"})
    assert ev["type"] == "done"
    assert ev["spoken"] == "Brake later into four."
    assert ev["answer"] == "Brake later.\n\nMore detail."
    assert "VOICE:" in model.last_system
    rows = store.chat_messages(ev["chat_id"])
    assert rows[0]["question"] == "🎤 where am I losing time"
    assert rows[0]["answer"] == "Brake later.\n\nMore detail."      # never stored with the tag
    assert rows[0]["spoken"] == "Brake later into four."


def _ask_all(c, body):
    """Like _ask, but returns every event up to and including done/error."""
    with c.websocket_connect("/coach", headers={"host": "127.0.0.1",
                                                 "origin": "http://127.0.0.1:8765"}) as ws:
        ws.send_json(body)
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                return events


def test_ptt_spoken_streams_before_text_and_is_never_in_text_events(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="[spoken] Brake later into four.\n\nFull answer.\n\nMore.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask_all(c, {"question": "q", "voice": "ptt"})
    spoken_idx = next(i for i, e in enumerate(events) if e["type"] == "spoken")
    text_idxs = [i for i, e in enumerate(events) if e["type"] == "text"]
    assert all(spoken_idx < i for i in text_idxs)
    joined_text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "[spoken]" not in joined_text
    assert events[spoken_idx]["text"] == "Brake later into four."
    done = events[-1]
    assert done["type"] == "done"
    assert done["answer"] == "Full answer.\n\nMore."


def test_ptt_spoken_streams_mid_line_after_a_preamble(tmp_path, monkeypatch):
    # #120: the tag can land mid-line, after a preamble with no newline before it
    # (no tool round happened to trigger #133's separating blank line, so this is
    # still all one streamed text block/delta). The preamble is emitted as its own
    # text event before the spoken event, and the tag never leaks into any text.
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Preamble. [spoken] Cue here.\n\nBody")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask_all(c, {"question": "q", "voice": "ptt"})
    spoken_idx = next(i for i, e in enumerate(events) if e["type"] == "spoken")
    assert events[spoken_idx]["text"] == "Cue here."
    joined_text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "[spoken]" not in joined_text
    assert joined_text == "Preamble. Body"
    done = events[-1]
    assert done["type"] == "done"
    assert done["answer"] == "Preamble.\n\nBody"
    assert done["spoken"] == "Cue here."


def test_ptt_spoken_row_152_preamble_then_tool_then_tag(tmp_path, monkeypatch):
    # #120's real rig payload (chat row 152, Spa): the model writes a preamble
    # with no newline, calls a tool, then opens the next block with the tag.
    # #133 inserts a "\n\n" separator between those two text blocks, so by the
    # time the full answer is assembled the tag is at a line start and both the
    # stored answer and spoken cue come out clean.
    # ponytail: the LIVE per-chunk `spoken` event still doesn't fire early here
    # (the buffer resolves off #133's separator, one delta before the tag's own
    # block arrives) -- the tag can flash in a text delta mid-stream before
    # `done` corrects it. Only the final `done.answer`/`done.spoken` are
    # guaranteed clean by this interim fix. Fix properly if/when #119 lands.
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(stream_script=[
        ("text", "I'll pull up what we know about Spa and give you a walkthrough."),
        ("tool", "get_corner", {"corner": "1"}),
        ("text", ("[spoken] Welcome to Spa, mind the Eau Rouge exit kerb.\n\n"
                   "Big picture: strong straights, tricky Eau Rouge.")),
    ])
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "q", "voice": "ptt"})
    assert ev["type"] == "done"
    assert ev["answer"] == ("I'll pull up what we know about Spa and give you a walkthrough.\n\n"
                             "Big picture: strong straights, tricky Eau Rouge.")
    assert ev["spoken"] == "Welcome to Spa, mind the Eau Rouge exit kerb."
    assert "[spoken]" not in ev["answer"]


def test_ptt_no_spoken_event_when_tag_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="First para.\n\nSecond.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    events = _ask_all(c, {"question": "q", "voice": "ptt"})
    assert not any(e["type"] == "spoken" for e in events)
    joined_text = "".join(e["text"] for e in events if e["type"] == "text")
    assert joined_text == "First para.\n\nSecond."
    done = events[-1]
    assert done["spoken"] == "First para."


def test_ptt_falls_back_to_first_paragraph_when_tag_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    c = TestClient(create_app(store, Coach(FakeModel(reply="First para.\n\nSecond."))), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "q", "voice": "ptt"})
    assert ev["spoken"] == "First para." and ev["answer"] == "First para.\n\nSecond."


def test_conversation_and_typed_add_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    model = FakeModel(reply="Answer.")
    c = TestClient(create_app(store, Coach(model)), base_url="http://127.0.0.1")
    ev = _ask(c, {"question": "q", "voice": "conversation"})
    assert "spoken" not in ev and ev["answer"] == "Answer." and "VOICE:" not in model.last_system
    assert store.chat_messages(ev["chat_id"])[0]["question"] == "🎤 q"
    assert store.chat_messages(ev["chat_id"])[0]["spoken"] is None
    ev = _ask(c, {"question": "typed", "voice": "bogus"})
    assert "spoken" not in ev and store.chat_messages(ev["chat_id"])[0]["question"] == "typed"
    assert store.chat_messages(ev["chat_id"])[0]["spoken"] is None
