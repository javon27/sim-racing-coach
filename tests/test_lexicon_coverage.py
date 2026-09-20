"""The point of the whole #118/#123 exercise: every name the app ships (track display
names, corner names, section names) must actually speak -- no OOV word, no spelling it
out letter by letter. Walks simcoach/tracks/*.json AT TEST TIME (not a hardcoded list),
so a new track file fails this test until its names get lexicon coverage.

car_lookup.py resolves car names live from the driver's own result logs (#41) -- there
is no shipped car-name list to walk, so there's nothing to add the same check for.
"""
import json
import pathlib
import string

import pytest

pytest.importorskip("misaki")

from simcoach.pronunciations import apply, table_for
from simcoach.tts_kokoro import _split_markup, phonemize

TRACKS_DIR = pathlib.Path("simcoach/tracks")

try:
    phonemize("lap")
except Exception as e:
    pytest.skip(f"phonemize can't run in this environment: {e!r}", allow_module_level=True)


def _track_files():
    return sorted(p for p in TRACKS_DIR.glob("*.json") if p.name != "test_track.json")


def _shipped_names(data: dict) -> list[tuple[str, str]]:
    names = []
    if data.get("name"):
        names.append(("track name", data["name"]))
    for c in data.get("corners") or []:
        if c.get("name"):
            names.append(("corner", c["name"]))
    for s in data.get("sections") or []:
        if s.get("name"):
            names.append(("section", s["name"]))
    return names


def test_every_shipped_track_corner_and_section_name_phonemizes_with_the_global_lexicon():
    word_ok: dict[str, bool] = {}

    def probe(word: str) -> bool:
        if word not in word_ok:
            try:
                phonemize(word)
                word_ok[word] = True
            except Exception:
                word_ok[word] = False
        return word_ok[word]

    failures = []
    for path in _track_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        table = table_for(data)
        for kind, name in _shipped_names(data):
            marked = apply(name, table)
            for tok, is_markup in _split_markup(marked):
                if is_markup:
                    continue
                word = tok.strip(string.punctuation)
                if word and not probe(word):
                    failures.append(f"{path.name}: {kind} {name!r} -- word {word!r} has no G2P mapping")

    assert not failures, "shipped names with no TTS coverage (add a lexicon entry):\n" + "\n".join(failures)
