"""Guard against the PyInstaller spec silently dropping the voice-model collects
(#104) -- faster_whisper/ctranslate2 are lazy-imported in simcoach/voice.py so a
missing collect_all() ships an exe that ImportErrors on first /voice/transcribe."""
from pathlib import Path

SPEC = (Path(__file__).parent.parent / "packaging" / "simcoach-sidecar.spec").read_text()


def test_spec_collects_faster_whisper():
    assert 'collect_all("faster_whisper")' in SPEC


def test_spec_collects_ctranslate2():
    assert 'collect_all("ctranslate2")' in SPEC


def test_spec_collects_kokoro_and_excludes_gpl_packages():
    src = Path("packaging/simcoach-sidecar.spec").read_text(encoding="utf-8")
    for pkg in ("kokoro_onnx", "misaki", "onnxruntime"):
        assert f'collect_all("{pkg}")' in src
    for pkg in ("spacy", "phonemizer", "espeakng_loader", "num2words"):
        assert f'"{pkg}"' in src.split("excludes=", 1)[1].split("]", 1)[0]


def test_voice_extra_pins_kokoro_and_never_lists_gpl_packages():
    src = Path("pyproject.toml").read_text(encoding="utf-8")
    extra = src.split("voice = [", 1)[1].split("]", 1)[0]
    for pin in ('"kokoro-onnx==0.6.1"', '"misaki==0.7.4"', '"onnxruntime==1.30.0"', '"regex'):
        assert pin in extra
    for bad in ("spacy", "phonemizer", "espeakng", "num2words"):
        assert bad not in extra
