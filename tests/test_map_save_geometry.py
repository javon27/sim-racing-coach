# tests/test_map_save_geometry.py
#
# Per-corner geometry (radius_m/width_m/banking_deg/elev_delta_m) is OWNED, BAKED data:
# scripts/backfill_corner_geometry.py computes it from the point cloud and the curator
# commits the diff. The editor's save path used to RE-DERIVE it on every PUT, so opening
# a shipped map to fix a corner NAME silently rewrote geometry the curator never touched
# and cannot see in the UI -- and radius_m reaches the driver as owned fact through
# geometry_clause (#35), so the coach described a 2nd-gear corner as a sweeper (#62).
#
# The invariant these assert is the one scripts/promote_map.py already holds: a SELF-SAVE
# IS A NO-OP. Read a shipped map through the editor's GET and PUT it straight back; the
# map on disk must not change.
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import simcoach
import simcoach.server.core as core_mod
from simcoach import analysis
from simcoach.analysis import (
    CORNER_GEOMETRY_KEYS,
    SIGNED_GEOMETRY_KEYS,
    corner_geometry,
    geometry_clause,
)
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.track_lookup import load_map_data
from simcoach.trackmap import direction_from_path

SHIPPED = Path(simcoach.__file__).resolve().parent / "tracks"
IMOLA = "autodromo_enzo_e_dino_ferrari"
LAGUNA = "weathertech_raceway_laguna_seca"


def _shipped(slug: str) -> dict:
    return json.loads((SHIPPED / f"{slug}.json").read_text(encoding="utf-8"))


