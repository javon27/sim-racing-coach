import os
import time

from simcoach import car_lookup
from simcoach.car_lookup import _scan_results, resolve_car, resolve_car_class

_RESULT_XML = """<?xml version="1.0"?>
<rFactorXML>
 <RaceResults>
  <Race>
   <Driver><VehName>Team WRT 2026 #32:WEC</VehName>
    <CarType>BMW M4 LMGT3</CarType><CarClass>GT3</CarClass></Driver>
   <Driver><VehName>Iron Dames 2025 #85:ELMS</VehName>
    <CarType>Porsche 911 GT3 R LMGT3</CarType><CarClass>GT3</CarClass></Driver>
   <Driver><VehName>No Model Entry #99</VehName><CarClass>GTE</CarClass></Driver>
  </Race>
 </RaceResults>
</rFactorXML>
"""


def _fixture_dir(tmp_path):
    (tmp_path / "2026_01_01-R1.xml").write_text(_RESULT_XML)
    (tmp_path / "junk.xml").write_text("not xml <<<")  # unparseable -> skipped, not raised
    return tmp_path


def test_scan_scrapes_vehname_to_model_and_class(tmp_path):
    table = _scan_results(str(_fixture_dir(tmp_path)))
    assert table["Team WRT 2026 #32:WEC"] == ("BMW M4 LMGT3", "GT3")
    assert table["Iron Dames 2025 #85:ELMS"] == ("Porsche 911 GT3 R LMGT3", "GT3")
    # a <Driver> with no CarType contributes nothing (VehName alone is useless)
    assert "No Model Entry #99" not in table


def test_scan_missing_dir_is_empty_not_error():
    assert _scan_results(None) == {}
    assert _scan_results("/no/such/results/dir") == {}


def test_known_entry_resolves_to_model_and_class(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table",
                        lambda: {"Iron Dames 2025 #85:ELMS": ("Porsche 911 GT3 R LMGT3", "GT3")})
    assert resolve_car("Iron Dames 2025 #85:ELMS", "GT3") == "Porsche 911 GT3 R LMGT3"
    assert resolve_car_class("Iron Dames 2025 #85:ELMS") == "GT3"
    assert resolve_car_class("Unknown #1") is None


def test_unknown_entry_falls_back_to_class(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", dict)
    assert resolve_car("Some New Team #7:WEC", "GT3") == "GT3 car"


def test_unknown_entry_no_class_returns_raw(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", dict)
    assert resolve_car("Some New Team #7", None) == "Some New Team #7"


def _one_driver_xml(veh, car_type, car_class):
    return (f"<x><RaceResults><Race><Driver><VehName>{veh}</VehName>"
            f"<CarType>{car_type}</CarType><CarClass>{car_class}</CarClass>"
            f"</Driver></Race></RaceResults></x>")


def test_entry_table_refreshes_when_results_dir_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(car_lookup, "results_dir", lambda: tmp_path)
    car_lookup._cache["sig"] = None
    car_lookup._cache["table"] = {}
    (tmp_path / "a.xml").write_text(_one_driver_xml("A #1", "Car A", "GT3"))
    assert car_lookup._entry_table().get("A #1") == ("Car A", "GT3")
    # A new session logs a new car -> the Results dir mtime changes -> re-scan picks
    # it up without a restart (utime forces a distinct signature deterministically).
    (tmp_path / "b.xml").write_text(_one_driver_xml("B #2", "Car B", "Hyper"))
    os.utime(tmp_path, (time.time() + 5, time.time() + 5))
    table = car_lookup._entry_table()
    assert table.get("B #2") == ("Car B", "Hyper")
    assert table.get("A #1") == ("Car A", "GT3")
