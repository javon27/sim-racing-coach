"""#10: promoting a user-built map into the committed shipped set.

Every test writes to tmp_path -- nothing here touches simcoach/tracks/.
"""
import json
import logging
import math
import shutil
from pathlib import Path

import pytest

from scripts import promote_map
from scripts.promote_map import PromoteError, main, promote
from simcoach.corners import CornerMap

SHIPPED_DIR = Path(__file__).resolve().parents[1] / "simcoach" / "tracks"

_LEN = 1000.0


def _ring_path(n=400):
    """A circle of circumference ~_LEN, so lap_distance runs 0.._LEN and every corner
    has real path geometry for direction_from_path to read (one consistent hand)."""
    r = _LEN / (2 * math.pi)
    dist = [_LEN * i / n for i in range(n)]
    path = [[r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)]
            for i in range(n)]
    return path, dist


def _corner(number, start, apex, end, name="Turn"):
    return {"number": number, "label": str(number), "name": f"{name} {number}",
            "direction": "right", "start_dist": start, "apex_dist": apex,
            "end_dist": end, "apexes": [apex]}


def _map(**over):
    path, dist = _ring_path()
    data = {
        "track": "user_built_circuit", "name": "User Built Circuit", "length": _LEN,
        "path": path, "path_dist": dist, "sectors": [300.0, 700.0], "sections": [],
        "rotation": 0.0,
        "corners": [_corner(1, 100.0, 150.0, 200.0), _corner(2, 400.0, 450.0, 500.0),
                    _corner(3, 700.0, 750.0, 800.0)],
    }
    data.update(over)
    return data


def _src(tmp_path, data, name="user_built_circuit.json"):
    p = tmp_path / "userdata" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _tracks(tmp_path):
    d = tmp_path / "tracks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_promotes_a_user_map_into_the_shipped_dir(tmp_path):
    out = promote(_src(tmp_path, _map()), _tracks(tmp_path))
    assert out == _tracks(tmp_path) / "user_built_circuit.json"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["track"] == "user_built_circuit"
    assert [c["number"] for c in written["corners"]] == [1, 2, 3]
    assert written["length"] == _LEN and len(written["path"]) == len(written["path_dist"])


def test_written_file_matches_the_shipped_byte_format(tmp_path):
    """The shipped maps are 2-space JSON, real UTF-8 (not \\uXXXX escapes), CRLF, and
    end WITHOUT a newline. A promotion that drifted would land a whole-file diff."""
    raw = promote(_src(tmp_path, _map(name="Autódromo José")),
                  _tracks(tmp_path)).read_bytes()
    assert b"\xc3\xb3" in raw and b"\\u00f3" not in raw   # ensure_ascii=False
    assert raw.count(b"\r\n") == raw.count(b"\n")         # no bare LF anywhere
    assert not raw.endswith(b"\n")
    assert raw.startswith(b'{\r\n  "track"')


def test_refuses_to_replace_a_shipped_map_without_force(tmp_path):
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map()), tracks)
    before = (tracks / "user_built_circuit.json").read_bytes()
    with pytest.raises(PromoteError, match="already ships"):
        promote(_src(tmp_path, _map(name="Renamed")), tracks)
    assert (tracks / "user_built_circuit.json").read_bytes() == before   # untouched
    promote(_src(tmp_path, _map(name="Renamed")), tracks, force=True)
    assert json.loads((tracks / "user_built_circuit.json").read_text("utf-8"))["name"] \
        == "Renamed"


def test_refuses_to_reformat_a_shipped_file_whose_format_has_drifted(tmp_path):
    """Byte fidelity: if this writer can't reproduce the file it's replacing, the
    promotion would silently restyle it. Abort instead."""
    tracks = _tracks(tmp_path)
    (tracks / "user_built_circuit.json").write_bytes(
        json.dumps(_map(), indent=2).encode("utf-8"))     # LF, not the shipped CRLF
    with pytest.raises(PromoteError, match="format has drifted"):
        promote(_src(tmp_path, _map()), tracks, force=True)


