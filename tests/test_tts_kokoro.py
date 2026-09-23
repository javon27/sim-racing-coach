import importlib.util
import re
import struct
import sys
import threading
import time
import types

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
    Unpronounceable,
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
    assert out == "Turn ten, [Pouhon](/pˈaʊhɑːn/). Best one fifty two point three."


def test_preprocess_despaces_intra_word_hyphens_but_keeps_real_dashes(tmp_path):
    # "-3" has no word char before the hyphen, so it's a negative number, not an
    # intra-word hyphen; the number-expansion rule then spells it out same as any
    # other bare negative integer -- that's expand()'s job, not the hyphen fix's.
    out = preprocess("trail-braking right-left-right — fine - ok -3", {})
    assert out == "trail braking right left right — fine - ok minus three"
    # Numeric ranges are expand()'s job ("two to four"); the hyphen expand() itself
    # emits inside "forty-three" is despaced too (the rule runs after expansion).
    assert preprocess("Turns 2-4 and 143-145", {}) == "Turns two to four and one hundred forty three to one hundred forty five"


def test_synthesize_inserts_silence_between_sentences_not_around_a_single_one(tmp_path):
    class FakeKokoro:
        def __init__(self, *a): pass
        def get_voices(self): return ["af_heart"]
        def create(self, ph, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            return np.zeros(240, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=lambda t: t)

    one = eng.synthesize("Brake later.", voice="af_heart", speed=1.0)
    two = eng.synthesize("Brake later. Turn in earlier.", voice="af_heart", speed=1.0)

    assert len(one) == 44 + 240 * 2                       # unchanged: no gap added or removed
    gap_samples = int(0.25 * 24000)
    assert len(two) == 44 + (240 + gap_samples + 240) * 2  # one 250 ms gap between the two sentences


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


def test_synthesize_raises_unpronounceable_naming_the_failing_word(tmp_path):
    def g2p(text):
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
    with pytest.raises(Unpronounceable) as exc_info:
        eng.synthesize("Brake later. Pouhon is fast.", voice="af_heart", speed=1.0)
    assert exc_info.value.words == ["Pouhon"]


def test_synthesize_raises_unpronounceable_naming_every_failing_word_in_one_sentence(tmp_path):
    def g2p(text):
        if "Eau" in text or "Rouge" in text:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    class FakeKokoro:
        def __init__(self, *a): pass
        def get_voices(self): return ["af_heart"]
        def create(self, ph, voice, speed=1.0, is_phonemes=False, lang="en-us"):
            return np.zeros(240, dtype=np.float32), 24000

    _touch_model_files(tmp_path)
    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=g2p)
    with pytest.raises(Unpronounceable) as exc_info:
        eng.synthesize("Watch Eau Rouge.", voice="af_heart", speed=1.0)
    assert exc_info.value.words == ["Eau", "Rouge"]


def test_phonemes_for_raises_unpronounceable_for_a_lowercase_oov_word(tmp_path):
    def g2p(text):
        if re.search(r"\bkm\b", text):
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    eng = KokoroEngine(tmp_path, backend=None, g2p=g2p)
    with pytest.raises(Unpronounceable) as exc_info:
        eng._phonemes_for("a seven km lap")
    assert exc_info.value.words == ["km"]


def test_phonemes_for_raises_unpronounceable_when_every_word_fails(tmp_path):
    def g2p(_text):
        raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")

    eng = KokoroEngine(tmp_path, backend=None, g2p=g2p)
    with pytest.raises(Unpronounceable) as exc_info:
        eng._phonemes_for("Zzqx Vvbr.")
    assert exc_info.value.words == ["Zzqx", "Vvbr"]


def test_word_probe_result_is_cached_across_sentences(tmp_path):
    monza_calls = []

    def g2p(text):
        if text == "Monza":
            monza_calls.append(text)
        if "Monza" in text:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    eng = KokoroEngine(tmp_path, backend=None, g2p=g2p)
    with pytest.raises(Unpronounceable):
        eng._phonemes_for("Monza is fast.")
    with pytest.raises(Unpronounceable):
        eng._phonemes_for("I love Monza today.")
    assert len(monza_calls) == 1


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


