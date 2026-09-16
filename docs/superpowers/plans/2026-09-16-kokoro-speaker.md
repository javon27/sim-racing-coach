# Kokoro Speaker Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An opt-in, fully offline Kokoro-82M voice for the coach behind the existing `Speaker` seam, with Windows `speechSynthesis` staying the default, and nothing copyleft shipped.

**Architecture:** The FastAPI sidecar synthesises (`POST /voice/speak` → WAV): our number-to-words, the track's pronunciation table, misaki G2P patched to run without spaCy, then kokoro-onnx through an adapter that never imports `phonemizer` or loads espeak-ng. The webview's `KokoroSpeaker` implements `Speaker` by fetching WAVs and playing them through the shared `AudioContext`, pipelining sentences. A `KokoroInstaller` mirrors the whisper installer; Settings gains an Engine select.

**Tech Stack:** Python 3.13, FastAPI, numpy, `kokoro-onnx==0.6.1`, `misaki==0.7.4`, `onnxruntime==1.30.0`; Svelte 5 + TypeScript, vitest; PyInstaller sidecar.

**Spec:** `docs/superpowers/specs/2026-09-16-kokoro-speaker-design.md` (spike evidence: `.superpowers/sdd/overnight-2026-09-16/kokoro-licence-spike-report.md`, `kokoro-nospacy-spike-report.md`; the spike patches are embedded verbatim in Task 3).

## Global Constraints

- Windows voices stay the DEFAULT engine; `voice.tts_engine` defaults to `"windows"`.
- Never import `phonemizer`, `espeakng_loader`, `spacy` or `num2words` in the sidecar; the adapter's import guard test must stay red-proof (Task 3) and the frozen build must exclude all four (Task 5).
- Pins: `kokoro-onnx==0.6.1`, `misaki==0.7.4`, `onnxruntime==1.30.0`. misaki is installed with `--no-deps` (its `en` extra pulls spaCy/num2words/phonemizer); its only runtime dep we ship is `regex`.
- Model files (opt-in download, never in the installer): `kokoro-v1.0.onnx` 325 532 387 bytes sha256 `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5`; `voices-v1.0.bin` 28 214 398 bytes sha256 `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d`; URL base `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/`. Stored at `<app-data root>/kokoro/`.
- WAV out: 24 000 Hz, mono, 16-bit PCM.
- First-word latency is accepted; the client keeps ONE synthesis in flight ahead of playback.
- Commit trailers per the session; `Refs #104` (never `Fixes`). Stage by name; never `git stash` / `git reset` / `git add -A`. Do not touch `simcoach/tracks/fuji_speedway.json` or `simcoach/tracks/silverstone_national_circuit.json` (untracked on purpose).
- Backend: `.venv/Scripts/python.exe -m pytest -q`, `ruff check` clean on touched files. Frontend: `cd frontend && npx vitest run`, `npx svelte-check --threshold error` (0 errors).

## File structure

- `simcoach/speech_numbers.py` (new) — number-to-words for coach speech + `num2words`-compatible shim. Pure.
- `simcoach/pronunciations.py` (new) — validate a track file's `pronunciations` block; rewrite text to misaki inline markup. Pure.
- `simcoach/tts_kokoro.py` (new) — no-GPL adapter (stubs + Tokenizer patch), misaki no-spaCy patch, `KokoroEngine`, `preprocess()`, WAV encoding.
- `simcoach/voice.py` — `KokoroInstaller`, `kokoro_dir()`, `kokoro_installed()`.
- `simcoach/config.py` — `voice.tts_engine`, `voice.kokoro_voice`.
- `simcoach/server/app.py` — `/voice/kokoro/setup`, `/voice/kokoro/status`, `/voice/speak`, `voice_setup` frame `engine`.
- `pyproject.toml`, `packaging/simcoach-sidecar.spec`, `.github/workflows/build-installer.yml`, `packaging/README.md`.
- `frontend/src/lib/audio.ts` — `KokoroSpeaker`, shared earcon helper; `frontend/src/lib/api.ts` — `speak`, `kokoroStatus`, `kokoroSetup`, settings types; `frontend/src/lib/ChatPanel.svelte` — engine factory; `frontend/src/lib/SettingsScreen.svelte` — Engine select, dialog, preview, notice.
- `scripts/harvest_pronunciations.py` (new, dev only) + `pronunciations` blocks in the shipped track files.
- Tests: `tests/test_speech_numbers.py`, `tests/test_pronunciations.py`, `tests/test_tts_kokoro.py`, `tests/test_voice.py`, `tests/test_server_voice.py`, `tests/test_packaging_spec.py`, `frontend/src/lib/audio.test.ts`, `frontend/src/lib/ChatPanel.test.ts`, `frontend/src/lib/SettingsScreen.test.ts`.

---

### Task 1: `speech_numbers` — number-to-words for coach speech

**Files:**
- Create: `simcoach/speech_numbers.py`
- Test: `tests/test_speech_numbers.py`

**Interfaces:**
- Produces: `expand(text: str) -> str` (rewrites every numeric token the coach speaks into words; leaves everything else byte-identical) and `num2words(n: int | float, to: str = "cardinal") -> str` (the `num2words` call signature misaki uses: `to` ∈ `cardinal`, `ordinal`, `year`; floats read as "X point Y Z" digit by digit after the point). Task 3 installs this module as `sys.modules["num2words"]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_speech_numbers.py
import pytest

from simcoach.speech_numbers import expand, num2words


@pytest.mark.parametrize("n,words", [
    (0, "zero"), (7, "seven"), (10, "ten"), (13, "thirteen"), (20, "twenty"),
    (21, "twenty-one"), (52, "fifty-two"), (100, "one hundred"), (101, "one hundred one"),
    (120, "one hundred twenty"), (1000, "one thousand"), (1234, "one thousand two hundred thirty-four"),
    (-3, "minus three"),
])
def test_cardinals(n, words):
    assert num2words(n) == words


@pytest.mark.parametrize("n,words", [
    (1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"), (9, "ninth"),
    (12, "twelfth"), (20, "twentieth"), (21, "twenty-first"), (100, "one hundredth"),
])
def test_ordinals(n, words):
    assert num2words(n, to="ordinal") == words


def test_year_reads_in_pairs():
    assert num2words(1998, to="year") == "nineteen ninety-eight"
    assert num2words(2026, to="year") == "twenty twenty-six"


def test_float_reads_digits_after_the_point():
    assert num2words(0.3) == "zero point three"
    assert num2words(52.3) == "fifty-two point three"
    assert num2words(1.25) == "one point two five"


@pytest.mark.parametrize("text,spoken", [
    ("Brake later into T4.", "Brake later into turn four."),
    ("You're losing 3 tenths there.", "You're losing three tenths there."),
    ("Your best is 1:52.3, this lap was 1:53.1.",
     "Your best is one fifty-two point three, this lap was one fifty-three point one."),
    ("A 2:05.0 lap.", "A two oh-five point zero lap."),
    ("ABS is on 3, TC on 6.", "ABS is on three, TC on six."),
    ("0.3 seconds", "zero point three seconds"),
    ("-0.3 s", "minus zero point three s"),
    ("52 percent", "fifty-two percent"),
    ("52%", "fifty-two percent"),
    ("2nd", "second"), ("21st", "twenty-first"), ("3rd gear", "third gear"),
    ("Turn 10, sector 2.", "Turn ten, sector two."),
    ("T10", "turn ten"),
    ("no numbers here", "no numbers here"),
])
def test_expand(text, spoken):
    assert expand(text) == spoken


def test_expand_leaves_unknown_shapes_alone():
    assert expand("v1.0.3 and 12:34:56") == "v1.0.3 and 12:34:56"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_speech_numbers.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'simcoach.speech_numbers'`

- [ ] **Step 3: Implement**