def test_refuses_an_annotation_only_map_with_no_path_geometry(tmp_path):
    """Corner direction is owned data derived from path geometry. An overlay carries
    corners but no path, so promoting one would ship unverifiable directions."""
    data = _map()
    del data["path"], data["path_dist"]
    with pytest.raises(PromoteError, match="direction can't be re-derived"):
        promote(_src(tmp_path, data), _tracks(tmp_path))


def test_direction_is_re_derived_not_taken_from_the_file(tmp_path):
    """A hand-edited export claiming the wrong direction must not reach the shipped
    set: the ring turns one way, so the promoted corners are identical either way."""
    lied = _map()
    for c in lied["corners"]:
        c["direction"] = "left" if c["direction"] == "right" else "right"
    honest = promote(_src(tmp_path, _map()), _tracks(tmp_path)).read_text("utf-8")
    written = promote(_src(tmp_path, lied), _tracks(tmp_path), force=True).read_text("utf-8")
    assert json.loads(written)["corners"] == json.loads(honest)["corners"]
    assert len({c["direction"] for c in json.loads(written)["corners"]}) == 1


def test_rejects_a_degenerate_window_an_apex_outside_it_and_a_duplicate_number(tmp_path):
    degen = _map()
    degen["corners"][0]["end_dist"] = degen["corners"][0]["start_dist"]
    with pytest.raises(PromoteError, match="degenerate"):
        promote(_src(tmp_path, degen), _tracks(tmp_path))

    outside = _map()
    outside["corners"][0]["apex_dist"] = 650.0     # not inside [100, 200]
    with pytest.raises(PromoteError, match="apex_dist must be inside"):
        promote(_src(tmp_path, outside), _tracks(tmp_path))

    dupe = _map()
    dupe["corners"][1]["number"] = 1
    with pytest.raises(PromoteError, match="duplicate corner number"):
        promote(_src(tmp_path, dupe), _tracks(tmp_path))


def test_promoted_file_satisfies_the_on_disk_window_invariant(tmp_path):
    """Overlapping windows in the source are clamped BEFORE writing, so the promoted
    file passes tests/test_corners.py's raw-JSON regression check -- CornerMap.load
    re-clamps on read, so an unclamped file would otherwise look fine in the app."""
    data = _map()
    data["corners"][0]["end_dist"] = 420.0     # bleeds into corner 2's window
    corners = json.loads(promote(_src(tmp_path, data), _tracks(tmp_path))
                         .read_text("utf-8"))["corners"]
    assert corners[0]["end_dist"] <= corners[1]["start_dist"]
    for c in corners:
        assert c["start_dist"] <= c["apex_dist"] <= c["end_dist"]


def test_authored_notes_survive_but_a_stale_corner_key_is_dropped(tmp_path):
    """The editor's save guard, not bypassed: a note keyed to a label that no longer
    exists would rebind to a different corner, which the coach states as fact."""
    data = _map(notes={"overview": "keep me",
                       "corners": {"1": "brake late", "7": "no such corner"}})
    written = json.loads(promote(_src(tmp_path, data), _tracks(tmp_path)).read_text("utf-8"))
    assert written["notes"]["overview"] == "keep me"
    assert written["notes"]["corners"] == {"1": "brake late"}


def test_force_carries_the_shipped_maps_notes_onto_the_replacements_corners(tmp_path):
    """--force must run the notes guard, not bypass it. The shipped map's notes are
    identified against the SHIPPED corners: corner 3 is relabelled '3A' in the
    replacement (same apex), so its note moves onto '3A' rather than being deleted (#64),
    and a shipped note whose corner the replacement drops is not rebound to anything."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map(notes={"overview": "curator wrote this",
                                       "corners": {"2": "late apex", "3": "flat"}})),
            tracks)
    replacement = _map(notes={"corners": {"1": "from the driver"}})
    replacement["corners"][2]["label"] = "3A"     # corner 3 relabelled, same apex
    written = json.loads(promote(_src(tmp_path, replacement), tracks, force=True)
                         .read_text("utf-8"))
    assert written["notes"]["overview"] == "curator wrote this"   # not corner-keyed
    assert written["notes"]["corners"] == {"1": "from the driver", "2": "late apex",
                                           "3A": "flat"}


def test_force_drops_a_shipped_note_whose_corner_the_replacement_removes(tmp_path):
    """The other side: the replacement has no corner at 750 m at all (it has one at
    600 m and one at 900 m instead), so the shipped note about the 750 m corner cannot
    be carried -- and '3' must not land on whatever the replacement happens to call '3'."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map(notes={"corners": {"2": "late apex", "3": "flat"}})), tracks)
    replacement = _map()
    replacement["corners"] = [_corner(1, 100.0, 150.0, 200.0), _corner(2, 400.0, 450.0, 500.0),
                              _corner(3, 550.0, 600.0, 650.0), _corner(4, 850.0, 900.0, 950.0)]
    written = json.loads(promote(_src(tmp_path, replacement), tracks, force=True)
                         .read_text("utf-8"))
    assert written["notes"]["corners"] == {"2": "late apex"}