def test_phonemes_for_keeps_pronunciation_markup_atomic_next_to_an_oov_word(tmp_path):
    """A `[Name](/phonemes/)` span from the track table must reach G2P verbatim even
    when ANOTHER word in the sentence is OOV -- never split into words, probed, or
    rebuilt, and the OOV word is named (not spelled out) so the caller can fall back
    (rig 2026-09-17: 'Pouhon' vanished from speech next to 'Kemmel')."""
    seen = []

    markup = re.compile(r"\[[^\]]*\]\(/[^)]*/\)")

    def g2p(text):
        seen.append(text)
        # Like misaki: an unknown word raises, and so does a torn markup fragment
        # (`[Pouhon` or `Pouhon](/pˈaʊhɑːn` on its own is not valid inline markup).
        leftover = markup.sub("", text)
        if "Kemmel" in text or "[" in leftover or "]" in leftover:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    eng = KokoroEngine(tmp_path, backend=None, g2p=g2p)
    with pytest.raises(Unpronounceable) as exc_info:
        eng._phonemes_for("Brake late at [Pouhon](/pˈaʊhɑːn/) after the Kemmel straight.")
    assert exc_info.value.words == ["Kemmel"]
    assert not any(s.startswith("[Pouhon") and "(/" not in s for s in seen)   # never probed as a bare fragment
    assert not any(s == "Pouhon" or s == "P o u h o n" for s in seen)


def test_phonemes_for_succeeds_when_the_only_oov_looking_word_is_inside_markup(tmp_path):
    """The whole-sentence call can fail for a reason that has nothing to do with any
    single word (e.g. markup punctuation confusing G2P in a larger context). If every
    word -- markup included -- probes clean on its own, that's not an OOV word: no
    Unpronounceable, no spelling, just a rebuild from each token's own phonemes."""
    def g2p(text):
        if "[" in text and "after" in text:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
        return text

    eng = KokoroEngine(tmp_path, backend=None, g2p=g2p)
    out = eng._phonemes_for("Watch [Pouhon](/pˈaʊhɑːn/) after the apex.")
    assert out == "Watch [Pouhon](/pˈaʊhɑːn/) after the apex"


# ---- #148: CMUdict fallback ---------------------------------------------------------
@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_cmudict_fallback_speaks_an_english_word_misaki_lacks():
    # "Glickenhaus" / "Kowalczyk" are OOV for misaki 0.7.4 (raised on main) but in CMUdict.
    from simcoach.tts_kokoro import phonemize
    assert phonemize("Glickenhaus") == "ɡlˈɪkənhˌWs"
    assert phonemize("Kowalczyk passed the Glickenhaus.") == "kˈWəlʧɪk pˈæst ðə ɡlˈɪkənhˌWs."


@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_a_word_in_neither_misaki_nor_cmudict_is_still_unpronounceable(tmp_path):
    eng = KokoroEngine(tmp_path, backend=None)
    with pytest.raises(Unpronounceable) as exc_info:
        eng._phonemes_for("Verstappen in the Glickenhaus.")
    assert exc_info.value.words == ["Verstappen"]


@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_lexicon_still_overrides_cmudict():
    # "de" is in CMUdict as English "dee" (D IY1) and in lexicon.json as French "də".
    from simcoach.pronunciations import _global_table
    from simcoach.tts_kokoro import phonemize
    assert tts_kokoro._cmudict_fallback(types.SimpleNamespace(text="de"))[0] == "dˈi"
    out = phonemize(preprocess("Circuit de Spa", _global_table()))
    assert "də" in out and "dˈi" not in out


@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_every_arpabet_mapping_is_in_kokoros_vocab():
    tts_kokoro.install_stubs()
    from kokoro_onnx.config import DEFAULT_VOCAB
    arpabet = [f"{p}{s}" for p in tts_kokoro._ARPABET for s in ("", "0", "1", "2")] + [
        f"{p}{s}" for p in ("AH", "ER") for s in ("0", "1", "2")]
    bad = {p: c for p in arpabet for c in tts_kokoro._arpabet_to_misaki(p) if c not in DEFAULT_VOCAB}
    assert not bad
    # and the whole shipped dictionary converts (no ARPAbet symbol missing from the table)
    tts_kokoro._cmudict_fallback(types.SimpleNamespace(text="a"))
    assert all(tts_kokoro._arpabet_to_misaki(a) for a in tts_kokoro._cmudict.values())
