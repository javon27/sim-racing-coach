"""#158: a track JSON rewritten on disk OUTSIDE the sidecar (curation script, promote_map,
a hand edit) must reach every per-track cache, and must not be overwritten by a save
against the copy loaded before it."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

import simcoach
from simcoach.coach import Coach
from simcoach.corners import CornerMap
from simcoach.lmu import smm
from simcoach.model.fake import FakeModel
from simcoach.server import live_ws as live_ws_mod
from simcoach.server.app import create_app
from simcoach.store import LapStore
from simcoach.track_lookup import dump_track_json, load_map_data

SLUG = "autodromo_enzo_e_dino_ferrari"


def _setup(tmp_path, resolver=False):
    shipped = Path(simcoach.__file__).resolve().parent / "tracks" / f"{SLUG}.json"
    tracks = tmp_path / "tracks"
    tracks.mkdir()
    f = tracks / f"{SLUG}.json"
    f.write_bytes(shipped.read_bytes())
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    map_resolver = ((lambda t: CornerMap.from_data(load_map_data(t, str(tracks))))
                    if resolver else None)
    app = create_app(store, Coach(FakeModel(reply="ok")), tracks_dir=str(tracks),
                     map_resolver=map_resolver)
    return TestClient(app, base_url="http://127.0.0.1"), app, f


def _rewrite(f: Path, edit) -> None:
    # What a script outside the app does: read, change, write the bytes back.
    data = json.loads(f.read_text(encoding="utf-8"))
    edit(data)
    f.write_bytes(dump_track_json(data))


def _rename_first(name):
    return lambda d: d["corners"][0].update(name=name)


def test_external_rewrite_is_served_fresh_by_every_map_cache(tmp_path):
    client, app, f = _setup(tmp_path, resolver=True)
    assert client.get(f"/tracks/{SLUG}/map").json()["corners"][0]["name"] == "Turn 1"
    assert app.state.map_for(SLUG).corners[0].name == "Turn 1"     # cmaps (coach, race)
    _rewrite(f, _rename_first("Rewritten On Disk"))
    # map_for first: a GET would refresh cmaps as a side effect and hide a stale one.
    assert app.state.map_for(SLUG).corners[0].name == "Rewritten On Disk"
    assert client.get(f"/tracks/{SLUG}/map").json()["corners"][0]["name"] == "Rewritten On Disk"


def test_external_notes_rewrite_is_served_fresh(tmp_path):
    client, _, f = _setup(tmp_path)
    client.get(f"/tracks/{SLUG}/map")
    _rewrite(f, lambda d: d["notes"].update(overview="written by a script"))
    assert client.get(f"/tracks/{SLUG}/notes").json()["overview"] == "written by a script"


def test_unchanged_notes_save_after_external_rewrite_is_refused(tmp_path):
    # The incident: the screen held empty notes, a script wrote real ones (and a new
    # corner set), then an unchanged save put the empty notes back over them.
    client, _, f = _setup(tmp_path)
    _rewrite(f, lambda d: d.pop("notes"))
    held = client.get(f"/tracks/{SLUG}/notes").json()
    client.get(f"/tracks/{SLUG}/map")
    _rewrite(f, lambda d: (d.update(notes={"overview": "real notes"}),
                           d["corners"].pop()))
    r = client.put(f"/tracks/{SLUG}/notes",
                   json={"track": SLUG, "notes": held, "rev": held["rev"]})
    assert r.status_code == 409, r.text
    assert json.loads(f.read_text(encoding="utf-8"))["notes"] == {"overview": "real notes"}


def _drain_map(ws, limit=40):
    for _ in range(limit):
        fr = ws.receive_json()
        if fr["topic"] == "map":
            return fr["data"]
    raise AssertionError("no map frame")


def test_events_pushes_a_new_map_after_external_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(live_ws_mod, "_EVENTS_PERIOD", 0.01)
    monkeypatch.setattr(smm, "read_live_status",
                        lambda: {"track": SLUG, "car": "", "state": "garage"})
    client, _, f = _setup(tmp_path)
    with client.websocket_connect("/events", headers={"host": "127.0.0.1"}) as ws:
        assert _drain_map(ws)["corners"][0]["name"] == "Turn 1"
        _rewrite(f, _rename_first("Rewritten Outside The App"))
        assert _drain_map(ws)["corners"][0]["name"] == "Rewritten Outside The App"