def test_range_keyed_note_survives_a_same_count_promote(tmp_path):
    """A note key may name a COMPLEX ('2-3'), which is never itself a corner label.
    Testing the raw key against the label set classifies every such key stale and
    deletes it -- 16 of the 135 shipped note keys, on a promote whose corner count
    never changed. Ask note_key_binds what the key names instead."""
    data = _map(notes={"corners": {"2-3": "one flowing complex", "1": "brake late",
                                   "7-9": "no such corners"}})
    written = json.loads(promote(_src(tmp_path, data), _tracks(tmp_path)).read_text("utf-8"))
    assert written["notes"]["corners"] == {"2-3": "one flowing complex", "1": "brake late"}


@pytest.mark.parametrize("sections", [
    [{"name": "x", "start_dist": 900.0, "end_dist": 100.0}],     # #65's table, row by row
    [{"name": "x", "start_dist": 0.0, "end_dist": 99999.0}],
    [{"name": "x", "start_dist": 0.0}],
    ["nope"],
])
def test_a_section_the_app_cannot_open_is_refused(tmp_path, sections):
    """#65: `sections` was written verbatim, and CornerMap.from_data RAISES on each of
    these -- a promote committed a map no client can open. Fixture sanity first, so the
    refusal below is proven to be about a map that really is unloadable."""
    data = _map(sections=sections)
    with pytest.raises((ValueError, KeyError, TypeError)):
        CornerMap.from_data(data)
    with pytest.raises(PromoteError):
        promote(_src(tmp_path, data), _tracks(tmp_path))


@pytest.mark.parametrize("key, value", [("radius_m", "banana"), ("banking_deg", "3"),
                                        ("width_m", -1e9), ("radius_m", True),
                                        ("elev_delta_m", 10**400)])
def test_unusable_baked_geometry_is_refused(tmp_path, key, value):
    """#65: every per-corner key was carried through `{**c, ...}` unexamined. A string
    radius loads fine and then crashes geometry_clause on the coach's answer."""
    data = _map()
    data["corners"][1][key] = value
    with pytest.raises(PromoteError, match=key):
        promote(_src(tmp_path, data), _tracks(tmp_path))


def test_promote_writes_the_validated_sectors_not_the_source_value(tmp_path):
    """#65: sectors were validated and the result DISCARDED, so strings passed the check
    as numbers and shipped as strings. And `true` is refused, not shipped as a 1 m split."""
    written = json.loads(promote(_src(tmp_path, _map(sectors=["300", "700"])),
                                 _tracks(tmp_path)).read_text("utf-8"))
    assert written["sectors"] == [300.0, 700.0]
    assert [type(s) for s in written["sectors"]] == [float, float]
    with pytest.raises(PromoteError):
        promote(_src(tmp_path, _map(sectors=[True])), _tracks(tmp_path), force=True)


