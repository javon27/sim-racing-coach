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
import simcoach.server.app as app_module
from simcoach.analysis import (
    CORNER_GEOMETRY_KEYS,
    SIGNED_GEOMETRY_KEYS,
    corner_geometry,
)
from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore

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


BAKED_MAPS = _baked_maps()


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
    24 of its width_m/banking_deg values outright."""
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


def test_imola_t13_keeps_its_baked_radius_across_a_save(tmp_path):
    """The headline case, pinned by VALUE so a regression names the corner: Imola T13's
    baked radius is 67.7 m. Re-deriving it from the corner's current window measures
    322.1 m -- a 4.8x change the curator never asked for, on a corner the coach
    describes to the driver in metres."""
    before, after = _self_save(tmp_path, IMOLA)
    t13_before = next(c for c in before["corners"] if str(c["label"]) == "13")
    t13_after = next(c for c in after["corners"] if str(c["label"]) == "13")
    assert t13_before["radius_m"] == 67.7          # what ships
    assert t13_after["radius_m"] == 67.7           # what a save must leave behind
    assert t13_after["radius_m"] != 322.1          # the re-derived value, for the record


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


def test_the_shipped_radius_is_the_preclamp_window_bleed_not_the_corner(tmp_path):
    """WHICH VALUE IS CORRECT, pinned so the question cannot be silently re-answered.

    The shipped radii were baked in 1559297 against the PRE-CLAMP corner windows, which
    were WIDER than today's and reached into the neighbouring corner. 35254d5 (#30)
    narrowed them and did not re-bake. `corner_geometry` reports the TIGHTEST point in
    the window, so what the stale value captured is the NEIGHBOUR'S radius.

    On 13 of the 18 drifting corners those wide windows are exactly `_persist_map`'s own
    `pad = max(20.0, length * 0.025)` default -- which is why this test DERIVES the pad
    from the map's own length below rather than hardcoding 122.4. Do NOT "tidy" it into
    `mapbuild._WIN_M`: that is 55.0 and always has been, it is not where these came
    from, and substituting it turns this test red over a bug that does not exist.

    Imola T13 is the proof: its stored 67.7 m is exactly what T12's own window measures,
    and T13's pre-clamp window reaches back over T12's apex. Measured on T13's own
    window it is 322.1 m. So the SHIPPED value is the stale one -- but a save is still
    the wrong place to correct it (backfill_corner_geometry.py is, under #73), which is
    why the fix preserves a value this test shows to be wrong.
    """
    d = _shipped(IMOLA)
    path, dist = d["path"], d["path_dist"]
    rib = d.get("ribbon") or {}
    by_label = {str(c["label"]): c for c in d["corners"]}
    t12, t13 = by_label["12"], by_label["13"]
    assert t13["radius_m"] == 67.7 == t12["radius_m"]  # the duplicate is the fingerprint

    now = corner_geometry(path, dist, d["corners"], width=rib.get("width"),
                          bank=rib.get("bank"))
    # T12's current window measures the 67.7 m that T13 has stored; T13's measures 322.1.
    assert now["12"]["radius_m"] == 67.7
    assert now["13"]["radius_m"] == 322.1

    # And T13's PRE-CLAMP window -- _persist_map's own pad either side of the apex,
    # which swallows T12's apex at 2846.3 -- reproduces the stored 67.7 exactly. That
    # is where it came from.
    pad = max(20.0, d["length"] * 0.025)
    assert round(pad, 1) == 122.4, "Imola's editor-default pad"
    pre = dict(t13, start_dist=t13["apex_dist"] - pad, end_dist=t13["apex_dist"] + pad)
    assert pre["start_dist"] < t12["apex_dist"] < pre["end_dist"]
    assert corner_geometry(path, dist, [pre])["13"]["radius_m"] == 67.7

    # The error is ONE-DIRECTIONAL, which is why #73 matters: a superset window's max
    # curvature is >= the subset's, so shipped <= recomputed on every drifting corner
    # -- the coach always calls a fast corner a slow one, never the reverse.
    assert all(c["radius_m"] <= now[str(c["label"])]["radius_m"] for c in d["corners"])


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


def _editor_save(tmp_path, slug, corners, strip_notes=True):
    """Save `corners` against a tmp copy of a shipped map, as the editor's PUT does.
    Returns the corners written to disk."""
    tracks = tmp_path / "tracks"
    tracks.mkdir(parents=True, exist_ok=True)
    d = _shipped(slug)
    if strip_notes:
        d = {k: v for k, v in d.items() if k != "notes"}  # notes carry is #37's rule
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


def test_the_signed_metrics_are_classified_beside_the_key_list():
    """`_measurable` exempts the SIGNED metrics from its positivity rule, and which
    those are must not become a second independent list of the metrics that exist --
    that shape (one writer's list missing a key) is what deleted Laguna's width/banking.
    A metric added to CORNER_GEOMETRY_KEYS and not classified silently inherits `> 0`.
    """
    assert SIGNED_GEOMETRY_KEYS <= set(CORNER_GEOMETRY_KEYS)
    assert app_module.SIGNED_GEOMETRY_KEYS is SIGNED_GEOMETRY_KEYS  # not a local copy


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