def _baked_maps() -> list[str]:
    """Shipped maps that carry baked geometry AND a frozen path the editor can save
    against. Spa has neither (#61) and test_track declares no geometry, so there is
    nothing for a save to preserve there."""
    out = []
    for p in sorted(SHIPPED.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        has_geo = any(k in c for c in (d.get("corners") or []) for k in CORNER_GEOMETRY_KEYS)
        if has_geo and d.get("path") and d.get("path_dist"):
            out.append(p.stem)
    return out


# PINNED, not derived. Every per-map test below parametrizes over this, and a list
# derived from the data itself means a map that loses its path or all of its geometry
# silently drops out of the parametrization -- its tests stop being collected instead of
# failing, which is the regression they exist to catch. Derived vs pinned is compared in
# test_the_pinned_baked_map_list_is_what_ships, which also forces a newly baked map to be
# added here rather than shipping unguarded.
BAKED_MAPS = [
    "algarve_international_circuit",
    "autodromo_enzo_e_dino_ferrari",
    "autodromo_jose_carlos_pace",
    "autodromo_nazionale_monza",
    "bahrain_endurance_circuit",
    "bahrain_international_circuit",
    "bahrain_outer_circuit",
    "bahrain_paddock_circuit",
    "circuit_de_la_sarthe",
    "monza_curva_grande_circuit",
    "weathertech_raceway_laguna_seca",
]


def test_the_pinned_baked_map_list_is_what_ships():
    """Both directions. A pinned map missing from the data lost its path or its whole bake;
    a baked map missing from the pin ships with none of the per-map guards run on it."""
    derived = _baked_maps()
    assert derived == BAKED_MAPS, (
        f"lost path/geometry: {sorted(set(BAKED_MAPS) - set(derived))}; "
        f"baked but not pinned: {sorted(set(derived) - set(BAKED_MAPS))}")


def _self_save(tmp_path: Path, slug: str) -> tuple[dict, dict]:
    """Copy one shipped map into a throwaway tracks dir, read it back through the
    editor's GET, PUT that body straight back, and return (before, after) parsed.

    Nothing touches simcoach/tracks/ -- create_app's tracks_dir is the tmp copy, which
    is also where _write_track_json writes when curating (running from source)."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    target = tracks / f"{slug}.json"
    target.write_bytes((SHIPPED / f"{slug}.json").read_bytes())
    before = json.loads(target.read_text(encoding="utf-8"))
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    got = client.get(f"/tracks/{slug}/map")
    assert got.status_code == 200, got.text
    saved = client.put(f"/tracks/{slug}/map", json=got.json())
    assert saved.status_code == 200, saved.text
    return before, json.loads(target.read_text(encoding="utf-8"))


def _geometry_delta(before: dict, after: dict) -> list[str]:
    """Every baked-geometry value a save changed, named by corner and metric."""
    was = {str(c.get("label") or c.get("number")): c for c in (before.get("corners") or [])}
    out = []
    for c in after.get("corners") or []:
        lab = str(c.get("label") or c.get("number"))
        for k in CORNER_GEOMETRY_KEYS:
            o, n = was.get(lab, {}).get(k), c.get(k)
            if o != n:
                out.append(f"T{lab} {k}: {o} -> {n}")
    return out


@pytest.mark.parametrize("slug", BAKED_MAPS)
def test_self_saving_a_shipped_map_leaves_its_geometry_alone(tmp_path, slug):
    """Per-map, the way promote_map.py reports its byte-fidelity check. Before the fix
    this failed on 7 of the 11 maps: radius_m moved on 18 corners, and Laguna lost all
    24 of its width_m/banking_deg values outright. Those 18 radii have since been
    re-baked (#73), so today only the width/banking and elev_delta_m halves of this
    would still catch a re-derivation -- see
    test_a_save_carries_a_stored_radius_forward_instead_of_re_deriving_it for the radius
    half, which now needs a divergent fixture to mean anything."""
    before, after = _self_save(tmp_path, slug)
    delta = _geometry_delta(before, after)
    assert delta == [], f"{slug}: save rewrote baked geometry: {delta}"


@pytest.mark.parametrize("slug", BAKED_MAPS)
def test_self_saving_a_shipped_map_changes_nothing_at_all(tmp_path, slug):
    """Wider than the geometry assertion above: the whole parsed map round-trips. Keeps
    the no-op honest, so a later save-path change cannot trade a geometry fix for a
    corner-window or notes regression."""
    before, after = _self_save(tmp_path, slug)
    assert after == before, f"{slug}: save was not a no-op"


def test_a_save_carries_a_stored_radius_forward_instead_of_re_deriving_it(tmp_path):
    """#62's headline case, kept DISCRIMINATING after #73 re-baked the shipped radii.

    It used to read straight off the shipped file: Imola T13 stored 67.7 m and its own
    window measured 322.1 m, so a save that re-derived was visible as a 4.8x change on a
    corner the coach describes to the driver in metres. #73 re-baked T13 to the 322.1 its
    window measures, so stored and re-derived now AGREE on every shipped corner and no
    shipped corner can tell carry-forward from re-derivation apart any more.

    So the fixture puts the historical stale value back on disk. 67.7 is measurable
    (finite, positive), so the carry-forward must hand it back untouched; re-deriving
    returns 322.1. Without a divergent stored value this assertion is worth nothing --
    do not "simplify" it back to reading the shipped file."""
    stale = [dict(c, radius_m=67.7) if str(c["label"]) == "13" else dict(c)
             for c in _shipped(IMOLA)["corners"]]
    after = {str(c["label"]): c for c in _editor_save(tmp_path, IMOLA, stale, on_disk=stale)}
    assert after["13"]["radius_m"] == 67.7    # carried, not re-measured
    assert after["13"]["radius_m"] != 322.1   # what re-deriving would have written


def test_laguna_keeps_its_point_cloud_width_and_banking_across_a_save(tmp_path):
    """A SECOND way the same re-derivation destroyed owned data, and the reason the fix
    covers every metric rather than radius alone. Laguna's width/banking were baked from
    the raw boundary-weave cloud (d69d5ec) and the map carries no `ribbon`, so the save
    path had no source for them and wrote all 12 corners back without them."""
    assert "ribbon" not in _shipped(LAGUNA), "premise: Laguna has no stored ribbon"
    before, after = _self_save(tmp_path, LAGUNA)
    pairs = [(c["width_m"], c["banking_deg"]) for c in before["corners"]]
    assert len(pairs) == 12 and all(w is not None and b is not None for w, b in pairs)
    assert [(c.get("width_m"), c.get("banking_deg")) for c in after["corners"]] == pairs
    corkscrew = next(c for c in after["corners"] if str(c["label"]) == "9")
    assert (corkscrew["width_m"], corkscrew["banking_deg"]) == (9.4, 4.1)


def _flip(direction: str) -> str:
    return "left" if direction == "right" else "right"


def test_a_save_carries_the_stored_direction_forward_instead_of_re_deriving_it(tmp_path):
    """#87's headline case (Spa T19 Bus Stop): direction used to be re-derived from the
    averaged/frozen path on every save, which is ambiguous at a chicane and flipped a
    curated direction. Carry the existing direction forward for a corner matched by
    apex, the same identity rule #62 established for baked geometry -- only re-derive
    for a corner that is new or whose apex moved (see the next test).

    Flips one shipped corner's stored direction to the WRONG value on disk: carry-
    forward must hand it back untouched. Re-deriving would "correct" it back to what
    the path actually shows, so an unflipped fixture couldn't tell the two apart --
    same technique as test_a_save_carries_a_stored_radius_forward_instead_of_re_deriving_it."""
    shipped = _shipped(IMOLA)["corners"]
    flipped = _flip(shipped[0]["direction"])
    on_disk = [dict(c, direction=flipped) if i == 0 else dict(c)
               for i, c in enumerate(shipped)]
    sent = [{k: c[k] for k in ("number", "label", "name", "apex_dist", "start_dist", "end_dist")}
            for c in on_disk]
    after = _editor_save(tmp_path, IMOLA, sent, on_disk=on_disk)
    assert after[0]["direction"] == flipped                     # carried, not re-measured
    assert after[0]["direction"] != shipped[0]["direction"]     # what re-deriving would give


def test_moving_a_corners_apex_re_derives_only_its_own_direction(tmp_path):
    """The carry-forward must not become "never re-derive": moving an apex is the
    curator changing the geometry, in a diff they can see, and that corner's direction
    must come back from the path like every other owned metric does (#62's apex-move
    corollary). Every OTHER corner, apex unchanged, keeps its stored value."""
    d = _shipped(IMOLA)
    shipped = d["corners"]
    flipped = _flip(shipped[0]["direction"])
    on_disk = [dict(c, direction=flipped) if i == 0 else dict(c)
               for i, c in enumerate(shipped)]
    sent = [{k: c[k] for k in ("number", "label", "name", "apex_dist")}
            for c in on_disk]
    moved_apex = sent[0]["apex_dist"] + 5.0
    sent[0] = dict(sent[0], apex_dist=moved_apex)
    after = _editor_save(tmp_path, IMOLA, sent, on_disk=on_disk)
    expected = direction_from_path(d["path"], d["path_dist"], moved_apex)
    assert after[0]["direction"] == expected     # re-derived at the NEW apex
    assert after[0]["direction"] != flipped       # not the stale on-disk value
    assert after[1]["direction"] == on_disk[1]["direction"]   # untouched corner: carried


def test_a_save_preserves_unknown_top_level_provenance_keys(tmp_path):
    """#87: `_persist_map` used to build its output from a fixed key set, so any key it
    didn't know about vanished on the next save -- on shipped Spa, `_config_note`,
    `_distances_note` and `length_m`. Add one to a shipped map's tmp copy and self-save
    it: it must survive, byte-identical."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = {**_shipped(IMOLA), "_config_note": "curated; T2 driver-confirmed",
         "length_m": 4909}
    (tracks / f"{IMOLA}.json").write_text(json.dumps(d), encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    got = client.get(f"/tracks/{IMOLA}/map")
    assert got.status_code == 200, got.text
    saved = client.put(f"/tracks/{IMOLA}/map", json=got.json())
    assert saved.status_code == 200, saved.text
    after = json.loads((tracks / f"{IMOLA}.json").read_text(encoding="utf-8"))
    assert after["_config_note"] == "curated; T2 driver-confirmed"
    assert after["length_m"] == 4909


@pytest.mark.parametrize("slug", BAKED_MAPS)
def test_every_shipped_radius_is_measured_on_its_own_corner_window(slug):
    """#73's guard: a stored `radius_m` must be reproducible from the SAME file's frozen
    path and that corner's CURRENT window.

    Those are two pieces of committed data that can silently drift apart, and did. The
    radii were baked in 1559297 against windows wider than the corner; 35254d5 (#30)
    then narrowed 69 corner windows across 11 maps and did not re-bake. Because
    `corner_geometry` returns the TIGHTEST point in the window, an over-wide window can
    only ever report a radius at or below the true one -- so for months the coach told
    the driver a fast corner was a slow one, through geometry_clause (#35), as owned
    fact: 18 corners across 7 maps, worst Bahrain Endurance T13 at 40.8 m stored against
    340.7 m measured (8.3x).

    Any future window edit not followed by
    `python scripts/backfill_corner_geometry.py --only=radius_m` turns this red.

    Radius depends only on the path, so this needs no capture, no ribbon and no lap
    store -- it is checkable from the repo alone, which is why it is a test rather than
    something only the curator's machine can run.

    RADIUS ONLY, deliberately, and the other three are not oversights. width_m and
    banking_deg are read at the APEX index, and #30 moved no apex (all 169 corners still
    match on apex_dist across 35254d5), so they cannot have gone stale this way.
    elev_delta_m CAN and DID -- it reads the window ends, and all 169 stored values were
    reproduced exactly by the pre-#30 windows, so 59 of them described a window that no
    longer existed; #74 re-baked those from the current windows (`--only=elev_delta_m`).
    It is not checked here because the elevation profile is derived from app-data
    captures that are not in the repo, so there is nothing to compare against -- a
    future window edit needs that elevation re-bake too, and nothing here will notice."""
    d = _shipped(slug)
    # Positional probe, as _persist_map does: corner_geometry keys its result on label,
    # so two corners sharing one would collapse into a single entry.
    geo = corner_geometry(d["path"], d["path_dist"],
                          [dict(c, label=str(i)) for i, c in enumerate(d["corners"])])
    drift = [f"T{c.get('label')}: stored {c.get('radius_m')} vs measured "
             f"{geo[str(i)].get('radius_m')}"
             for i, c in enumerate(d["corners"])
             if c.get("radius_m") != geo[str(i)].get("radius_m")]
    assert drift == [], f"{slug}: {len(drift)} stale baked radius/radii: {drift}"


def test_imola_t13s_old_radius_was_its_neighbours_measured_through_a_pre_clamp_window():
    """WHICH VALUE IS CORRECT, pinned so the question cannot be silently re-answered --
    it was already answered backwards once. #62 was filed on the premise that the editor
    had corrupted a good bake; the opposite was true, and #73 re-baked accordingly.

    Imola T13 shipped 67.7 m. That number is not T13: it is exactly what T12's own
    window measures. T13's PRE-CLAMP window -- `_persist_map`'s own
    `pad = max(20.0, length * 0.025)` either side of the apex -- reaches back over T12's
    apex at 2846.3 and reproduces the old 67.7 exactly, which is where it came from.
    Measured on T13's own window it is 322.1 m, and that is what ships now.

    This test DERIVES the pad from the map's own length rather than hardcoding 122.4. Do
    NOT "tidy" it into `mapbuild._WIN_M`: that is 55.0 and always has been, it is not
    where these came from, and substituting it turns this test red over a bug that does
    not exist. (The pad accounts for 13 of the 18; the other 5 are asymmetric spans from
    some earlier writer, so the provable claim is only "wider windows that reached into
    the neighbour".)"""
    d = _shipped(IMOLA)
    path, dist = d["path"], d["path_dist"]
    by_label = {str(c["label"]): c for c in d["corners"]}
    t12, t13 = by_label["12"], by_label["13"]

    assert t13["radius_m"] == 322.1, "T13's own window, re-baked under #73"
    assert t12["radius_m"] == 67.7, "never drifted, and the source of T13's old value"

    pad = max(20.0, d["length"] * 0.025)
    assert round(pad, 1) == 122.4, "Imola's editor-default pad"
    pre = dict(t13, start_dist=t13["apex_dist"] - pad, end_dist=t13["apex_dist"] + pad)
    assert pre["start_dist"] < t12["apex_dist"] < pre["end_dist"], "swallows T12's apex"
    # And so it measures T12's radius, not T13's -- the old stored value, to 1 dp.
    assert corner_geometry(path, dist, [pre])["13"]["radius_m"] == t12["radius_m"] == 67.7


def test_a_corner_with_no_baked_geometry_still_gets_it_computed(tmp_path):
    """The carry-forward must not become "never compute". A corner the curator ADDS has
    no baked value, so the save is its only source -- guards against over-correcting the
    fix into a path that can never bake a new corner."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = {k: v for k, v in _shipped(IMOLA).items() if k != "notes"}  # notes carry is #37's
    fresh = {"number": 20, "label": "20", "name": "New Kink", "apex_dist": 2944.4,
             "start_dist": 2895.4, "end_dist": 3066.8, "apexes": [2944.4]}
    (tracks / f"{IMOLA}.json").write_text(json.dumps({**d, "corners": [fresh]}),
                                          encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{IMOLA}/map", json={"corners": [fresh]})
    assert r.status_code == 200, r.text
    saved = json.loads((tracks / f"{IMOLA}.json").read_text(encoding="utf-8"))["corners"][0]
    # T13's window, so the computed value is T13's own 322.1 -- not absent, not inherited.
    assert saved["radius_m"] == 322.1
    assert saved["width_m"] == 8.0  # from the map's stored ribbon at the apex


# --- A save is only a no-op if it survives the edits the editor actually offers. -----
#
# The carry-forward is matched on apex distance because the other two candidate keys are
# both rewritten by the UI: `label` is a free-text input the curator types in, and
# `number` is reassigned positionally by the editor's renumber() on every insert. Each
# of these fails on a label key, and the insert one fails WORSE on a number key.


def _editor_save(tmp_path, slug, corners, strip_notes=True, on_disk=None):
    """Save `corners` against a tmp copy of a shipped map, as the editor's PUT does.
    Returns the corners written to disk.

    `on_disk` replaces the STORED corners before the save, for the cases where what the
    carry-forward reads has to differ from what ships (a hand-edited map, or the pre-#73
    stale bake)."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = _shipped(slug)
    if strip_notes:
        d = {k: v for k, v in d.items() if k != "notes"}  # notes carry is #37's rule
    if on_disk is not None:
        d = {**d, "corners": on_disk}
    (tracks / f"{slug}.json").write_text(json.dumps(d), encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{slug}/map", json={"corners": corners})
    assert r.status_code == 200, r.text
    return json.loads((tracks / f"{slug}.json").read_text(encoding="utf-8"))["corners"]


def _geo(corner):
    return {k: corner.get(k) for k in CORNER_GEOMETRY_KEYS}


@pytest.mark.parametrize(
    "slug,label,new_label",
    [(IMOLA, "13", "13A"),          # has a ribbon: radius was rewritten, elev dropped
     (LAGUNA, "1", "1x")],          # no ribbon: width/banking/elev all dropped
)
def test_renaming_a_corner_label_keeps_every_baked_metric(tmp_path, slug, label, new_label):
    """#62's own words are "a save whose only intended change was a corner NAME", and
    the label box is exactly what the curator types in. Keyed on label, the renamed
    corner matched nothing in the prior map and lost its whole bake -- Imola T13 went
    back to radius 322.1 with elev_delta_m absent, Laguna T1 lost width, banking and
    elevation. Keyed on apex distance, a rename is invisible to the geometry."""
    before = {str(c["label"]): c for c in _shipped(slug)["corners"]}
    sent = [dict(c, label=(new_label if str(c["label"]) == label else c["label"]))
            for c in _shipped(slug)["corners"]]
    after = {str(c["label"]): c for c in _editor_save(tmp_path, slug, sent)}
    assert set(after) == (set(before) - {label}) | {new_label}, "the rename itself"
    assert _geo(after[new_label]) == _geo(before[label])      # the renamed corner
    for lab in set(before) - {label}:                          # and every other one
        assert _geo(after[lab]) == _geo(before[lab]), f"T{lab} changed"


def test_an_inserted_corner_gets_its_own_geometry_not_a_neighbours(tmp_path):
    """The editor's "add corner" appends {number: 0, label: ""} and renumber() assigns
    positional numbers, so a new corner's server-filled label is a positional number an
    existing corner already holds. Keyed on label the new corner inherited the real T2's
    bake, measured 294 m away; keyed on NUMBER it is worse -- renumber() shifts every
    downstream number, so 18 of Imola's 19 corners would inherit a neighbour's. Keyed on
    apex distance: 0 do."""
    shipped = {str(c["label"]): c for c in _shipped(IMOLA)["corners"]}
    # What the editor sends: a corner at 353.3 m inserted between T1 (58.9) and T2
    # (647.8), everything renumbered by apex order, the new one's label left blank.
    sent = sorted([*(dict(c) for c in _shipped(IMOLA)["corners"]),
                   {"label": "", "name": "", "apex_dist": 353.3, "apexes": [353.3]}],
                  key=lambda c: c["apex_dist"])
    for i, c in enumerate(sent):
        c["number"] = i + 1
    after = _editor_save(tmp_path, IMOLA, sent)

    new = next(c for c in after if c["apex_dist"] == 353.3)
    real_t2 = next(c for c in after if c["apex_dist"] == 647.8)
    # The real T2 keeps its bake; the newcomer does NOT get handed a copy of it.
    assert _geo(real_t2) == _geo(shipped["2"])
    assert new["radius_m"] != shipped["2"]["radius_m"], "inherited T2's bake"
    assert new["radius_m"] != shipped["1"]["radius_m"], "inherited T1's bake"
    # Its own window measured instead -- a real value, and neither neighbour's
    # (T1 ships 535.0, T2 ships 128.8).
    assert new["radius_m"] == 567.8
    # And every pre-existing corner still has exactly the bake it shipped with, which is
    # what a number key breaks: 18 of these would hold their neighbour's numbers.
    for c in after:
        if c["apex_dist"] == 353.3:
            continue
        lab = next(ll for ll, s in shipped.items() if s["apex_dist"] == c["apex_dist"])
        assert _geo(c) == _geo(shipped[lab]), f"apex {c['apex_dist']} took another bake"


def test_a_stored_null_metric_is_computed_not_dropped(tmp_path):
    """`k in baked` is true for a stored null, so the carry-forward picked None and the
    `is not None` guard then skipped the write -- leaving the key ABSENT where the old
    code would have computed it. Half the property is "never leaves one absent that
    could have been computed", so a null has to fall through to the computed value."""
    corners = [dict(c, radius_m=None) if str(c["label"]) == "13" else dict(c)
               for c in _shipped(IMOLA)["corners"]]
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = {k: v for k, v in _shipped(IMOLA).items() if k != "notes"}
    (tracks / f"{IMOLA}.json").write_text(json.dumps({**d, "corners": corners}),
                                          encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{IMOLA}/map", json={"corners": corners})
    assert r.status_code == 200, r.text
    after = {str(c["label"]): c
             for c in json.loads((tracks / f"{IMOLA}.json").read_text("utf-8"))["corners"]}
    assert "radius_m" in after["13"], "a null was deleted instead of computed"
    assert after["13"]["radius_m"] == 322.1          # T13's own window, computed
    assert after["12"]["radius_m"] == 67.7           # untouched neighbours still carried


@pytest.mark.parametrize(
    "bad,expect",
    [(float("nan"), 322.1),    # allow_nan=False: carrying this forward 500s the save
     (float("inf"), 322.1),
     (0, 322.1),               # a zero-length radius cannot have been measured
     (-5.0, 322.1),
     (True, 322.1),            # bool IS an int: `True > 0` would carry it through
     ("abc", 322.1),
     ({"x": 1}, 322.1),
     ([], 322.1)],
)
def test_an_unmeasurable_stored_metric_is_replaced_not_preserved(tmp_path, bad, expect):
    """DECISION on corrupt stored values: carry forward only what could have been
    MEASURED -- a finite number, positive for the two that are lengths. "Don't rewrite
    good data" is not "preserve data that cannot be right", and a non-finite one is not
    merely ugly: _write_track_json serialises with allow_nan=False, so carrying a NaN
    forward turned the save into a 500. Reachable from hand-edited and third-party maps;
    backfill_corner_geometry.py pops a metric rather than writing a bad one."""
    corners = [dict(c, radius_m=bad) if str(c["label"]) == "13" else dict(c)
               for c in _shipped(IMOLA)["corners"]]
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = {k: v for k, v in _shipped(IMOLA).items() if k != "notes"}
    # json.dumps writes bare NaN/Infinity; the server reads it back with json.load,
    # which accepts them -- that is how one reaches the carry-forward at all.
    (tracks / f"{IMOLA}.json").write_text(json.dumps({**d, "corners": corners}),
                                          encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    sendable = [dict(c, radius_m=None) if str(c["label"]) == "13" else c for c in corners]
    r = client.put(f"/tracks/{IMOLA}/map", json={"corners": sendable})
    assert r.status_code == 200, r.text          # a NaN on disk used to 500 here
    after = {str(c["label"]): c
             for c in json.loads((tracks / f"{IMOLA}.json").read_text("utf-8"))["corners"]}
    assert after["13"]["radius_m"] == expect


def test_splitting_a_corner_keeps_its_note_on_the_dragged_corner(tmp_path):
    """8 -> 8A/8B on a real layout. The curator drags Algarve T5's apex 8 m and adds a
    corner 30 m the other side of where T5 was -- both turn left, as the server derives
    direction from the shipped path. T5's note belongs to the corner that was dragged: 8 m
    away against 30 m. 7ea972a treated ANY second same-way corner within 50 m as ambiguous
    and dropped the note on every save of this shape; only a near-tie is ambiguous now.
    Asserted through PUT /map, so the directions are the server's, not the fixture's."""
    slug = "algarve_international_circuit"
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    (tracks / f"{slug}.json").write_bytes((SHIPPED / f"{slug}.json").read_bytes())
    shipped = _shipped(slug)
    note = shipped["notes"]["corners"]["5"]
    t5 = next(c for c in shipped["corners"] if str(c["label"]) == "5")
    dragged = round(t5["apex_dist"] + 8.0, 1)
    body = [dict(c, apex_dist=dragged, apexes=[dragged]) if c is t5 else dict(c)
            for c in shipped["corners"]]
    body.append({"number": 0, "label": "5A", "name": "Turn 5A",
                 "apex_dist": round(t5["apex_dist"] - 30.0, 1)})
    body.sort(key=lambda c: c["apex_dist"])
    for n, c in enumerate(body, 1):
        c["number"] = n                        # the editor renumbers; labels are kept
    client = TestClient(create_app(LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data")),
                                   Coach(FakeModel("{}")), tracks_dir=str(tracks)),
                        base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{slug}/map", json={"corners": body})
    assert r.status_code == 200, r.text
    assert r.json()["dropped_notes"] == {}
    saved = json.loads((tracks / f"{slug}.json").read_text(encoding="utf-8"))
    assert saved["notes"]["corners"]["5"] == note
    by_label = {str(c["label"]): c for c in saved["corners"]}
    assert by_label["5"]["apex_dist"] == dragged           # the note names the dragged corner
    assert by_label["5"]["direction"] == by_label["5A"]["direction"] == t5["direction"]


def test_the_signed_metrics_are_classified_beside_the_key_list():
    """`measurable_geometry` exempts the SIGNED metrics from its positivity rule, and
    which those are must not become a second independent list of the metrics that exist
    -- that shape (one writer's list missing a key) is what deleted Laguna's width/banking.
    A metric added to CORNER_GEOMETRY_KEYS and not classified silently inherits `> 0`.

    The rule itself moved beside the lists (#65) because it now gates what ANY writer may
    put in a track file, not only what the editor carries forward. So what must not be a
    local copy is the rule, and the server must use the same function object.
    """
    assert SIGNED_GEOMETRY_KEYS <= set(CORNER_GEOMETRY_KEYS)
    from simcoach.server import trackfile, tracks
    for mod in (trackfile, tracks):   # every server module that applies the rule (#65)
        assert mod.measurable_geometry is analysis.measurable_geometry   # not a local copy
        assert not hasattr(mod, "_measurable")
        assert not hasattr(mod, "SIGNED_GEOMETRY_KEYS")


def test_the_positivity_rule_matches_what_the_shipped_maps_actually_hold():
    """The `> 0` rule is correct against real DATA, not just reasoning. Across every
    shipped map: the two lengths never hold a zero or negative, so the rule cannot drop
    a real measurement; and both signed metrics DO hold values <= 0, so the exemption is
    load-bearing rather than theoretical -- drop it and real banking/elevation dies."""
    seen = {k: [] for k in CORNER_GEOMETRY_KEYS}
    for p in sorted(SHIPPED.glob("*.json")):
        for c in json.loads(p.read_text(encoding="utf-8")).get("corners") or []:
            for k in CORNER_GEOMETRY_KEYS:
                if isinstance(c.get(k), (int, float)):
                    seen[k].append(c[k])
    for k in CORNER_GEOMETRY_KEYS:
        assert seen[k], f"no shipped {k} to reason about"
        if k in SIGNED_GEOMETRY_KEYS:
            assert min(seen[k]) <= 0, f"{k}: exemption unused, so it is untested"
        else:
            assert min(seen[k]) > 0, f"{k}: a shipped length is <= 0; the rule drops it"
    # And every one of them survives a save unchanged -- the ranges above are the data
    # the per-map no-op test above is protecting.
    assert min(seen["radius_m"]) > 0 and min(seen["width_m"]) > 0


def test_a_legitimately_zero_banking_is_still_carried(tmp_path):
    """The flip side of the decision above: banking_deg and elev_delta_m are SIGNED and
    legitimately zero (a flat corner, a level one), so the positivity rule must apply
    only to the two metrics that are lengths. Imola ships a -0.0 banking, and a save
    must not treat it as corrupt and overwrite it."""
    corners = [dict(c, banking_deg=0.0, elev_delta_m=0.0) if str(c["label"]) == "13"
               else dict(c) for c in _shipped(IMOLA)["corners"]]
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = {k: v for k, v in _shipped(IMOLA).items() if k != "notes"}
    (tracks / f"{IMOLA}.json").write_text(json.dumps({**d, "corners": corners}),
                                          encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel("{}")), tracks_dir=str(tracks))
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.put(f"/tracks/{IMOLA}/map", json={"corners": corners})
    assert r.status_code == 200, r.text
    after = {str(c["label"]): c
             for c in json.loads((tracks / f"{IMOLA}.json").read_text("utf-8"))["corners"]}
    # Carried as stored, NOT recomputed from the ribbon (which would give -0.0 / absent).
    assert after["13"]["banking_deg"] == 0.0
    assert after["13"]["elev_delta_m"] == 0.0


# --- Installed build: the app-data copy annotates the shipped map, it is not geometry (#75).
#
# A frozen build writes every save to app-data, and the read used to take that copy's
# `corners` wholesale -- geometry included. So a driver who once renamed a corner kept the
# geometry that shipped that day, forever, and each later save wrote it back. The fixtures
# pin values where the two readings DIFFER: the stale bake an older copy holds (Imola T13
# radius 67.7 pre-#73, elevation 14.1 pre-#74) against the re-baked build's (322.1, 9.4).
# The re-baked elevation is set explicitly, so this does not depend on #74 having landed.

STALE_T13 = {"radius_m": 67.7, "elev_delta_m": 14.1, "banking_deg": -1.8, "width_m": 6.1}
# All four metrics differ from the copy's. banking_deg is exactly 0.0: shipped maps hold
# level corners at 0.0, a real measurement that a falsy check would hand to the copy.
REBAKED_T13 = {"radius_m": 322.1, "elev_delta_m": 9.4, "banking_deg": 0.0, "width_m": 8.0}
# Rule (c) with the key PRESENT: shipped Tosa (T7) holds an unusable radius and the copy a
# real one, so the copy's is used -- "shipped has the key" is not "shipped measured it".
# NOT 26.5: that is what the editor save re-measures for T7, so a save that dropped the
# copy's value and re-derived it would pass by coincidence.
T7_SHIPPED_RADIUS, T7_COPY_RADIUS = 0, 31.0
T13_NOTE = "lift, don't brake"


def _installed(tmp_path, monkeypatch):
    """A frozen build with the re-baked Imola in its bundle, and the driver's app-data copy
    from an older build: T5 renamed, a note on T13, T13 still holding the stale bake, and
    T7 holding a real radius where the bundle's is unusable. Returns
    (client, bundled_dir, user_dir)."""
    monkeypatch.setattr(core_mod.sys, "frozen", True, raising=False)
    shipped = _shipped(IMOLA)
    shipped["corners"] = [dict(c, **REBAKED_T13) if str(c["label"]) == "13"
                          else dict(c, radius_m=T7_SHIPPED_RADIUS) if str(c["label"]) == "7"
                          else c for c in shipped["corners"]]
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / f"{IMOLA}.json").write_text(json.dumps(shipped), encoding="utf-8")
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    user = Path(store.data_dir) / "tracks"
    user.mkdir(parents=True)
    corners = [dict(c, **STALE_T13) if str(c["label"]) == "13"
               else dict(c, radius_m=T7_COPY_RADIUS) if str(c["label"]) == "7"
               else dict(c, name="Driver's Five") if str(c["label"]) == "5"
               else c for c in shipped["corners"]]
    notes = {**shipped["notes"], "corners": {**shipped["notes"]["corners"], "13": T13_NOTE}}
    # What an installed save writes: the whole merged map, corners and all.
    (user / f"{IMOLA}.json").write_text(
        json.dumps({**shipped, "corners": corners, "notes": notes}), encoding="utf-8")
    client = TestClient(create_app(store, Coach(FakeModel("{}")), tracks_dir=str(bundled)),
                        base_url="http://127.0.0.1")
    return client, bundled, user


def _t13(corners):
    return next(c for c in corners if str(c["label"]) == "13")


def test_installed_read_takes_geometry_from_shipped_and_annotations_from_the_copy(
        tmp_path, monkeypatch):
    """#75's probe: the merged read gave T13 radius 67.7 and the coach said "radius ~68m"."""
    _, bundled, user = _installed(tmp_path, monkeypatch)
    merged = load_map_data(IMOLA, str(bundled), str(user))
    by = {str(c["label"]): c for c in merged["corners"]}
    assert {k: by["13"][k] for k in REBAKED_T13} == REBAKED_T13
    assert by["7"]["radius_m"] == T7_COPY_RADIUS          # shipped 0 is no measurement
    assert by["5"]["name"] == "Driver's Five"
    assert merged["notes"]["corners"]["13"] == T13_NOTE
    # What the coach is told, through the CornerMap the sidecar's resolver builds.
    clause = geometry_clause(CornerMap.from_data(merged).geometry["13"], by["13"]["direction"])
    assert clause.startswith("uphill 9m"), clause     # radius no longer rendered (#99)


@pytest.mark.parametrize("save", ["editor", "rotation"])
def test_an_installed_save_does_not_write_the_stale_bake_back(tmp_path, monkeypatch, save):
    """The second half of #75: the next save re-wrote the stale values through the
    carry-forward. After a save, the copy must hold the shipped bake -- and a LATER re-bake
    must still reach the read, so the copy's values are never what the driver is told."""
    client, bundled, user = _installed(tmp_path, monkeypatch)
    shipped_bytes = (bundled / f"{IMOLA}.json").read_bytes()
    if save == "editor":
        got = client.get(f"/tracks/{IMOLA}/map")
        assert got.status_code == 200, got.text
        r = client.put(f"/tracks/{IMOLA}/map", json=got.json())
    else:
        r = client.post(f"/tracks/{IMOLA}/rotation", json={"degrees": 30})
    assert r.status_code == 200, r.text
    assert (bundled / f"{IMOLA}.json").read_bytes() == shipped_bytes, "premise: installed"

    copy = json.loads((user / f"{IMOLA}.json").read_text(encoding="utf-8"))
    by = {str(c["label"]): c for c in copy["corners"]}
    assert {k: by["13"][k] for k in REBAKED_T13} == REBAKED_T13, "stale bake written back"
    assert by["7"]["radius_m"] == T7_COPY_RADIUS, "shipped's unusable 0 written over it"
    assert by["5"]["name"] == "Driver's Five"
    assert copy["notes"]["corners"]["13"] == T13_NOTE

    # The build after that re-bakes T13 again; the copy on disk is not consulted for it.
    shipped = json.loads(shipped_bytes)
    shipped["corners"] = [dict(c, radius_m=301.0, elev_delta_m=8.8) if str(c["label"]) == "13"
                          else c for c in shipped["corners"]]
    (bundled / f"{IMOLA}.json").write_text(json.dumps(shipped), encoding="utf-8")
    t13 = _t13(load_map_data(IMOLA, str(bundled), str(user))["corners"])
    assert (t13["radius_m"], t13["elev_delta_m"]) == (301.0, 8.8)


def test_the_copy_owns_its_corner_set_and_only_fills_metrics_shipped_lacks(tmp_path):
    """The three cases the merge has to decide, matched on apex (the carry-forward's key):
      (a) no shipped corner at the copy's apex -- added, or its apex moved -- keeps the
          copy's own geometry, which the editor measured at THAT apex when it saved;
      (b) a shipped corner the copy deleted stays deleted: the copy owns the corner set;
      (c) a metric the shipped corner has no measurement for comes from the copy, while
          the metrics it does have still win."""
    shipped = _shipped(IMOLA)
    s = {str(c["label"]): c for c in shipped["corners"]}
    shipped["corners"] = [{k: v for k, v in c.items() if k != "banking_deg"}
                          if str(c["label"]) == "1" else c for c in shipped["corners"]]
    added = {"number": 2, "label": "1A", "name": "Added", "start_dist": 300.0,
             "apex_dist": 353.3, "end_dist": 400.0, "apexes": [353.3],
             "radius_m": 55.5, "width_m": 7.7}
    moved = dict(s["12"], apex_dist=s["12"]["apex_dist"] + 5.0, radius_m=99.9)
    overlay = [dict(s["1"], radius_m=999.0, banking_deg=2.5), added,
               *(moved if lab == "12" else c for lab, c in s.items() if lab not in ("1", "2"))]
    bundled, user = tmp_path / "bundled", tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    (bundled / f"{IMOLA}.json").write_text(json.dumps(shipped), encoding="utf-8")
    (user / f"{IMOLA}.json").write_text(json.dumps({**shipped, "corners": overlay}),
                                        encoding="utf-8")
    by = {str(c["label"]): c for c in load_map_data(IMOLA, str(bundled), str(user))["corners"]}

    assert "2" not in by and len(by) == len(overlay)                       # (b)
    assert (by["1A"]["radius_m"], by["1A"]["width_m"]) == (55.5, 7.7)      # (a) added
    assert by["12"]["radius_m"] == 99.9 != s["12"]["radius_m"]             # (a) apex moved
    assert by["1"]["radius_m"] == s["1"]["radius_m"] == 535.0              # shipped wins...
    assert by["1"]["banking_deg"] == 2.5                                   # (c) ...not absent
