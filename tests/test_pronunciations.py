import json
import pathlib

import pytest

from simcoach import tts_kokoro
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


def test_every_shipped_map_pronunciation_block_validates_and_fits_the_vocab():
    tts_kokoro.install_stubs()
    pytest.importorskip("kokoro_onnx")
    from kokoro_onnx.config import DEFAULT_VOCAB

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
    assert any(
        "pronunciations" in json.loads(p.read_text(encoding="utf-8"))
        for p in pathlib.Path("simcoach/tracks").glob("*.json")
        if p.name == "circuit_de_spa_francorchamps.json"
    )
