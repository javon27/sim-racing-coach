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
    for pkg in ("spacy", "phonemizer", "espeakng_loader", "num2words", "av"):
        assert f'"{pkg}"' in src.split("excludes=", 1)[1].split("]", 1)[0]


def test_voice_extra_pins_kokoro_and_never_lists_gpl_packages():
    src = Path("pyproject.toml").read_text(encoding="utf-8")
    extra = src.split("voice = [", 1)[1].split("]", 1)[0]
    for pin in ('"kokoro-onnx==0.6.1"', '"misaki==0.7.4"', '"onnxruntime==1.30.0"', '"regex'):
        assert pin in extra
    for bad in ("spacy", "phonemizer", "espeakng", "num2words"):
        assert bad not in extra


def test_spec_bundles_the_vendored_cmudict_and_its_licence():
    # #148: tts_kokoro reads simcoach/cmudict/cmudict.dict at runtime; LICENSE must ship with it.
    assert '"simcoach", "cmudict"' in SPEC and '"simcoach/cmudict"' in SPEC
    assert (Path(__file__).parent.parent / "simcoach" / "cmudict" / "LICENSE").is_file()


def test_faster_whisper_imports_with_pyav_blocked(monkeypatch):
    # #221: the frozen sidecar excludes PyAV (its wheel bundles GPL x264/x265). With real
    # PyAV unimportable, faster_whisper must fail without the stub and import with it.
    import importlib
    import sys

    import pytest
    pytest.importorskip("faster_whisper")
    from simcoach import voice

    class _BlockAv:
        def find_spec(self, name, path=None, target=None):
            if name == "av" or name.startswith("av."):
                raise ImportError("PyAV excluded from the frozen build")

    for name in [m for m in sys.modules if m == "av" or m.startswith(("av.", "faster_whisper"))]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockAv(), *sys.meta_path])
    with pytest.raises(ImportError):
        importlib.import_module("faster_whisper")
    for name in [m for m in sys.modules if m.startswith("faster_whisper")]:
        monkeypatch.delitem(sys.modules, name)

    voice._stub_av()
    fw = importlib.import_module("faster_whisper")
    assert fw.WhisperModel and not hasattr(sys.modules["av"], "open")
    monkeypatch.delitem(sys.modules, "av")


def test_spec_drops_cudnn():
    # #221: proprietary NVIDIA DLL, unused on the CPU-only build.
    assert '"cudnn" not in b[0].lower()' in SPEC


def test_third_party_licenses_covers_the_sidecar_and_skips_excluded():
    # #221: the installer's license texts come from this walk; PyAV is excluded from
    # the freeze, so listing it would misstate what ships.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tpl", Path(__file__).parent.parent / "scripts" / "third_party_licenses.py")
    tpl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tpl)
    entries = {name.split()[0].lower(): texts for name, _lic, texts in tpl._python()}
    assert "av" not in entries
    assert entries["fastapi"] and "MIT" in entries["fastapi"][0]
