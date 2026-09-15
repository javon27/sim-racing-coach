"""Guard against the PyInstaller spec silently dropping the voice-model collects
(#104) -- faster_whisper/ctranslate2 are lazy-imported in simcoach/voice.py so a
missing collect_all() ships an exe that ImportErrors on first /voice/transcribe."""
from pathlib import Path

SPEC = (Path(__file__).parent.parent / "packaging" / "simcoach-sidecar.spec").read_text()


def test_spec_collects_faster_whisper():
    assert 'collect_all("faster_whisper")' in SPEC


def test_spec_collects_ctranslate2():
    assert 'collect_all("ctranslate2")' in SPEC