@pytest.mark.parametrize("bad", ["2-3: brake late", 5])
def test_a_non_object_notes_corners_is_ignored_with_a_warning(tmp_path, bad, caplog):
    """`notes` here comes straight out of an arbitrary user JSON file, so a truthy
    non-dict `corners` is input, not a bug: it used to reach .items() and surface as a
    raw AttributeError traceback instead of the tool's `refused:`/`promoted` line.

    It carries no authored per-corner prose to preserve, so the promote proceeds -- but
    it is a plausible typo for the object form, and a silent success that ships no notes
    at all is how someone loses them without knowing. Say what was ignored."""
    data = _map(notes={"overview": "keep me", "corners": bad})
    with caplog.at_level(logging.WARNING, logger="simcoach.track_lookup"):
        out = promote(_src(tmp_path, data), _tracks(tmp_path))
    assert json.loads(out.read_text("utf-8"))["notes"] == {"overview": "keep me", "corners": {}}
    assert "non-object `notes.corners`" in caplog.text
    assert repr(bad) in caplog.text             # what it saw, so the typo is findable
    assert type(bad).__name__ in caplog.text


def test_self_promoting_a_shipped_map_is_a_byte_identical_no_op(tmp_path):
    """The operation a curator thinks is safest -- re-promoting a shipped map from its
    own file with --force -- must change nothing at all. It is the whole shipped set's
    regression test for this writer: format drift, a dropped field, or a note the
    guard wrongly classifies stale all show up as a byte diff here.

    Copies into tmp_path; simcoach/tracks/ is read-only to this test."""
    # Only the FULL maps: a promotable map carries reconstructed path geometry, which is
    # what lets corner direction be re-derived. Two shipped files are deliberately not
    # that and promote refuses both -- test_track.json (a fixture, no length) and
    # circuit_de_spa_francorchamps.json (hand-authored corners, `length_m`, no path).
    maps = [p for p in sorted(SHIPPED_DIR.glob("*.json"))
            if (d := json.loads(p.read_text(encoding="utf-8"))).get("length") and d.get("path")]
    assert len(maps) >= 11, "expected the shipped corner maps"
    src_dir, tracks = tmp_path / "src", _tracks(tmp_path)
    src_dir.mkdir()
    for p in maps:
        shutil.copyfile(p, src_dir / p.name)      # the user's own copy of the shipped map
        shutil.copyfile(p, tracks / p.name)       # the shipped map --force replaces
        out = promote(src_dir / p.name, tracks, slug=p.stem, force=True)
        assert out.read_bytes() == p.read_bytes(), f"{p.name}: self-promote was not a no-op"


@pytest.mark.parametrize("sectors,msg", [
    ([700.0, 300.0], "strictly increasing"),      # descending
    ([300.0, 300.0], "strictly increasing"),      # equal: zero-length sector 2
    ([9999.0, 1.0], "within"),                    # outside (0, length), and descending
    ([0.0, 500.0], "within"),                     # 0 is the S/F line, not a split
    ([300.0, _LEN], "within"),                    # length itself is not a split either
    (["one", "two"], "must be numbers"),
    ("300,700", "must be a list"),
])
def test_refuses_sector_splits_the_editor_would_reject(tmp_path, sectors, msg):
    """Sector splits are committed shipped data (#41 baked the captured ones into all
    11 maps), and promote is the one path that can put an unvalidated set back -- a
    hand-edited export must not reach simcoach/tracks/ with [9999, 1] in it. Same rule
    as the editor's PUT, and it refuses rather than clamping."""
    tracks = _tracks(tmp_path)
    with pytest.raises(PromoteError, match=msg):
        promote(_src(tmp_path, _map(sectors=sectors)), tracks)
    assert list(tracks.iterdir()) == []          # nothing written


@pytest.mark.parametrize("field", ["rotation", "path"])
def test_a_non_finite_refuses_instead_of_raising(tmp_path, field):
    """json.loads ACCEPTS the bare `NaN` literal, and only `length`, the corner windows
    and (now) the sectors are range-checked -- so a NaN in `rotation` or the raw path
    reached _serialize's allow_nan=False and came out as a bare ValueError. main()
    catches only PromoteError, so the curator saw a traceback, not `refused: ...`.

    Both fields are chosen for having no OTHER check in front of them: a NaN in
    `sectors` is refused by validated_sectors and a short `path_dist` by the
    length-match check, so neither would exercise this path."""
    data = _map()
    if field == "rotation":
        data["rotation"] = math.nan
    else:
        data["path"][5] = [math.nan, math.nan]     # same length, so the shape check passes
    src = _src(tmp_path, data)           # json.dumps emits the bare `NaN` literal
    assert "NaN" in src.read_text(encoding="utf-8")
    tracks = _tracks(tmp_path)
    with pytest.raises(PromoteError):
        promote(src, tracks)
    assert list(tracks.iterdir()) == []                              # nothing written
    assert main(["user_built_circuit", "--from", str(src),
                 "--tracks-dir", str(tracks)]) == 1   # `refused:`, not a traceback