```python
# simcoach/speech_numbers.py
"""Number-to-words for the coach's SPOKEN text (Kokoro path).

Two jobs: `expand(text)` rewrites the numeric shapes the coach actually says into
words BEFORE grapheme-to-phoneme (misaki drops the colon sub-token in "1:52.3", so lap
times must be words by the time it sees them), and `num2words(n, to=...)` provides the
call signature misaki itself uses, so the LGPL `num2words` package is never installed:
`simcoach.tts_kokoro` registers this module under that name. Pure; no dependencies.
"""
from __future__ import annotations

import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_ONES = {"one": "first", "two": "second", "three": "third", "five": "fifth",
             "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _below_thousand(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    return f"{_ONES[hundreds]} hundred" + (f" {_below_thousand(rest)}" if rest else "")


def _cardinal(n: int) -> str:
    if n < 0:
        return "minus " + _cardinal(-n)
    if n < 1000:
        return _below_thousand(n)
    parts = []
    for scale, name in _SCALES:
        q, n = divmod(n, scale)
        if q:
            parts.append(f"{_below_thousand(q)} {name}")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def _ordinal(n: int) -> str:
    words = _cardinal(n)
    head, sep, last = words.rpartition("-") if "-" in words.rsplit(" ", 1)[-1] else words.rpartition(" ")
    if last in _ORD_ONES:
        last_ord = _ORD_ONES[last]
    elif last.endswith("y"):
        last_ord = last[:-1] + "ieth"
    else:
        last_ord = last + "th"
    return f"{head}{sep}{last_ord}"


def _year(n: int) -> str:
    if 1000 <= n <= 9999 and n % 100:
        hi, lo = divmod(n, 100)
        return f"{_cardinal(hi)} {_cardinal(lo) if lo >= 10 else 'oh-' + _ONES[lo]}"
    if 1000 <= n <= 9999:
        return f"{_cardinal(n // 100)} hundred"
    return _cardinal(n)


def num2words(n: int | float, to: str = "cardinal") -> str:
    """misaki-compatible: `to` in {"cardinal", "ordinal", "year"}; floats read the
    digits after the point one by one ("52.3" -> "fifty-two point three")."""
    if isinstance(n, float) and not n.is_integer():
        whole, _, frac = repr(n).partition(".")
        return f"{_cardinal(int(whole))} point " + " ".join(_ONES[int(d)] for d in frac)
    n = int(n)
    if to == "ordinal":
        return _ordinal(n)
    if to == "year":
        return _year(n)
    return _cardinal(n)


def _lap_time(m: re.Match) -> str:
    minutes, seconds, tenths = int(m.group(1)), int(m.group(2)), m.group(3)
    secs = _cardinal(seconds) if seconds >= 10 else f"oh-{_ONES[seconds]}"
    out = f"{_cardinal(minutes)} {secs}"
    if tenths is not None:
        out += " point " + " ".join(_ONES[int(d)] for d in tenths)
    return out


def _decimal(m: re.Match) -> str:
    sign = "minus " if m.group(1) else ""
    return sign + f"{_cardinal(int(m.group(2)))} point " + " ".join(_ONES[int(d)] for d in m.group(3))


# Order matters: lap times before decimals, T-prefix before bare integers.
_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?:\.(\d+))?(?![\d:.])"), _lap_time),
    (re.compile(r"(?<![\w.])(-?)(\d+)\.(\d+)(?![\d.])"), _decimal),
    (re.compile(r"(?<![\w-])(\d+)(st|nd|rd|th)\b"), lambda m: _ordinal(int(m.group(1)))),
    (re.compile(r"\bT(\d{1,2})\b"), lambda m: f"turn {_cardinal(int(m.group(1)))}"),
    (re.compile(r"(?<![\w.])(-?)(\d+)%"), lambda m: ("minus " if m.group(1) else "") + f"{_cardinal(int(m.group(2)))} percent"),
    (re.compile(r"(?<![\w.:-])(-?)(\d+)(?![\w.:])"), lambda m: ("minus " if m.group(1) else "") + _cardinal(int(m.group(2)))),
]


def expand(text: str) -> str:
    """Rewrite the numeric shapes the coach speaks into words; leave any other shape
    (version strings, hh:mm:ss) untouched."""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
```

- [ ] **Step 4: Run to verify it passes** — `.venv/Scripts/python.exe -m pytest tests/test_speech_numbers.py -q` → all pass. If a parametrised case disagrees with the implementation, fix the IMPLEMENTATION to the table (the table is the spec); the only acceptable table edit is a typo.

- [ ] **Step 5: Commit** — `git add simcoach/speech_numbers.py tests/test_speech_numbers.py && git commit -m "feat(voice): number-to-words for spoken cues (replaces num2words)"` (+ trailers, `Refs #104`).

---

### Task 2: `pronunciations` — per-track table → misaki inline markup

**Files:**
- Create: `simcoach/pronunciations.py`
- Test: `tests/test_pronunciations.py`

**Interfaces:**
- Produces: `validated_pronunciations(value) -> dict[str, str]` (raises `TypeError`/`ValueError` naming what's wrong: must be a dict of non-empty str → non-empty str; keys stripped; same contract style as `corners.validated_sectors`), `table_for(track_data: dict) -> dict[str, str]` (the block from a merged track dict, `{}` when absent or invalid — logs a warning, never raises), `apply(text: str, table: dict[str, str]) -> str` (longest key first, whole-word, case-insensitive, each match → `[<original text>](/<phonemes>/)`; text already inside markup is left alone).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pronunciations.py
import pytest

from simcoach.pronunciations import apply, table_for, validated_pronunciations

TABLE = {"Pouhon": "pˈaʊhɑːn", "Eau Rouge": "ˈoʊ ɹˈuːʒ", "Raidillon": "ɹˈeɪdɪlən"}


def test_apply_rewrites_whole_words_longest_first_case_insensitive():
    assert apply("Eau Rouge and Raidillon were flat; pouhon too.", TABLE) == (
        "[Eau Rouge](/ˈoʊ ɹˈuːʒ/) and [Raidillon](/ɹˈeɪdɪlən/) were flat; [pouhon](/pˈaʊhɑːn/) too.")


def test_apply_does_not_touch_substrings_or_existing_markup():
    assert apply("Pouhonx", TABLE) == "Pouhonx"
    assert apply("[Pouhon](/x/)", TABLE) == "[Pouhon](/x/)"


def test_apply_with_empty_table_is_identity():
    assert apply("Turn 10, Pouhon", {}) == "Turn 10, Pouhon"


def test_table_for_reads_the_block_and_tolerates_junk():
    assert table_for({"pronunciations": TABLE}) == TABLE
    assert table_for({}) == {}
    assert table_for({"pronunciations": ["nope"]}) == {}
    assert table_for({"pronunciations": {"Pouhon": 3}}) == {}


@pytest.mark.parametrize("bad,exc", [
    (["a"], TypeError), ({"": "x"}, ValueError), ({"a": ""}, ValueError), ({"a": 1}, ValueError),
    ({1: "x"}, ValueError),
])
def test_validated_pronunciations_refuses(bad, exc):
    with pytest.raises(exc):
        validated_pronunciations(bad)


def test_validated_pronunciations_strips_keys():
    assert validated_pronunciations({" Pouhon ": "pˈaʊhɑːn"}) == {"Pouhon": "pˈaʊhɑːn"}
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# simcoach/pronunciations.py
"""Per-track pronunciation table (Kokoro path, #104).

A track file may carry `"pronunciations": {"Pouhon": "pˈaʊhɑːn"}` -- phonemes in
Kokoro's own vocabulary, harvested once by scripts/harvest_pronunciations.py. `apply`
rewrites each name in the spoken text to misaki's inline markup so G2P copies the
phonemes verbatim instead of guessing (or crashing) on a proper noun it doesn't know.
"""
from __future__ import annotations

import logging
import re

_MARKUP = re.compile(r"\[[^\]]*\]\(/[^)]*/\)")


def validated_pronunciations(value) -> dict[str, str]:
    """The block as {name: phonemes}, or TypeError/ValueError naming what's wrong.
    Same contract as corners.validated_sectors: refuses, never repairs."""
    if not isinstance(value, dict):
        raise TypeError("pronunciations must be an object of name -> phonemes")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("pronunciation names must be non-empty strings")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"pronunciation for {k!r} must be a non-empty string")
        out[k.strip()] = v.strip()
    return out


