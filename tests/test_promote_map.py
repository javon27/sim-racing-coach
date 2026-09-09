"""#10: promoting a user-built map into the committed shipped set.

Every test writes to tmp_path -- nothing here touches simcoach/tracks/.
"""
import json
import math

import pytest

from scripts.promote_map import PromoteError, promote

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


def test_force_keeps_the_shipped_maps_notes_that_still_bind(tmp_path):
    """--force must run the notes guard, not bypass it: a note whose label survives
    the replacement is kept, one whose label is gone is dropped rather than rebound."""
    tracks = _tracks(tmp_path)
    promote(_src(tmp_path, _map(notes={"overview": "curator wrote this",
                                       "corners": {"2": "late apex", "3": "flat"}})),
            tracks)
    replacement = _map(notes={"corners": {"1": "from the driver"}})
    replacement["corners"][2]["label"] = "3A"     # corner 3 relabelled: "3" no longer binds
    written = json.loads(promote(_src(tmp_path, replacement), tracks, force=True)
                         .read_text("utf-8"))
    assert written["notes"]["overview"] == "curator wrote this"   # not corner-keyed
    assert written["notes"]["corners"] == {"1": "from the driver", "2": "late apex"}


def test_refuses_a_source_whose_stem_is_not_a_slug(tmp_path):
    with pytest.raises(PromoteError, match="not a valid track slug"):
        promote(_src(tmp_path, _map(), name="Not A Slug.json"), _tracks(tmp_path))


def test_slug_override_names_the_shipped_file_not_the_export_filename(tmp_path):
    out = promote(_src(tmp_path, _map(), name="export-2026.json"), _tracks(tmp_path),
                  slug="user_built_circuit")
    assert out.name == "user_built_circuit.json"
