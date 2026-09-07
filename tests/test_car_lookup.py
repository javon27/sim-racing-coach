import simcoach.car_lookup as car_lookup
from simcoach.car_lookup import _scan_results, resolve_car

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


def test_known_entry_resolves_to_model(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table",
                        lambda: {"Iron Dames 2025 #85:ELMS": ("Porsche 911 GT3 R LMGT3", "GT3")})
    assert resolve_car("Iron Dames 2025 #85:ELMS", "GT3") == "Porsche 911 GT3 R LMGT3"


def test_unknown_entry_falls_back_to_class(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", lambda: {})
    assert resolve_car("Some New Team #7:WEC", "GT3") == "GT3 car"


def test_unknown_entry_no_class_returns_raw(monkeypatch):
    monkeypatch.setattr(car_lookup, "_entry_table", lambda: {})
    assert resolve_car("Some New Team #7", None) == "Some New Team #7"