def table_for(track_data: dict) -> dict[str, str]:
    """The track's table, or {} (absent, or invalid -- logged, never raised)."""
    block = track_data.get("pronunciations") if isinstance(track_data, dict) else None
    if block is None:
        return {}
    try:
        return validated_pronunciations(block)
    except (TypeError, ValueError) as e:
        logging.getLogger(__name__).warning("ignoring pronunciations block: %s", e)
        return {}


def apply(text: str, table: dict[str, str]) -> str:
    """Rewrite every whole-word, case-insensitive match of a table key (longest key
    first) to `[match](/phonemes/)`; spans already in markup are left untouched."""
    if not table:
        return text
    keys = sorted(table, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w\]])",
                         re.IGNORECASE)
    lookup = {k.lower(): v for k, v in table.items()}
    out, pos = [], 0
    for m in _MARKUP.finditer(text):           # protect existing markup spans
        out.append(pattern.sub(lambda mm: f"[{mm.group(1)}](/{lookup[mm.group(1).lower()]}/)", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(pattern.sub(lambda mm: f"[{mm.group(1)}](/{lookup[mm.group(1).lower()]}/)", text[pos:]))
    return "".join(out)
```

- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Commit** — `feat(voice): per-track pronunciation table -> misaki inline markup`.

---

### Task 3: `tts_kokoro` — no-GPL adapter, misaki without spaCy, `KokoroEngine`

**Files:**
- Create: `simcoach/tts_kokoro.py`
- Test: `tests/test_tts_kokoro.py`

**Interfaces:**
- Consumes: `speech_numbers.expand`, `speech_numbers` as the `num2words` shim, `pronunciations.apply`.
- Produces: `preprocess(text, table) -> str`; `class KokoroEngine(model_dir: Path)` with `synthesize(text: str, voice: str, speed: float, table: dict[str, str] | None = None) -> bytes` (WAV), `voices() -> list[str]`, `available() -> bool`; `class EngineUnavailable(RuntimeError)`; `KOKORO_ONNX_VERSION = "0.6.1"`, `MISAKI_VERSION = "0.7.4"`; `phonemize(text) -> str` (the G2P step alone, for tests); `install_stubs()`.
- The GPL packages are never importable: `install_stubs()` registers `phonemizer`, `phonemizer.backend`, `phonemizer.backend.espeak`, `phonemizer.backend.espeak.wrapper`, `espeakng_loader`, `spacy` and `num2words` in `sys.modules` (num2words → `simcoach.speech_numbers`) BEFORE `kokoro_onnx` / `misaki.en` are imported, and refuses to run if any real one is already imported.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tts_kokoro.py
import importlib.util
import struct
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from simcoach import tts_kokoro
from simcoach.tts_kokoro import KOKORO_ONNX_VERSION, MISAKI_VERSION, EngineUnavailable, KokoroEngine, preprocess

HAVE_KOKORO = importlib.util.find_spec("kokoro_onnx") is not None and importlib.util.find_spec("misaki") is not None


def test_gpl_packages_are_not_installed_in_this_environment():
    # The whole point of the adapter: none of these may be importable for real.
    for name in ("phonemizer", "espeakng_loader", "spacy", "num2words"):
        spec = importlib.util.find_spec(name)
        assert spec is None or getattr(sys.modules.get(name), "__simcoach_stub__", False), name


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

    eng = KokoroEngine(tmp_path, backend=FakeKokoro, g2p=g2p)
    wav = eng.synthesize("Brake later. Pouhon is fast.", voice="af_heart", speed=1.0)
    assert _wav_header(wav)[1] == 24000
    assert "P o u h o n is fast." in seen          # spelled-out retry
    assert any("Pouhon" in r.message for r in caplog.records)


def test_engine_unavailable_when_model_files_missing(tmp_path):
    eng = KokoroEngine(tmp_path, backend=None, g2p=lambda t: t)
    assert eng.available() is False
    with pytest.raises(EngineUnavailable):
        eng.synthesize("hi", voice="af_heart", speed=1.0)


@pytest.mark.skipif(not HAVE_KOKORO, reason="voice extra not installed")
def test_misaki_without_spacy_matches_the_spike_reference_phonemes():
    # Reference strings from kokoro-nospacy-spike-report.md (spaCy path == stub path).
    from simcoach.tts_kokoro import phonemize
    assert phonemize("ABS is on 3, TC on 6.") == "ˌAbˌiˈɛs ɪz ˌɔn θɹˈi, tˌisˈi ˌɔn sˈɪks."
    assert phonemize("Record the lap; a new record.") == "ɹəkˈɔɹd ðə lˈæp; ɐ nˈu ɹˈɛkəɹd."
    assert "spacy" not in sys.modules or getattr(sys.modules["spacy"], "__simcoach_stub__", False)
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: simcoach.tts_kokoro`.

- [ ] **Step 3: Implement** (the stub/patch code is the spike's, verbatim except for module names and the `__simcoach_stub__` marker):

```python
# simcoach/tts_kokoro.py
"""Kokoro-82M on the CPU without any GPL code loaded (#104).

kokoro-onnx imports `phonemizer` and `espeakng_loader` (GPL) at module level and
loads espeak-ng.dll in Tokenizer.__init__; misaki.en imports `spacy` and `num2words`
(LGPL) at module level. `install_stubs()` registers stand-ins for all of them BEFORE
either package is imported; `_patch_misaki()` replaces G2P's tokenizer with a regex
tokenizer + a small heuristic Penn tagger that is phoneme-identical to spaCy on coach
speech (spike 2026-09-16); `_patch_kokoro()` makes Tokenizer a pure vocab lookup.
Pinned to the versions the spikes ran -- see the version constants.
"""
from __future__ import annotations

import io
import logging
import re
import struct
import sys
import threading
import types
from pathlib import Path
from typing import Callable

import numpy as np

from simcoach import speech_numbers
from simcoach.pronunciations import apply as apply_pronunciations

KOKORO_ONNX_VERSION = "0.6.1"
MISAKI_VERSION = "0.7.4"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
SAMPLE_RATE = 24_000
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
log = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    """The model is not installed or failed to load; callers fall back to Windows voices."""


# ---- GPL/LGPL import stubs -----------------------------------------------------------
def install_stubs() -> None:
    """Register stand-ins for phonemizer / espeakng_loader / spacy / num2words. Idempotent.
    Refuses if a REAL one is already imported: the adapter's guarantee is that they never are."""
    for name in ("phonemizer", "espeakng_loader", "spacy", "num2words"):
        mod = sys.modules.get(name)
        if mod is not None and not getattr(mod, "__simcoach_stub__", False):
            raise RuntimeError(f"{name} is already imported; the no-GPL adapter must load first")
    if getattr(sys.modules.get("phonemizer"), "__simcoach_stub__", False):
        return

    def _boom(*_a, **_k):
        raise RuntimeError("GPL backend stubbed out; phonemes come from misaki")

    ph = types.ModuleType("phonemizer"); ph.__simcoach_stub__ = True; ph.phonemize = _boom
    backend = types.ModuleType("phonemizer.backend")
    espeak = types.ModuleType("phonemizer.backend.espeak")
    wrapper = types.ModuleType("phonemizer.backend.espeak.wrapper")

    class _EspeakWrapper:
        @staticmethod
        def set_data_path(*_a, **_k): pass
        @staticmethod
        def set_library(*_a, **_k): pass

    wrapper.EspeakWrapper = _EspeakWrapper
    loader = types.ModuleType("espeakng_loader"); loader.__simcoach_stub__ = True
    loader.get_data_path = lambda: None; loader.get_library_path = lambda: None

    spacy = types.ModuleType("spacy"); spacy.__simcoach_stub__ = True
    spacy.load = _boom
    spacy.util = types.SimpleNamespace(is_package=lambda name: True)
    spacy.cli = types.SimpleNamespace(download=_boom)
    spacy.training = types.SimpleNamespace(Alignment=types.SimpleNamespace(from_strings=_boom))

    n2w = types.ModuleType("num2words"); n2w.__simcoach_stub__ = True
    n2w.num2words = speech_numbers.num2words

    for mod in (ph, backend, espeak, wrapper, loader, spacy, n2w):
        sys.modules[mod.__name__] = mod


# ---- misaki without spaCy (spike patch, verbatim) -----------------------------------
_TOKEN_RE = re.compile(
    r"\d+(?:[.:,]\d+)*%?"            # numbers, times, decimals, percents
    r"|[A-Za-z]+(?:['’][A-Za-z]+)*"  # words, incl. contractions
    r"|[^\sA-Za-z0-9]"               # single punctuation char
)
_CLOSED_TAGS = {"a": "DT", "an": "DT", "the": "DT", "to": "TO", "i": "PRP"}
_VERB_CUES = {"i", "you", "we", "they", "to", "will", "can", "did", "does", "do", "please", "let"}
_NOUN_CUES = {"a", "an", "the", "your", "my", "his", "her", "its", "their", "this", "that", "new", "a's"}
_ADJ_CUES = {"is", "was", "are", "were", "too", "very", "be"}
_PUNCT_TAGS = {".": ".", "!": ".", "?": ".", ",": ",", ":": ":", ";": ":", "-": ":", "–": ":",
               "(": "-LRB-", ")": "-RRB-", '"': "''", "'": "''", "$": "$", "%": "NFP", "#": "#"}


def _guess_tag(word: str, prev_lower: str | None, is_sent_start: bool) -> str:
    lw = word.lower()
    if lw in _CLOSED_TAGS:
        return _CLOSED_TAGS[lw]
    if word.isupper() and len(word) > 1:
        return "NNP"                         # acronym-shaped: ABS, TC
    if word[:1].isupper():
        return "VB" if is_sent_start else "NNP"   # coach cues are imperatives ("Record the lap")
    if prev_lower in _VERB_CUES:
        return "VBP"
    if prev_lower in _NOUN_CUES:
        return "NN"
    if prev_lower in _ADJ_CUES:
        return "JJ"
    return "NN"


def _simple_tokenize(text: str):
    from misaki.en import MToken
    matches = list(_TOKEN_RE.finditer(text))
    tokens, prev_lower, sent_start = [], None, True
    for idx, m in enumerate(matches):
        word = m.group(0)
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        whitespace = text[m.end():next_start]
        if word[0].isdigit():
            tag = "CD"
        elif word[0].isalpha():
            tag = _guess_tag(word, prev_lower, sent_start)
            prev_lower = word.lower()
        else:
            tag = _PUNCT_TAGS.get(word, ".")
        tokens.append(MToken(text=word, tag=tag, whitespace=whitespace))
        sent_start = word in (".", "!", "?")
    return tokens


def _tokenize_nospacy(self, text, tokens, features):
    """Drop-in for misaki.en.G2P.tokenize: maps inline-markup feature indices onto our
    fine tokens by consuming them until their concatenated text equals each coarse
    (whitespace-split) token -- what spaCy's Alignment computed before."""
    mutable = _simple_tokenize(text)
    if not features:
        return mutable
    j = 0
    for k, coarse in enumerate(tokens):
        js, acc = [], ""
        while j < len(mutable) and acc != coarse:
            if js:
                acc += mutable[js[-1]].whitespace
            acc += mutable[j].text
            js.append(j)
            j += 1
        if k not in features:
            continue
        v = features[k]
        for i, jj in enumerate(js):
            t = mutable[jj]
            if not isinstance(v, str):
                t.stress = v
            elif v.startswith("/"):
                t.is_head = i == 0
                t.phonemes = v.lstrip("/") if i == 0 else ""
                t.rating = 5
            elif v.startswith("#"):
                t.num_flags = v.lstrip("#")
    return mutable


_g2p = None
_g2p_lock = threading.Lock()


def _patch_misaki():
    from misaki import en

    def _init(self, trf=False, british=False, fallback=None, unk="❓"):
        self.british, self.nlp, self.lexicon, self.fallback, self.unk = british, None, en.Lexicon(british), None, unk

    en.G2P.__init__ = _init
    en.G2P.tokenize = _tokenize_nospacy
    return en.G2P()


def phonemize(text: str) -> str:
    """misaki English G2P, no spaCy, no espeak fallback. Raises on a token it can't resolve."""
    global _g2p
    install_stubs()
    with _g2p_lock:
        if _g2p is None:
            _g2p = _patch_misaki()
        phonemes, _tokens = _g2p(text)
    return phonemes


# ---- kokoro-onnx without espeak ------------------------------------------------------
def _patch_kokoro():
    install_stubs()
    import kokoro_onnx
    from kokoro_onnx.tokenizer import Tokenizer

    def _init(self, espeak_config=None, vocab=None):
        self.vocab = vocab or kokoro_onnx.config.DEFAULT_VOCAB   # no ctypes espeak load

    Tokenizer.__init__ = _init
    return kokoro_onnx.Kokoro


def preprocess(text: str, table: dict[str, str] | None) -> str:
    return apply_pronunciations(speech_numbers.expand(text), table or {})


def _wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    buf.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
    buf.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
    return buf.getvalue()


class KokoroEngine:
    """One loaded Kokoro per process, lazy; synthesis serialised (CPU-bound, ordered)."""

    def __init__(self, model_dir: Path, backend: Callable | None = None, g2p: Callable[[str], str] | None = None):
        self.model_dir = Path(model_dir)
        self._backend = backend          # None = real kokoro-onnx via _patch_kokoro()
        self._g2p = g2p or phonemize
        self._kokoro = None
        self._lock = threading.Lock()
        self._error: str | None = None

    def available(self) -> bool:
        return (self.model_dir / MODEL_FILE).exists() and (self.model_dir / VOICES_FILE).exists()

    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        if not self.available():
            raise EngineUnavailable("Kokoro model is not installed")
        try:
            cls = self._backend or _patch_kokoro()
            self._kokoro = cls(str(self.model_dir / MODEL_FILE), str(self.model_dir / VOICES_FILE))
        except Exception as e:      # noqa: BLE001 -- surfaced to Settings; Windows voices take over
            self._error = str(e) or type(e).__name__
            raise EngineUnavailable(self._error) from e
        return self._kokoro

    def voices(self) -> list[str]:
        return [v for v in self._load().get_voices() if v[:1] in ("a", "b")]   # American/British English

    def _phonemes_for(self, sentence: str) -> str | None:
        try:
            return self._g2p(sentence)
        except Exception as e:      # noqa: BLE001 -- misaki raises on an unknown proper noun
            word = str(e).strip("'\" ") or "?"
            log.warning("kokoro g2p failed on %r (word %r); retrying spelled out", sentence, word)
            spelled = re.sub(re.escape(word), " ".join(word), sentence) if word in sentence else sentence
            try:
                return self._g2p(spelled)
            except Exception:       # noqa: BLE001
                log.warning("kokoro g2p dropped sentence %r", sentence)
                return None

    def synthesize(self, text: str, voice: str, speed: float, table: dict[str, str] | None = None) -> bytes:
        kokoro = self._load()
        prepared = preprocess(text, table)
        chunks = []
        with self._lock:
            for sentence in (s for s in _SENTENCE.split(prepared) if s.strip()):
                ph = self._phonemes_for(sentence)
                if not ph:
                    continue
                samples, rate = kokoro.create(ph, voice=voice, speed=float(speed), is_phonemes=True)
                chunks.append((np.asarray(samples, dtype=np.float32), rate))
        if not chunks:
            return _wav_bytes(np.zeros(0, dtype=np.float32), SAMPLE_RATE)
        rate = chunks[0][1]
        return _wav_bytes(np.concatenate([c for c, _ in chunks]), rate)
```

Note the spelled-out retry keys off the exception message; misaki's `TypeError` on an unknown token does not name the word, so `_phonemes_for` must ALSO try each capitalised word in turn when the message doesn't match a word in the sentence: implement that loop (for each `[A-Z][a-z]+` token not in markup, spell it and retry; first success wins; else drop) and extend the retry test to a `TypeError("unsupported operand")` message.

- [ ] **Step 4: Run to verify it passes** — `.venv/Scripts/python.exe -m pytest tests/test_tts_kokoro.py -q`. The reference-phoneme test runs only where the voice extra is installed (install it in the worktree venv: `pip install --only-binary=:all: onnxruntime==1.30.0 regex && pip install --no-deps kokoro-onnx==0.6.1 misaki==0.7.4` — never `pip install misaki[en]`).

- [ ] **Step 5: Commit** — `feat(voice): Kokoro engine with no GPL code loaded (adapter + misaki without spaCy)`.

---

### Task 4: Installer, config keys, `/voice/kokoro/*`, `/voice/speak`, `voice_setup` frame

**Files:**
- Modify: `simcoach/voice.py` (after `ModelInstaller`), `simcoach/config.py` (`voice` block), `simcoach/server/app.py` (voice endpoints ~2447-2456; `create_app` wiring ~287; events tick voice section ~2330)
- Test: `tests/test_voice.py`, `tests/test_config.py`, `tests/test_server_voice.py`, `tests/test_server_events.py`

**Interfaces:**
- Consumes: `KokoroEngine`, `EngineUnavailable`, `pronunciations.table_for`, `_track_file_data(track)` (app.py), `load_map_data`.
- Produces: `voice.kokoro_dir(data_dir) -> Path` (`<root>/kokoro`), `voice.kokoro_installed(data_dir) -> bool`, `class KokoroInstaller(data_dir, downloader=None)` with the same `start()/status()` shape as `ModelInstaller` plus `status()["engine"] == "kokoro"` and `status()["voices"]` (list, `[]` until installed); `create_app(..., kokoro_installer=None, kokoro_engine=None)`; endpoints `POST /voice/kokoro/setup` → `{started, ...status}`, `GET /voice/kokoro/status`, `POST /voice/speak` JSON `{text, voice?, speed?, track_slug?}` → `audio/wav` (400 empty text, 409 not installed, 503 engine failed with `detail`); config keys `voice.tts_engine="windows"`, `voice.kokoro_voice="af_heart"`; `voice_setup` frames now carry `engine: "whisper" | "kokoro"` and the events tick watches both installers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_voice.py (append)
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
    with pytest.raises(v.VoiceInputError if hasattr(v, "VoiceInputError") else Exception, match="sha256"):
        v._default_kokoro_download(tmp_path / "kokoro")
```

```python
# tests/test_config.py (append)
def test_voice_defaults_include_engine_and_kokoro_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert cfg["voice"]["tts_engine"] == "windows" and cfg["voice"]["kokoro_voice"] == "af_heart"
```

```python
# tests/test_server_voice.py (append; reuse _app/_wav helpers)
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
    c = _app(tmp_path, monkeypatch, kokoro_engine=eng)
    (Path(c.app.state.tracks_dir) / "circuit_de_spa_francorchamps.json")  # shipped file has no block yet
    r = c.post("/voice/speak", json={"text": "Turn 10", "voice": "am_michael", "speed": 1.2,
                                     "track_slug": "circuit_de_spa_francorchamps"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert eng.calls[0][:3] == ("Turn 10", "am_michael", 1.2)


def test_speak_400_on_empty_409_when_not_installed_503_when_engine_fails(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch, kokoro_engine=FakeEngine(ok=False))
    assert c.post("/voice/speak", json={"text": "  "}).status_code == 400
    assert c.post("/voice/speak", json={"text": "hi"}).status_code == 409
    bad = FakeEngine(); bad.synthesize = lambda *a, **k: (_ for _ in ()).throw(EngineUnavailable("onnx failed"))
    c2 = _app(tmp_path, monkeypatch, kokoro_engine=bad)
    r = c2.post("/voice/speak", json={"text": "hi"})
    assert r.status_code == 503 and "onnx failed" in r.json()["detail"]


def test_kokoro_setup_and_status_endpoints(tmp_path, monkeypatch):
    class Inst:
        started = False
        def start(self): self.started = True; return True
        def status(self): return {"engine": "kokoro", "installed": False, "installing": self.started,
                                  "progress_bytes": 0, "total_bytes": 1, "error": None, "voices": []}
    c = _app(tmp_path, monkeypatch, kokoro_installer=Inst())
    assert c.post("/voice/kokoro/setup", json={}).json()["started"] is True
    assert c.get("/voice/kokoro/status").json()["engine"] == "kokoro"
```

```python
# tests/test_server_events.py (append) -- voice_setup frames for BOTH installers
def test_voice_setup_frame_carries_engine(...):
    # start a fake kokoro installer reporting installing=True; expect a frame
    # {"topic": "voice_setup", "data": {..., "engine": "kokoro"}} and the whisper one "whisper".
```

- [ ] **Step 2: Run to verify they fail** (ImportError / 404s).

- [ ] **Step 3: Implement**

`simcoach/voice.py` (append after `ModelInstaller`):

```python
KOKORO_FILES = {
    "kokoro-v1.0.onnx": (325_532_387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "voices-v1.0.bin": (28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
KOKORO_URL_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
KOKORO_TOTAL_BYTES = sum(size for size, _ in KOKORO_FILES.values())


def kokoro_dir(data_dir: str) -> Path:
    return Path(data_dir) / "kokoro"


def kokoro_installed(data_dir: str) -> bool:
    return all((kokoro_dir(data_dir) / name).exists() for name in KOKORO_FILES)


def _fetch_to(url: str, dest: Path) -> None:
    import urllib.request
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def _default_kokoro_download(dest: Path) -> None:
    import hashlib
    dest.mkdir(parents=True, exist_ok=True)
    for name, (size, sha) in KOKORO_FILES.items():
        tmp = dest / (name + ".part")
        _fetch_to(KOKORO_URL_BASE + name, tmp)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{name}: sha256 mismatch (got {digest[:12]}…) — download again")
        tmp.replace(dest / name)


class KokoroInstaller(ModelInstaller):
    """Same state machine as the whisper installer, for the Kokoro model pair."""

    def __init__(self, data_dir: str, downloader: Callable[[Path], None] | None = None):
        super().__init__(data_dir, downloader or _default_kokoro_download)
        self.dest = kokoro_dir(data_dir)
        self.voices_fn: Callable[[], list[str]] | None = None   # set by app.py once the engine exists

    def status(self) -> dict:
        installing = self._thread is not None and self._thread.is_alive()
        installed = all((self.dest / n).exists() for n in KOKORO_FILES)
        voices: list[str] = []
        if installed and self.voices_fn is not None:
            try:
                voices = self.voices_fn()
            except Exception:       # noqa: BLE001 -- a broken model reports as installed-but-no-voices; /speak says why
                voices = []
        return {"engine": "kokoro", "installed": installed, "installing": installing,
                "progress_bytes": _dir_bytes(self.dest), "total_bytes": KOKORO_TOTAL_BYTES,
                "error": self._error, "voices": voices}
```

Also add `"engine": "whisper"` to `ModelInstaller.status()`.

`simcoach/config.py`: add `"tts_engine": "windows",   # "windows" | "kokoro"` and `"kokoro_voice": "af_heart"` to the `voice` block.

`simcoach/server/app.py`:
- `create_app(..., *, transcriber=None, installer=None, kokoro_installer=None, kokoro_engine=None)`; after the whisper wiring:
  ```python
  from ..tts_kokoro import KokoroEngine, EngineUnavailable
  from ..voice import KokoroInstaller, kokoro_dir
  app.state.kokoro_installer = kokoro_installer or KokoroInstaller(voice_root)
  app.state.kokoro_engine = kokoro_engine or KokoroEngine(kokoro_dir(voice_root))
  app.state.kokoro_installer.voices_fn = app.state.kokoro_engine.voices
  ```
- endpoints next to `/voice/status`:
  ```python
  @app.post("/voice/kokoro/setup")
  def kokoro_setup(body: dict = Body(...)):
      started = app.state.kokoro_installer.start()
      return {"started": started, **app.state.kokoro_installer.status()}

  @app.get("/voice/kokoro/status")
  def kokoro_status():
      return app.state.kokoro_installer.status()

  @app.post("/voice/speak")
  async def voice_speak(body: dict = Body(...)):
      text = str(body.get("text") or "").strip()
      if not text:
          raise HTTPException(400, "text is required")
      eng = app.state.kokoro_engine
      if not eng.available():
          raise HTTPException(409, "Kokoro voice isn't installed — enable it in Settings")
      cfg = config.load_config()["voice"]
      voice = str(body.get("voice") or cfg.get("kokoro_voice") or "af_heart")
      speed = float(body.get("speed") or cfg.get("tts_rate") or 1.0)
      slug = body.get("track_slug") or (_live_track() or "")
      table = table_for(_track_file_data(slug)) if slug else {}
      loop = asyncio.get_running_loop()
      try:
          wav = await loop.run_in_executor(None, eng.synthesize, text, voice, speed, table)
      except EngineUnavailable as e:
          raise HTTPException(503, str(e))
      return Response(content=wav, media_type="audio/wav")
  ```
  (`_live_track()` already exists for `/mapcapture/status`; `table_for` from `simcoach.pronunciations`; `Response` from `fastapi`.)
- events tick: the `voice_setup` block iterates `for inst in (app.state.voice_installer, app.state.kokoro_installer)` with a per-engine `last` key (`_voice_sig_whisper`, `_voice_sig_kokoro`); the frame data is the installer's `status()` (which now carries `engine`).

- [ ] **Step 4: Run** — `tests/test_voice.py tests/test_config.py tests/test_server_voice.py tests/test_server_events.py` green, then the full backend suite.
- [ ] **Step 5: Commit** — `feat(server): Kokoro model installer, /voice/speak, engine setting`.

---

### Task 5: Packaging — extra, frozen build, CI proof

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies] voice`), `packaging/simcoach-sidecar.spec` (~25-39), `.github/workflows/build-installer.yml` (the `pip install ".[voice]"` step), `packaging/README.md`
- Test: `tests/test_packaging_spec.py`

**Interfaces:** none new. Consumes Task 3's import guard.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_packaging_spec.py (append)
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
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement**
  - `pyproject.toml`: `voice = ["faster-whisper>=1.1", "kokoro-onnx==0.6.1", "misaki==0.7.4", "onnxruntime==1.30.0", "regex>=2024.0"]`. Because `kokoro-onnx`'s own metadata requires `phonemizer`/`espeakng-loader`, the extra alone would pull them: document in `packaging/README.md` and in CI that the voice deps are installed as `pip install --only-binary=:all: onnxruntime==1.30.0 regex faster-whisper && pip install --no-deps kokoro-onnx==0.6.1 misaki==0.7.4`, and add a CI step `python -c "import importlib.util as u, sys; sys.exit(any(u.find_spec(m) for m in ('phonemizer','espeakng_loader','spacy','num2words')))"` that fails the build if any is present.
  - spec: `_ko_datas, _ko_binaries, _ko_hidden = collect_all("kokoro_onnx")`, same for `misaki` and `onnxruntime`; append to `datas`/`binaries`/`hiddenimports`; `excludes=["spacy", "phonemizer", "espeakng_loader", "num2words", "spacy_curated_transformers"]`.
  - CI: after building the exe, with the model files cached at the app-data `kokoro/` dir (a cache step keyed on the two SHA-256s; download once with `curl -L`), start the exe and `curl -f -X POST /voice/speak -H 'content-type: application/json' -d '{"text":"Turn ten."}' -o out.wav` and assert `out.wav` starts with `RIFF`. If the cache is cold, download the files first (they are ~340 MB; keep the cache).
  - `packaging/README.md`: the model path, the no-deps install, why the four packages are excluded.
- [ ] **Step 4: Run** — `tests/test_packaging_spec.py` green; build the frozen exe locally per README and run the curl proof (record the output in the report).
- [ ] **Step 5: Commit** — `build(voice): freeze kokoro-onnx + misaki, exclude GPL packages, CI proves /voice/speak`.

---

### Task 6: `KokoroSpeaker` (webview) + API client

**Files:**
- Modify: `frontend/src/lib/audio.ts` (after `SynthSpeaker`), `frontend/src/lib/api.ts` (voice section ~780-820; `VoiceSettings` ~139)
- Test: `frontend/src/lib/audio.test.ts` (new)

**Interfaces:**
- Produces: `api.speak(text, opts: {voice?: string; speed?: number; trackSlug?: string}, signal?: AbortSignal) -> Promise<ArrayBuffer>` (throws on non-2xx with the server `detail`), `api.kokoroStatus() -> Promise<KokoroStatus>` (`VoiceStatus & {engine: "kokoro"; voices: string[]}`), `api.kokoroSetup()`; `VoiceSettings.tts_engine: "windows" | "kokoro"`, `VoiceSettings.kokoro_voice: string`; `class KokoroSpeaker implements Speaker` with `voice: string`, `rate: number`, `trackSlug: string | null`, `onUnavailable?: (reason: string) => void` (called once when a request fails; the loop factory swaps to `SynthSpeaker`); constructor `new KokoroSpeaker(deps?: {fetchWav?: typeof api.speak; audio?: AudioContext})` for tests; shared `playEarcon(ctx, kind)` used by both speakers.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/audio.test.ts
import { describe, it, expect, vi } from "vitest";
import { KokoroSpeaker } from "./audio";

class FakeSource {
  onended: (() => void) | null = null;
  started = false; stopped = false;
  connect() {} start() { this.started = true; } stop() { this.stopped = true; this.onended?.(); }
}
function fakeAudio() {
  const sources: FakeSource[] = [];
  const ctx = {
    state: "running", destination: {}, resume: vi.fn(),
    decodeAudioData: vi.fn(async (buf: ArrayBuffer) => ({ duration: buf.byteLength / 1000 })),
    createBufferSource: () => { const s = new FakeSource(); sources.push(s); return s; },
    createOscillator: () => ({ frequency: { value: 0 }, connect() {}, start() {}, stop() {} }),
    createGain: () => ({ gain: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {} }),
    currentTime: 0,
  } as unknown as AudioContext;
  return { ctx, sources };
}
const wav = (n: number) => new ArrayBuffer(n);

describe("KokoroSpeaker", () => {
  it("speak() fetches, decodes, plays and resolves on ended", async () => {
    const { ctx, sources } = fakeAudio();
    const fetchWav = vi.fn(async () => wav(100));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.voice = "am_michael"; sp.rate = 1.2; sp.trackSlug = "spa";
    const done = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(1));
    expect(fetchWav).toHaveBeenCalledWith("Turn ten.", { voice: "am_michael", speed: 1.2, trackSlug: "spa" }, expect.any(AbortSignal));
    let resolved = false; void done.then(() => (resolved = true));
    await Promise.resolve(); expect(resolved).toBe(false);
    sources[0].stop();                       // fires onended
    await done; expect(resolved).toBe(true);
  });

  it("enqueue() keeps one synthesis in flight ahead of playback and idle() drains", async () => {
    const { ctx, sources } = fakeAudio();
    let resolvers: ((b: ArrayBuffer) => void)[] = [];
    const fetchWav = vi.fn(() => new Promise<ArrayBuffer>((r) => resolvers.push(r)));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.enqueue("One."); sp.enqueue("Two."); sp.enqueue("Three.");
    await Promise.resolve();
    expect(fetchWav).toHaveBeenCalledTimes(1);         // only the head is synthesising
    resolvers[0](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(1));
    expect(fetchWav).toHaveBeenCalledTimes(2);         // next one synthesises while the first plays
    sources[0].stop(); resolvers[1](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop(); resolvers[2](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(3));
    sources[2].stop();
    await sp.idle();                                   // resolves once everything played
  });

  it("cancel() aborts the in-flight fetch, stops playback, clears the queue and resolves idle()", async () => {
    const { ctx, sources } = fakeAudio();
    const seen: AbortSignal[] = [];
    const fetchWav = vi.fn((_t: string, _o: unknown, signal: AbortSignal) => { seen.push(signal); return new Promise<ArrayBuffer>(() => {}); });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.enqueue("One."); sp.enqueue("Two.");
    await Promise.resolve();
    sp.cancel();
    expect(seen[0].aborted).toBe(true);
    await sp.idle();
    expect(fetchWav).toHaveBeenCalledTimes(1);
    expect(sources.every((s) => s.stopped || !s.started)).toBe(true);
  });

  it("reports the first failure through onUnavailable once and resolves the caller", async () => {
    const { ctx } = fakeAudio();
    const fetchWav = vi.fn(async () => { throw new Error("Kokoro voice isn't installed"); });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;
    await sp.speak("a"); await sp.speak("b");
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(onUnavailable).toHaveBeenCalledWith("Kokoro voice isn't installed");
  });
});
```

- [ ] **Step 2: Run to verify it fails** — `cd frontend && npx vitest run src/lib/audio.test.ts` → `KokoroSpeaker` is not exported.

- [ ] **Step 3: Implement**

`api.ts`:
```ts
export interface KokoroStatus extends VoiceStatus { engine: "kokoro"; voices: string[] }
export function kokoroStatus(): Promise<KokoroStatus> { return request<KokoroStatus>("/voice/kokoro/status"); }
export function kokoroSetup(): Promise<KokoroStatus & { started: boolean }> {
  return request("/voice/kokoro/setup", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({}) });
}
export async function speak(text: string, opts: { voice?: string; speed?: number; trackSlug?: string }, signal?: AbortSignal): Promise<ArrayBuffer> {
  const res = await fetch(`${BASE}/voice/speak`, {
    method: "POST", headers: JSON_HEADERS, signal,
    body: JSON.stringify({ text, voice: opts.voice, speed: opts.speed, track_slug: opts.trackSlug }),
  });
  if (!res.ok) {
    let detail = "couldn't synthesise speech";
    try { const b = await res.json(); if (typeof b?.detail === "string") detail = b.detail; } catch { /* keep default */ }
    throw new Error(detail);
  }
  return res.arrayBuffer();
}
```
and `tts_engine: "windows" | "kokoro"; kokoro_voice: string;` on `VoiceSettings` (update every test fixture that builds a `VoiceSettings` literal — grep `tts_rate:` in `frontend/src`).

`audio.ts`: extract the body of `SynthSpeaker.earcon` into `export function playEarcon(ctx: AudioContext, kind: Earcon): void` (SynthSpeaker keeps its lazy `#beepCtx` and calls it), then:

```ts
export class KokoroSpeaker implements Speaker {
  voice = "af_heart";
  rate = 1.0;
  trackSlug: string | null = null;
  onUnavailable: ((reason: string) => void) | null = null;
  #fetchWav: typeof apiSpeak;
  #ctx: AudioContext | null;
  #queue: { text: string; resolve: () => void }[] = [];
  #ahead: { buffer: Promise<AudioBuffer | null>; resolve: () => void; abort: AbortController } | null = null;
  #playing: AudioBufferSourceNode | null = null;
  #idleWaiters: (() => void)[] = [];
  #failed = false;

  constructor(deps: { fetchWav?: typeof apiSpeak; audio?: AudioContext } = {}) {
    this.#fetchWav = deps.fetchWav ?? apiSpeak;
    this.#ctx = deps.audio ?? null;
  }
  #audio(): AudioContext {
    this.#ctx ??= new AudioContext();
    if (this.#ctx.state === "suspended") void this.#ctx.resume();
    return this.#ctx;
  }
  speak(text: string): Promise<void> { return new Promise((resolve) => { this.#queue.push({ text, resolve }); this.#pump(); }); }
  enqueue(text: string): void { this.#queue.push({ text, resolve: () => {} }); this.#pump(); }
  idle(): Promise<void> {
    if (!this.#queue.length && !this.#ahead && !this.#playing) return Promise.resolve();
    return new Promise((r) => this.#idleWaiters.push(r));
  }
  cancel(): void {
    this.#ahead?.abort.abort(); this.#ahead?.resolve(); this.#ahead = null;
    try { this.#playing?.stop(); } catch { /* already stopped */ }
    this.#playing = null;
    for (const q of this.#queue.splice(0)) q.resolve();
    this.#settleIdle();
  }
  earcon(kind: Earcon): void { playEarcon(this.#audio(), kind); }

  #settleIdle(): void {
    if (this.#queue.length || this.#ahead || this.#playing) return;
    const w = this.#idleWaiters; this.#idleWaiters = []; w.forEach((f) => f());
  }
  // One synthesis in flight ahead of playback: fetch the head while the previous plays.
  #pump(): void {
    if (this.#ahead || !this.#queue.length) return;
    const item = this.#queue.shift()!;
    const abort = new AbortController();
    const buffer = this.#fetchWav(item.text, { voice: this.voice, speed: this.rate, trackSlug: this.trackSlug ?? undefined }, abort.signal)
      .then((bytes) => this.#audio().decodeAudioData(bytes))
      .catch((e: unknown) => { this.#fail(e); return null; });
    this.#ahead = { buffer, resolve: item.resolve, abort };
    void this.#play();
  }
  async #play(): Promise<void> {
    const ahead = this.#ahead!;
    const buf = await ahead.buffer;
    if (this.#ahead !== ahead) return;               // cancelled while synthesising
    await this.#drainPlaying();
    this.#ahead = null;
    this.#pump();                                    // start the NEXT synthesis before this one plays
    if (!buf) { ahead.resolve(); this.#settleIdle(); return; }
    const src = this.#audio().createBufferSource();
    src.buffer = buf; src.connect(this.#audio().destination);
    this.#playing = src;
    await new Promise<void>((done) => { src.onended = () => done(); src.start(); });
    if (this.#playing === src) this.#playing = null;
    ahead.resolve();
    this.#settleIdle();
  }
  #drainPlaying(): Promise<void> {
    const cur = this.#playing;
    return cur ? new Promise((r) => { const prev = cur.onended; cur.onended = (e) => { prev?.call(cur, e); r(); }; }) : Promise.resolve();
  }
  #fail(e: unknown): void {
    if (this.#failed) return;
    this.#failed = true;
    this.onUnavailable?.(e instanceof Error ? e.message : String(e));
  }
}
```
Adjust the pump/play ordering so the SECOND fetch starts only once the first buffer is decoded and playing (the test expects exactly one in flight until the first resolves). Keep the ordering invariant: sentences play in enqueue order.

- [ ] **Step 4: Run** — `npx vitest run src/lib/audio.test.ts` then the full frontend suite and `svelte-check`.
- [ ] **Step 5: Commit** — `feat(ui): KokoroSpeaker plays sidecar-synthesised sentences with one synthesis ahead`.

---

### Task 7: Engine factory + Settings (engine select, dialog, preview, notice)

**Files:**
- Modify: `frontend/src/lib/ChatPanel.svelte` (~163-190), `frontend/src/lib/SettingsScreen.svelte` (voice section ~150-260 script, ~555-665 markup), `frontend/src/lib/voice.ts` (only if `speakMessage` needs the factory)
- Test: `frontend/src/lib/ChatPanel.test.ts`, `frontend/src/lib/SettingsScreen.test.ts`

**Interfaces:**
- Consumes: `KokoroSpeaker`, `SynthSpeaker`, `kokoroStatus`, `kokoroSetup`, `speak`, `eventFeed.voiceSetup` (frames now carry `engine`).
- Produces: `makeSpeaker(v: VoiceSettings, kokoroInstalled: boolean, onFallback: (reason: string) => void): Speaker` exported from `audio.ts` (returns `KokoroSpeaker` when `v.tts_engine === "kokoro" && kokoroInstalled`, else `SynthSpeaker`; sets voice/rate on either).

- [ ] **Step 1: Write the failing tests**

```ts
// ChatPanel.test.ts (append; follow the file's existing voice fixtures)
it("uses KokoroSpeaker when the engine is kokoro and installed, SynthSpeaker otherwise", ...)
// mock kokoroStatus -> {installed:true, voices:["af_heart"]}, settings.voice.tts_engine="kokoro";
// assert the loop's speaker is a KokoroSpeaker (export a test hook or check `activeVoice.speakerKind`);
// flip installed:false -> SynthSpeaker and a one-line notice text "Kokoro voice isn't installed — using Windows voices" is rendered in the composer status area.

// SettingsScreen.test.ts (append)
it("Engine select offers Windows and Kokoro; picking Kokoro without the model opens the setup dialog and Not now reverts", ...)
it("Kokoro voice select lists the installed voices and Preview posts one fixed sentence", ...)
it("voice_setup frames with engine kokoro drive the dialog progress", ...)
```
Write these against the real DOM the way `SettingsScreen.test.ts` already drives the whisper dialog (read that file's "Download"/"Not now"/"Ready" tests and mirror them with `kokoroStatus`/`kokoroSetup` mocks and `eventFeed.push("voice_setup", {... engine: "kokoro"})`).

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**
  - `audio.ts`: `makeSpeaker` as above.
  - `ChatPanel.svelte`: replace `const speaker = new SynthSpeaker(); speaker.voiceName = ...; speaker.rate = ...` with `const ks = v.tts_engine === "kokoro" ? await kokoroStatus().catch(() => null) : null; const speaker = makeSpeaker(v, !!ks?.installed, (reason) => { voiceNotice = reason; rebuildWithWindows(); });` where `rebuildWithWindows()` swaps `activeVoice`'s speaker for a `SynthSpeaker` (add `VoiceLoop.setSpeaker(s: Speaker)` in `voice.ts` — a one-line setter; the loop already holds `speaker` in its options). Render `voiceNotice` in the existing `voice-status` live region. Kokoro's `trackSlug` = the live status track slug from `eventFeed.status` (`track_slug(track)` equivalent: reuse the slug helper the frontend already has, or send the track NAME and let the server slug it — pick the server side: extend `/voice/speak` to accept `track` and slug it; update Task 4's endpoint and test accordingly).
  - `SettingsScreen.svelte`: an "Engine" field before "Voice": `<select aria-label="Engine" bind:value={settings.voice.tts_engine} onchange={onEngineChange}>` with `windows`/`kokoro`; `onEngineChange` saves, then if `kokoro` and `kokoroStatus().installed` is false → `showKokoroDialog = true` (same dialog markup as whisper with the copy "Nestor downloads the Kokoro voice model (about 340 MB, one time)…"; Not now → `tts_engine = "windows"` + save; Download → `kokoroSetup()`; progress from `eventFeed.voiceSetup` frames with `engine === "kokoro"`; Ready → Done). The "Voice" field shows the speechSynthesis list when `tts_engine === "windows"`, else `kokoroStatusInfo.voices` bound to `settings.voice.kokoro_voice`; a "Preview" button next to it speaks `"Brake later into turn four, the fast left after the long straight."` through `makeSpeaker(...)`. A status line `Kokoro voice isn't installed — using Windows voices` when `tts_engine === "kokoro"` and not installed (outside the dialog).
- [ ] **Step 4: Run** — full frontend suite + `svelte-check` 0 errors.
- [ ] **Step 5: Commit** — `feat(ui): engine select, Kokoro setup dialog, voice preview, Windows fallback notice`.

---

### Task 8: Pronunciation harvest script + shipped tables

**Files:**
- Create: `scripts/harvest_pronunciations.py`
- Modify: the shipped track files under `simcoach/tracks/` EXCEPT `fuji_speedway.json` and `silverstone_national_circuit.json` (untracked; leave them), adding a `pronunciations` block each
- Test: `tests/test_pronunciations.py` (append: every shipped file's block validates and every phoneme char is in Kokoro's vocab)

**Interfaces:**
- Consumes: `pronunciations.validated_pronunciations`; `kokoro_onnx.config.DEFAULT_VOCAB` (read via the adapter in the test).
- Produces: `pronunciations` blocks (data only).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pronunciations.py (append)
import json, pathlib
from simcoach import tts_kokoro

def test_every_shipped_map_pronunciation_block_validates_and_fits_the_vocab():
    tts_kokoro.install_stubs()
    from kokoro_onnx.config import DEFAULT_VOCAB   # skip if kokoro_onnx not installed
    for p in pathlib.Path("simcoach/tracks").glob("*.json"):
        if p.name in ("test_track.json",):
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if "pronunciations" not in data:
            continue
        table = validated_pronunciations(data["pronunciations"])
        for name, ph in table.items():
            bad = [c for c in ph if c not in DEFAULT_VOCAB and c != " "]
            assert not bad, f"{p.name}: {name!r} has chars outside Kokoro's vocab: {bad}"
    assert any("pronunciations" in json.loads(p.read_text(encoding="utf-8"))
               for p in pathlib.Path("simcoach/tracks").glob("*.json") if p.name == "circuit_de_spa_francorchamps.json")
```

- [ ] **Step 2: Run to verify it fails** (Spa has no block).
- [ ] **Step 3: Implement the script**

```python
# scripts/harvest_pronunciations.py
"""DEV ONLY. Harvest Kokoro phonemes for a track's corner and section names through
espeak-ng (GPL) so the SHIPPED table carries only data. Run from a throwaway venv:
    python -m venv .venv-harvest && .venv-harvest/Scripts/pip install kokoro-onnx==0.6.1 phonemizer espeakng-loader
    .venv-harvest/Scripts/python scripts/harvest_pronunciations.py circuit_de_spa_francorchamps [--write]
Prints the block; --write merges it into simcoach/tracks/<slug>.json (keys that already
exist are kept unless --force). Only names whose phonemes are entirely inside Kokoro's
vocabulary are written; the rest are listed for hand-pinning.
"""
import argparse, json, pathlib, sys

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("slug"); ap.add_argument("--write", action="store_true"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    path = pathlib.Path("simcoach/tracks") / f"{a.slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    names = sorted({c["name"] for c in data.get("corners", []) if c.get("name")} | {s["name"] for s in data.get("sections", []) if s.get("name")})
    from kokoro_onnx.tokenizer import Tokenizer      # real GPL path, dev machine only
    from kokoro_onnx.config import DEFAULT_VOCAB
    tok = Tokenizer()
    table, rejected = {}, []
    for n in names:
        ph = tok.phonemize(n, lang="en-us").strip()
        if ph and all(c in DEFAULT_VOCAB or c == " " for c in ph):
            table[n] = ph
        else:
            rejected.append((n, ph))
    print(json.dumps(table, ensure_ascii=False, indent=2))
    for n, ph in rejected:
        print(f"REJECTED {n!r}: {ph!r}", file=sys.stderr)
    if a.write:
        existing = data.get("pronunciations") or {}
        merged = {**table, **existing} if not a.force else {**existing, **table}
        data["pronunciations"] = dict(sorted(merged.items()))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(merged)} entries to {path}", file=sys.stderr)

if __name__ == "__main__":
    main()
```
Run it with `--write` for every shipped slug (the throwaway harvest venv lives OUTSIDE the repo, e.g. the session scratchpad; the two untracked files are skipped). Write the shipped JSON through the same writer the repo uses for track files if one exists (`_write_track_json` is server-side; for the script, plain `json.dump` with the file's existing indent is acceptable — check `git diff` shows ONLY the new block).

- [ ] **Step 4: Run** — `tests/test_pronunciations.py` green; `git diff --stat simcoach/tracks` touches only shipped files and only adds `pronunciations`.
- [ ] **Step 5: Commit** — `feat(tracks): harvested Kokoro pronunciation tables for the shipped maps + harvest script`.

---

## Self-review (done while writing)

- Spec coverage: config keys (T4), installer + endpoints + frame (T4), adapter/G2P/preprocess/retry/WAV (T3), numbers (T1), table + harvest (T2, T8), `KokoroSpeaker` + factory + Settings (T6, T7), packaging + CI proof (T5), error handling (T3 retry/drop, T4 status codes, T6 `onUnavailable`, T7 fallback + notice). Rig checks are in the spec's Testing section, not a task.
- Placeholders: the T7 test bodies are described rather than fully written because they must mirror `SettingsScreen.test.ts`'s existing dialog tests; the implementer reads that file first (named in the step). Everything else carries code.
- Type consistency: `synthesize(text, voice, speed, table)` (T3) ↔ `run_in_executor(None, eng.synthesize, text, voice, speed, table)` (T4) ↔ `FakeEngine.synthesize(text, voice, speed, table=None)` (T4 tests); `api.speak(text, {voice, speed, trackSlug}, signal)` (T6) ↔ `KokoroSpeaker` call (T6 test); `status()["voices"]` (T4) ↔ `KokoroStatus.voices` (T6/T7); `makeSpeaker` (T7) defined in `audio.ts`.
- Open point folded in: T7 decides the server slugs the track (`/voice/speak` accepts `track` as well as `track_slug`); T4's implementer adds the `track` alias and a test line for it.