def test_an_interrupted_write_leaves_the_shipped_map_intact(tmp_path, monkeypatch):
    """write_bytes truncates before it writes, so an abort mid-write left a SHIPPED map
    empty -- the worst outcome here. The write goes to a sibling tmp and renames, so a
    failure lands on the tmp and the map is untouched (and no .tmp is left behind)."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map()), tracks)
    target = tracks / "user_built_circuit.json"
    before = target.read_bytes()

    # Inject at the rename, not at the write call: that pins "the payload is written
    # somewhere else and renamed into place" without naming the write API. A direct
    # target.write_bytes() never reaches os.replace, so this does not raise at all --
    # which is how the pre-fix code fails here.
    def no_replace(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(promote_map.os, "replace", no_replace)

    with pytest.raises(PromoteError, match="could not write.*the map is unchanged"):
        promote(_src(tmp_path, _map(name="Replacement")), tracks, force=True)
    monkeypatch.undo()
    assert target.read_bytes() == before                       # not truncated
    assert list(tracks.iterdir()) == [target]                  # no tmp left behind


def test_refuses_a_symlinked_target(tmp_path):
    """A DANGLING symlink is invisible to exists(), which gates both --force and the
    byte-fidelity check -- so a planted link to ../OUTSIDE.json was followed and written
    through, outside the tracks dir, with neither gate firing."""
    tracks = _tracks(tmp_path)
    outside = tmp_path / "OUTSIDE.json"
    link = tracks / "user_built_circuit.json"
    try:
        link.symlink_to(Path("..") / "OUTSIDE.json")
    except OSError as e:                  # Windows needs privileges/developer mode
        pytest.skip(f"cannot create symlinks here: {e}")
    assert link.is_symlink() and not link.exists()        # the hole the gate fell through
    with pytest.raises(PromoteError, match="symlink"):
        promote(_src(tmp_path, _map()), tracks)           # note: no force=True
    assert not outside.exists()
    assert link.is_symlink()                              # left alone, not written through


def test_a_planted_tmp_symlink_cannot_escape_the_tracks_dir(tmp_path):
    """The target's is_symlink gate doesn't cover the TMP path, so a planted
    `<slug>.json.tmp` -> ../OUTSIDE.json was written through. mkstemp opens
    O_CREAT|O_EXCL with a unique name, so the planted link is never opened at all."""
    tracks = _tracks(tmp_path)
    outside = tmp_path / "OUTSIDE.json"
    planted = tracks / "user_built_circuit.json.tmp"
    try:
        planted.symlink_to(Path("..") / "OUTSIDE.json")
    except OSError as e:
        pytest.skip(f"cannot create symlinks here: {e}")
    promote(_src(tmp_path, _map()), tracks)
    assert not outside.exists(), "wrote outside the tracks dir through the tmp symlink"
    assert planted.is_symlink()                   # untouched, not followed or replaced
    assert json.loads((tracks / "user_built_circuit.json").read_text("utf-8"))["track"] \
        == "user_built_circuit"


def _fail_target_readback(monkeypatch, target, outcome):
    """Break only the POST-replace read-back. `target` is read exactly twice: the
    byte-fidelity drift check, then the read-back -- so the 2nd read is the one. If that
    ever stops holding, the injection never fires and the test fails loudly."""
    reads = {"n": 0}
    real = Path.read_bytes
    def counting(self):
        if self == target:
            reads["n"] += 1
            if reads["n"] == 2:
                if outcome == "oserror":
                    raise OSError(5, "Input/output error")
                return b"not json at all"          # -> json.loads ValueError
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", counting)


@pytest.mark.parametrize("outcome", ["oserror", "unparseable"])
def test_a_failure_after_the_replace_says_the_map_was_replaced(tmp_path, monkeypatch,
                                                               capsys, outcome):
    """Pre- and post-replace failures shared one message, "the map is unchanged" -- a lie
    after the rename, and it contradicts the `git checkout --` advice it exists for. The
    unparseable case also pins that the read-back's json.loads raises ValueError, not
    OSError, so the block has to catch both or it tracebacks."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map()), tracks)
    target = tracks / "user_built_circuit.json"
    before = target.read_bytes()

    src = _src(tmp_path, _map(name="REPLACEMENT"))
    _fail_target_readback(monkeypatch, target, outcome)
    with pytest.raises(PromoteError, match="replaced, but could not verify"):
        promote(src, tracks, force=True)
    monkeypatch.undo()
    assert target.read_bytes() != before                      # it really was swapped
    assert json.loads(target.read_text("utf-8"))["name"] == "REPLACEMENT"
    assert list(tracks.iterdir()) == [target]                 # no tmp left behind

    _fail_target_readback(monkeypatch, target, outcome)
    assert main(["user_built_circuit", "--from", str(src),
                 "--tracks-dir", str(tracks), "--force"]) == 1
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert err.startswith("refused: ") and "git checkout --" in err


