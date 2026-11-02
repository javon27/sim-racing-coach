import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_reference_times",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_reference_times.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

# Real rows from the sheet's Laptimes tab (2026-10-30), header rows included, so the
# parser is tested against the sheet's own layout rather than one we invented.
SHEET = (
    ",Last updated: 2026.10.30. 08:48 CEST,,,,,,,,,,,,,\n"
    ",,,Hotlap/Q,R a c e   p a c e,,,,,,,,,,\n"
    ",Track,Patch,Class avgW,~100%,101%,102%,103%,104%,105%,106%,107%,Fastest car,Laptime,Best/Avg\n"
    "Bahrain (outer)LMGT3,Bahrain (outer),1.4+,1:12.02,1:12.38,1:13.11,1:13.83,1:14.55,"
    "1:15.28,1:16.00,1:16.73,1:17.45,Lamborghini Huracan EVO II (v1.41),1:11.87,-0.31%\n"
    "Bahrain (outer)LMP2elms,Bahrain (outer),1.4+,1:04.53,1:04.85,1:05.50,1:06.14,1:06.79,"
    "1:07.43,1:08.08,1:08.72,1:09.37,Oreca ELMS (v1.4),1:04.53,0.00%\n"
    "Bahrain (outer)LMP2wec,Bahrain (outer),1.4+,1:06.83,1:07.16,1:07.83,1:08.50,1:09.17,"
    "1:09.84,1:10.51,1:11.18,1:11.85,Oreca WEC (v1.41),1:06.83,0.00%\n"
    "Long BeachGTE,Long Beach,0.5+,0:00.00,0:00.00,0:00.00,0:00.00,0:00.00,0:00.00,0:00.00,"
    "0:00.00,0:00.00, (v),0:00.00,0.00%\n"
    "Bahrain (outer)GTE,Bahrain (outer),0.5+,1:11.18,1:11.54,1:12.25,1:12.97,1:13.68,"
    "1:14.40,1:15.11,1:15.83,1:16.54,Aston Martin (v0.5),1:11.13,-0.11%\n"
)
# A row in the sheet's shape, for the guards the real rows don't exercise.
_ROW = "{key},{track},{patch},1:00.00,,,,,,,,,Car (v1.4),{time},0.00%\n"


def _with(**kw):
    return SHEET + _ROW.format(**{"key": "SpaLMGT3", "track": "Spa", "patch": "1.4+",
                                  "time": "2:16.73", **kw})


def test_fastest_car_time_per_class_with_wec_and_elms_kept_apart():
    sheet = sync.parse_laptimes(SHEET)
    assert sync.reference_times(sheet, "Bahrain (outer)") == {
        "LMGT3": "1:11.87", "LMP2": "1:06.83", "LMP2_ELMS": "1:04.53"}


def test_an_older_patch_time_is_stale_and_cut():
    # #293 review: every GTE row is 0.5+ to 1.2+ while the rest are 1.4+; a v0.5 time
    # stated as today's class benchmark breaks "a stale value is cut, not hedged".
    assert "GTE" not in sync.reference_times(sync.parse_laptimes(SHEET), "Bahrain (outer)")


def test_a_placeholder_zero_time_is_never_a_reference():
    # On the current patch, so only the placeholder guard can drop it.
    sheet = sync.parse_laptimes(SHEET.replace("Long BeachGTE,Long Beach,0.5+",
                                              "Long BeachGTE,Long Beach,1.4+"))
    assert sync.reference_times(sheet, "Long Beach") == {}


@pytest.mark.parametrize("time", ["99:99.999", "0:00.01", "1:75.00"])
def test_an_implausible_time_is_never_a_reference(time):
    assert sync.reference_times(sync.parse_laptimes(_with(time=time)), "Spa") == {}


def test_unknown_classes_and_mismatched_rows_are_ignored():
    assert ("Spa", "LMH2") not in sync.parse_laptimes(_with(key="SpaLMH2"))
    # Column A must start with the row's own Track name: "SpbLMGT3" sliced at len("Spa")
    # would read as a Spa LMGT3 time.
    assert ("Spa", "LMGT3") not in sync.parse_laptimes(_with(key="SpbLMGT3"))


def test_a_duplicate_track_class_fails_loudly():
    with pytest.raises(ValueError):
        sync.parse_laptimes(_with() + _ROW.format(key="SpaLMGT3", track="Spa",
                                                   patch="1.4+", time="2:30.00"))


def test_a_relaid_out_sheet_fails_loudly():
    with pytest.raises(ValueError):
        sync.parse_laptimes(SHEET.replace("Laptime", "Lap time"))
    with pytest.raises(ValueError):
        sync.sheet_updated(SHEET.replace("Last updated", "Updated"))


def test_credit_is_one_sentence_replaced_on_each_run():
    assert sync.sheet_updated(SHEET) == "2026-10-30"
    once = sync.with_credit("LMU wiki, CC-BY-SA.", "2026-10-30")
    twice = sync.with_credit(once, "2026-11-15")
    assert twice.startswith("LMU wiki, CC-BY-SA. Reference lap times: ")
    assert twice.count("Reference lap times: ") == 1
    assert twice.endswith("sheet updated 2026-11-15.")
    assert sync.with_credit("", "2026-10-30").startswith("Reference lap times: ")


def test_credit_never_eats_a_human_written_clause():
    # #293 review: the old pattern removed anything from "Reference lap times: " to the
    # first "sheet updated <date>.", including text a human wrote.
    human = "Reference lap times: by hand, sheet updated 2025-01-01. Wiki CC-BY-SA."
    out = sync.with_credit(human, "2026-10-30")
    assert out.startswith(human + " Reference lap times: fastest car per class")
