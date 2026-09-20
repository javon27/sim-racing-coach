import json
import logging
import pathlib

import pytest

from simcoach import pronunciations, tts_kokoro
from simcoach.pronunciations import apply, table_for, validated_pronunciations

TABLE = {"Pouhon": "pˈaʊhɑːn", "Eau Rouge": "ˈoʊ ɹˈuːʒ", "Raidillon": "ɹˈeɪdɪlən"}


@pytest.fixture
def fresh_global_lexicon(monkeypatch, tmp_path):
    """Points _global_table() at a throwaway file (missing until written) instead of
    the real shipped lexicon.json -- these tests shouldn't couple to its content."""
    path = tmp_path / "lexicon.json"
    monkeypatch.setattr(pronunciations, "_LEXICON_PATH", path)
    pronunciations._global_table.cache_clear()
    yield path
    pronunciations._global_table.cache_clear()   # next caller reloads the real file


def test_apply_rewrites_whole_words_longest_first_case_insensitive():
    assert apply("Eau Rouge and Raidillon were flat; pouhon too.", TABLE) == (
        "[Eau Rouge](/ˈoʊ ɹˈuːʒ/) and [Raidillon](/ɹˈeɪdɪlən/) were flat; [pouhon](/pˈaʊhɑːn/) too.")


def test_apply_does_not_touch_substrings_or_existing_markup():
    assert apply("Pouhonx", TABLE) == "Pouhonx"
    assert apply("[Pouhon](/x/)", TABLE) == "[Pouhon](/x/)"


def test_apply_with_empty_table_is_identity():
    assert apply("Turn 10, Pouhon", {}) == "Turn 10, Pouhon"


def test_apply_longest_first_holds_across_a_global_plus_track_merge():
    # table_for's merge is a plain dict union; apply()'s longest-key-first sort must
    # still prefer a longer global entry over a shorter track entry that's a substring
    # of it (and vice versa) -- the sort doesn't know or care which side a key came from.
    merged = {"Les Fagnes": "GLOBAL", "Les": "TRACK"}
    assert apply("Les Fagnes is fast.", merged) == "[Les Fagnes](/GLOBAL/) is fast."


def test_table_for_with_no_track_returns_the_global_lexicon(fresh_global_lexicon):
    fresh_global_lexicon.write_text(json.dumps({"Pouhon": "pˈaʊhɑːn"}), encoding="utf-8")
    assert table_for({}) == {"Pouhon": "pˈaʊhɑːn"}


def test_table_for_track_block_overrides_a_global_key(fresh_global_lexicon):
    fresh_global_lexicon.write_text(json.dumps({"Pouhon": "WRONG", "Kemmel": "kˈɛməl"}), encoding="utf-8")
    assert table_for({"pronunciations": {"Pouhon": "pˈaʊhɑːn"}}) == {
        "Pouhon": "pˈaʊhɑːn", "Kemmel": "kˈɛməl"}


def test_table_for_tolerates_a_junk_track_block(fresh_global_lexicon):
    fresh_global_lexicon.write_text(json.dumps({"Pouhon": "pˈaʊhɑːn"}), encoding="utf-8")
    assert table_for({}) == {"Pouhon": "pˈaʊhɑːn"}
    assert table_for({"pronunciations": ["nope"]}) == {"Pouhon": "pˈaʊhɑːn"}
    assert table_for({"pronunciations": {"Raidillon": 3}}) == {"Pouhon": "pˈaʊhɑːn"}


def test_global_table_missing_file_yields_empty_and_logs(fresh_global_lexicon, caplog):
    with caplog.at_level(logging.WARNING):
        assert pronunciations._global_table() == {}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_global_table_invalid_json_yields_empty_and_logs(fresh_global_lexicon, caplog):
    fresh_global_lexicon.write_text("not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert pronunciations._global_table() == {}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_global_table_wrong_shape_yields_empty_and_logs(fresh_global_lexicon, caplog):
    fresh_global_lexicon.write_text(json.dumps(["nope"]), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert pronunciations._global_table() == {}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


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


def test_shipped_lexicon_json_validates_and_fits_the_vocab():
    tts_kokoro.install_stubs()
    pytest.importorskip("kokoro_onnx")
    from kokoro_onnx.config import DEFAULT_VOCAB

    data = json.loads(pathlib.Path("simcoach/lexicon.json").read_text(encoding="utf-8"))
    table = validated_pronunciations(data)
    assert table   # not empty -- a needless entry costs nothing, but zero would mean nothing was authored
    for name, ph in table.items():
        bad = [c for c in ph if c not in DEFAULT_VOCAB and c != " "]
        assert not bad, f"lexicon.json: {name!r} has chars outside Kokoro's vocab: {bad}"
