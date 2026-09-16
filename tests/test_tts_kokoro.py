import importlib.util
import struct
import sys
import threading
import time

import numpy as np
import pytest

from simcoach import tts_kokoro
from simcoach.tts_kokoro import (
    KOKORO_ONNX_VERSION,
    MISAKI_VERSION,
    MODEL_FILE,
    VOICES_FILE,
    EngineUnavailable,
    KokoroEngine,
    preprocess,
)


def _touch_model_files(model_dir):
    (model_dir / MODEL_FILE).touch()
    (model_dir / VOICES_FILE).touch()


HAVE_KOKORO = importlib.util.find_spec("kokoro_onnx") is not None and importlib.util.find_spec("misaki") is not None


def test_gpl_packages_are_not_installed_in_this_environment():
    # The whole point of the adapter: none of these may be importable for real.
    # Routes through gpl_free() itself -- a bare importlib.util.find_spec() raises
    # ValueError on a stub module (no __spec__) once any earlier test has stubbed
    # it in, which gpl_free() already guards against.
    assert tts_kokoro.gpl_free()


def test_pins_match_installed_versions_when_present():
    if not HAVE_KOKORO:
        pytest.skip("kokoro-onnx/misaki not installed (voice extra)")
    from importlib.metadata import version
    assert version("kokoro-onnx") == KOKORO_ONNX_VERSION
    assert version("misaki") == MISAKI_VERSION


def test_install_stubs_registers_num2words_as_our_shim():
    tts_kokoro.install_stubs()
    assert sys.modules["num2words"].num2words(52.3) == "fifty-two point three"
    assert sys.modules["phonemizer"].__simcoach_stub__


def test_preprocess_expands_numbers_then_applies_table():
    out = preprocess("Turn 10, Pouhon. Best 1:52.3.", {"Pouhon": "pˈaʊhɑːn"})
    assert out == "Turn ten, [Pouhon](/pˈaʊhɑːn/). Best one fifty-two point three."


def _wav_header(b: bytes):
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE"
    channels, rate, _, _, bits = struct.unpack("<HIIHH", b[22:36])
    return channels, rate, bits


def test_synthesize_with_fake_backend_returns_16bit_mono_24k_wav(tmp_path):
    calls = []

    class FakeKokoro:
        def __init__(self, model, voices): self.model, self.voices = model, voices
        def get_voices(self): return ["af_heart", "am_michael", "jf_alpha"]
        def create(self, phonemes, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            calls.append((phonemes, voice, speed, is_phonemes))
            return np.linspace(-0.5, 0.5, 2400, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=lambda text: f"<{text}>")
    wav = eng.synthesize("Turn 10.", voice="am_michael", speed=1.2)
    assert _wav_header(wav) == (1, 24000, 16)
    assert len(wav) == 44 + 2400 * 2
    assert calls == [("<Turn ten.>", "am_michael", 1.2, True)]
    assert eng.voices() == ["af_heart", "am_michael"]   # English voices only (a*/b*)


def test_synthesize_retries_a_failing_sentence_spelled_out_then_drops_it(tmp_path, caplog):
    seen = []

    def g2p(text):
        seen.append(text)
        if "Pouhon" in text:
            raise ValueError("Pouhon")
        return text

    class FakeKokoro:
        def __init__(self, *a): pass
        def get_voices(self): return ["af_heart"]
        def create(self, ph, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            return np.zeros(240, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=g2p)
    wav = eng.synthesize("Brake later. Pouhon is fast.", voice="af_heart", speed=1.0)
    assert _wav_header(wav)[1] == 24000
    assert "P o u h o n is fast." in seen          # spelled-out retry
    assert any("Pouhon" in r.message for r in caplog.records)


def test_synthesize_retries_by_spelling_capitalised_words_when_message_has_no_word_match(tmp_path, caplog):
    seen = []

    def g2p(text):
        seen.append(text)
        if "Eau Rouge" in text:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    class FakeKokoro:
        def __init__(self, *a): pass
        def get_voices(self): return ["af_heart"]
        def create(self, ph, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            return np.zeros(240, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=g2p)
    wav = eng.synthesize("Watch Eau Rouge.", voice="af_heart", speed=1.0)
    assert _wav_header(wav)[1] == 24000
    assert any(s.startswith(("Watch E a u", "Watch Eau")) for s in seen[1:])
    assert any("dropped" in r.message or "retrying" in r.message for r in caplog.records)


def test_engine_unavailable_when_model_files_missing(tmp_path):
    eng = KokoroEngine(tmp_path, backend=None, g2p=lambda t: t)
    assert eng.available() is False
    with pytest.raises(EngineUnavailable):
        eng.synthesize("hi", voice="af_heart", speed=1.0)


def test_load_is_locked_against_concurrent_voices_and_synthesize_calls(tmp_path):
    counter = {"n": 0}

    class FakeKokoro:
        def __init__(self, *a):
            counter["n"] += 1
            time.sleep(0.05)   # widen the race window an unlocked _load() would fall into
        def get_voices(self): return ["af_heart"]
        def create(self, ph, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            return np.zeros(240, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=lambda t: t)

    threads = [threading.Thread(target=eng.voices),
               threading.Thread(target=lambda: eng.synthesize("hi", voice="af_heart", speed=1.0))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert counter["n"] == 1


@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_misaki_without_spacy_matches_the_spike_reference_phonemes():
    # Reference strings from kokoro-nospacy-spike-report.md (spaCy path == stub path).
    from simcoach.tts_kokoro import phonemize
    assert phonemize("ABS is on 3, TC on 6.") == "ˌAbˌiˈɛs ɪz ˌɔn θɹˈi, tˌisˈi ˌɔn sˈɪks."
    assert phonemize("Record the lap; a new record.") == "ɹəkˈɔɹd ðə lˈæp; ɐ nˈu ɹˈɛkəɹd."
    assert "spacy" not in sys.modules or getattr(sys.modules["spacy"], "__simcoach_stub__", False)