def _unreadable_target(tracks, kind):
    """A shipped-map path that read_bytes/json.loads/decode can't get through."""
    t = tracks / "user_built_circuit.json"
    if kind == "directory":
        t.mkdir()
    elif kind == "truncated":             # what an interrupted promote used to leave
        t.write_bytes(b'{\r\n  "track": "user_built_circ')
    elif kind == "not-utf8":
        t.write_bytes(b"\xff\xfe{}")
    return t


@pytest.mark.parametrize("kind", ["directory", "truncated", "not-utf8"])
def test_an_unreadable_target_refuses_instead_of_raising(tmp_path, kind, capsys):
    """`promote()` had no try around exists()/read_bytes()/json.loads(), so every OS- and
    decode-level failure escaped as a raw traceback instead of main()'s `refused:`. The
    truncated case is the recovery path after an interrupted write, so it matters most:
    the curator has to be told what's wrong, not handed a JSONDecodeError."""
    tracks = _tracks(tmp_path)
    target = _unreadable_target(tracks, kind)
    before = None if kind == "directory" else target.read_bytes()
    src = _src(tmp_path, _map())
    with pytest.raises(PromoteError, match="can't read the shipped map"):
        promote(src, tracks, force=True)
    assert main(["user_built_circuit", "--from", str(src),
                 "--tracks-dir", str(tracks), "--force"]) == 1
    assert capsys.readouterr().err.startswith("refused: ")
    assert target.is_dir() if kind == "directory" else target.read_bytes() == before


def test_a_read_only_target_refuses_instead_of_raising(tmp_path, capsys):
    """The write-side twin: os.replace over a read-only map raises PermissionError, which
    escaped promote() as a traceback. The map must also still be intact afterwards."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map()), tracks)
    target = tracks / "user_built_circuit.json"
    before = target.read_bytes()
    target.chmod(0o444)
    try:
        src = _src(tmp_path, _map(name="Replacement"))
        with pytest.raises(PromoteError, match="could not write"):
            promote(src, tracks, force=True)
        assert main(["user_built_circuit", "--from", str(src),
                     "--tracks-dir", str(tracks), "--force"]) == 1
        assert capsys.readouterr().err.startswith("refused: ")
        assert target.read_bytes() == before
        assert list(tracks.iterdir()) == [target]        # no .tmp left behind
    finally:
        target.chmod(0o644)


def test_refuses_a_source_whose_stem_is_not_a_slug(tmp_path):
    with pytest.raises(PromoteError, match="not a valid track slug"):
        promote(_src(tmp_path, _map(), name="Not A Slug.json"), _tracks(tmp_path))


def test_slug_override_names_the_shipped_file_not_the_export_filename(tmp_path):
    out = promote(_src(tmp_path, _map(), name="export-2026.json"), _tracks(tmp_path),
                  slug="user_built_circuit")
    assert out.name == "user_built_circuit.json"
