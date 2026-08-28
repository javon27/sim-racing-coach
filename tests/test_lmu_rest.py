import pytest
from simcoach.lmu.rest import LmuRest

# Real shapes observed on localhost:6397 during the feasibility spike:
STANDINGS = [{
    "player": True, "driverName": "Jayson G", "carClass": "GT3", "carNumber": 34,
    "lapDistance": 6126.6982421875, "fuelFraction": 0.545098066329956,
    "sector": "SECTOR3", "timeIntoLap": 121.751792907715, "lapStartET": 751.904968261719,
    "estimatedLapTime": 141.318939208984, "lapsCompleted": 1,
    "lastLapTime": -1, "bestLapTime": -1,
    "carVelocity": {"velocity": 59.3323822021484, "x": -1.16, "y": 0.1, "z": -59.32},
    "pitState": "NONE", "pitting": False, "position": 4,
    "vehicleName": "Racing Team Turkey by TF 2026 #34:WEC",
}]
SETUP = {
    "VM_ANTILOCKBRAKESYSTEMMAP": {"stringValue": "9 (Understeer)", "isFreeSetting": True,
                                  "minValue": 0, "maxValue": 10},
    "VM_TRACTIONCONTROLMAP": {"stringValue": "8", "isFreeSetting": True},
    "VM_SOME_LOCKED_PART": {"stringValue": "x", "isFreeSetting": False},
}


def make(fetch_map):
    return LmuRest(fetch=lambda path: fetch_map[path])


FMAP = {
    "/rest/watch/standings": STANDINGS,
    "/rest/garage/getPlayerGarageData": SETUP,
}


def test_player_standing_picks_player_entry():
    r = make(FMAP)
    s = r.get_player_standing()
    assert s["driverName"] == "Jayson G" and s["carClass"] == "GT3"


def test_player_standing_fallback_first_when_no_player_flag():
    data = [{"driverName": "A", "player": False}, {"driverName": "B", "player": False}]
    r = LmuRest(fetch=lambda p: data)
    assert r.get_player_standing()["driverName"] == "A"


def test_fuel_fraction():
    assert abs(make(FMAP).fuel_fraction() - 0.545098066329956) < 1e-9


def test_timing_fields():
    t = make(FMAP).timing()
    assert abs(t["lapDistance"] - 6126.6982421875) < 1e-6
    assert t["sector"] == "SECTOR3" and t["lapsCompleted"] == 1


def test_adjustable_keys_uses_isFreeSetting():
    keys = make(FMAP).adjustable_keys()
    assert "VM_ANTILOCKBRAKESYSTEMMAP" in keys and "VM_TRACTIONCONTROLMAP" in keys
    assert "VM_SOME_LOCKED_PART" not in keys


def test_empty_standings_returns_none():
    r = LmuRest(fetch=lambda p: [])
    assert r.get_player_standing() is None
    assert r.fuel_fraction() is None
