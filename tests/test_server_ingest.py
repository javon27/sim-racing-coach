import json

from fastapi.testclient import TestClient

from simcoach.coach import Coach
from simcoach.model.fake import FakeModel
from simcoach.server.app import create_app
from simcoach.store import LapStore

_XML = """<?xml version="1.0"?>
<rFactorXML>
<RaceResults>
<PlayerFile>Settings</PlayerFile>
<DateTime>1750472422</DateTime>
<TrackCourse>Test Track</TrackCourse>
<Race>
<DateTime>1750473029</DateTime>
<Driver>
<Name>Jayson G</Name>
<VehName>Car</VehName>
<CarClass>GT3</CarClass>
<Position>1</Position>
<ClassPosition>1</ClassPosition>
<Lap num="1" s1="30.0" s2="30.0" s3="30.0">90.0</Lap>
<BestLapTime>90.0</BestLapTime>
<FinishStatus>Finished Normally</FinishStatus>
</Driver>
</Race>
</RaceResults>
</rFactorXML>
"""


def _client(tmp_path, monkeypatch, mem_keyring):
    lmu_dir = tmp_path / "UserData"
    results_dir = lmu_dir / "Log" / "Results"
    results_dir.mkdir(parents=True)
    player_dir = lmu_dir / "player"
    player_dir.mkdir(parents=True)
    # Real Settings.JSON nests "Player Name" under "DRIVER" (issue #86).
    (player_dir / "Settings.JSON").write_text(json.dumps({"DRIVER": {"Player Name": "Jayson G"}}))
    (results_dir / "race1.xml").write_text(_XML)

    monkeypatch.setenv("SIMCOACH_DATA_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SIMCOACH_LMU_DIR", str(lmu_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = LapStore(str(tmp_path / "c.sqlite"), str(tmp_path / "data"))
    app = create_app(store, Coach(FakeModel()))
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_post_ingest_returns_summary(tmp_path, monkeypatch, mem_keyring):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)

    r = c.post("/ingest", json={})

    assert r.status_code == 200
    assert r.json() == {"scanned": 1, "imported": 1, "skipped": 0, "unmatched": 0, "errors": 0}
    assert len(store.recent_race_sessions()) == 1


def test_post_ingest_bodyless_is_422(tmp_path, monkeypatch, mem_keyring):
    # JSON body required (CSRF guard, same mechanism as /mapcapture/*) -- a
    # bodyless POST stops being a CORS "simple request" and is rejected.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.post("/ingest").status_code == 422


def test_post_ingest_without_content_type_is_422(tmp_path, monkeypatch, mem_keyring):
    # The CSRF guard rests on FastAPI's strict_content_type=True default (0.132+):
    # a body with NO Content-Type must NOT be parsed as JSON. An absent
    # Content-Type is CORS-simple, so if FastAPI still parsed it the attacker gets
    # the handler with no preflight and the Body(...) guard is worthless.
    # Pins the BEHAVIOUR, so it fails if fastapi is downgraded below the
    # pyproject floor: verified 200 ("ran") on 0.130.0 and 0.131.0, 422 on
    # 0.132.0 and 0.141.1.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.post("/ingest", content=b"{}").status_code == 422


# --- Origin guard (CSRF defence in depth, independent of the fastapi version) ---
# CORSMiddleware only decides whether the browser hands the RESPONSE back to
# script; a CORS-simple request is still sent and still executed. So CORS can
# never stop a side effect -- only the server refusing the request can.

def test_hostile_origin_is_403_before_the_side_effect(tmp_path, monkeypatch, mem_keyring):
    c, store = _client(tmp_path, monkeypatch, mem_keyring)

    r = c.post("/ingest", json={}, headers={"Origin": "http://evil.example"})

    assert r.status_code == 403
    assert store.recent_race_sessions() == []  # the ingest never ran


def test_missing_origin_still_allowed(tmp_path, monkeypatch, mem_keyring):
    # Native/Tauri and non-browser clients send no Origin; only a browser does.
    c, store = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.post("/ingest", json={}).status_code == 200
    assert len(store.recent_race_sessions()) == 1


def test_tauri_and_dev_origins_still_allowed(tmp_path, monkeypatch, mem_keyring):
    # The whole legitimate set: the Tauri webview origin, the Vite dev origin
    # (src-tauri/tauri.conf.json devUrl), and the sidecar's own port under both
    # names ALLOWED_HOSTS accepts as a Host.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    # tauri://localhost is the macOS/Linux webview origin -- it passed the old
    # hostname-only compare too, so #60 must not lock those builds out (#54).
    # Both local ports appear under BOTH host aliases: 127.0.0.1 and localhost
    # are the same server, and ALLOWED_HOSTS accepts either as a Host, so the
    # guard must accept either as an Origin. Listing 5173 for only one of them
    # would refuse a dev who browses to http://127.0.0.1:5173.
    for origin in ("http://tauri.localhost", "tauri://localhost",
                   "http://localhost:5173", "http://127.0.0.1:5173",
                   "http://127.0.0.1:8765", "http://localhost:8765"):
        r = c.post("/ingest", json={}, headers={"Origin": origin})
        assert r.status_code == 200, f"{origin} must stay allowed, got {r.status_code}"


def test_other_localhost_ports_are_refused_before_the_side_effect(tmp_path, monkeypatch,
                                                                 mem_keyring):
    # #60: the guard used to compare urlparse(...).hostname only, so ANY port on
    # localhost/127.0.0.1 passed -- another local dev server, or an XSS in one,
    # could drive every guarded route. RED before the fix (both were 200 and the
    # ingest ran); the store assertion is the part that matters, since a refused
    # status with the side effect already committed would be worthless.
    for i, origin in enumerate(("http://127.0.0.1:9999", "http://localhost:3000")):
        c, store = _client(tmp_path / str(i), monkeypatch, mem_keyring)  # fresh store each
        r = c.post("/ingest", json={}, headers={"Origin": origin})
        assert r.status_code == 403, f"{origin} must be refused, got {r.status_code}"
        assert store.recent_race_sessions() == []  # the ingest never ran


def test_origin_shapes_already_refused_stay_refused(tmp_path, monkeypatch, mem_keyring):
    # GREEN BEFORE AND AFTER #60 -- a regression guard, not red-green evidence.
    # These all failed the old hostname compare too; pinned here so the switch to
    # a full scheme+host+port compare can't quietly re-open any of them.
    for i, origin in enumerate(("https://evil.com", "http://127.0.0.1.evil.com",
                                "http://localhost.evil.com", "null", "file://",
                                "http://[::1]")):
        c, store = _client(tmp_path / str(i), monkeypatch, mem_keyring)  # fresh store each
        r = c.post("/ingest", json={}, headers={"Origin": origin})
        assert r.status_code == 403, f"{origin} must be refused, got {r.status_code}"
        assert store.recent_race_sessions() == []


def test_hostile_origin_get_carrying_origin_is_blocked(tmp_path, monkeypatch, mem_keyring):
    # Not filtered to mutating methods: any GET that CARRIES a disallowed Origin
    # (a cors-mode fetch, not a no-cors <img>/<script> load) is refused too.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.get("/setup", headers={"Origin": "http://evil.example"}).status_code == 403


def test_no_origin_get_is_not_blocked_this_is_why_issue_51_exists(tmp_path, monkeypatch, mem_keyring):
    # A browser does NOT attach Origin to a no-cors GET (<img src=...>, <script>,
    # <link>, <iframe>, top-level navigation all send none), so _bad_origin sees
    # no Origin and this guard does NOT stop the request -- it reaches the
    # handler. This is the residual gap the comment above _host_guard documents.
    # #51 used to give that gap a consequence: GET /setup wrote to the DB
    # (save_session_setup), so a same-window no-Origin GET could trigger a
    # write this guard couldn't see coming to stop. #51 fixed that by moving
    # persistence to POST /setup/capture, so the gap this test pins is now
    # read-only -- it still reaches the handler, there's just nothing left to
    # do there but read.
    c, _ = _client(tmp_path, monkeypatch, mem_keyring)
    assert c.get("/setup").status_code == 200
