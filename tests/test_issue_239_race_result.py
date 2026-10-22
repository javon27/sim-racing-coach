"""#239 (rig 2026-10-21, Bahrain Outer): get_race_result showed three raw incident strings
and "(+7 more)", never marked the player as "you", and left the model to guess the lap
(it took the impact magnitude in "(201.86)" for a time) and to count places back.

The fixture is cut from that race's real log (2026_10_21_19_06_46-58R1.xml): the player's
real <Lap et> values (a lap's START ET -- 68103 of 68103 consecutive real lap pairs) and the
real Weissel stream lines. The Carter contact and the Post touch are real line shapes from
the same log, re-pointed at the player so there are two other cars and a non-car."""
import sqlite3

from simcoach.coach_tools import _incident_groups, _net_places, race_result_text
from simcoach.lmu.results import parse_result
from simcoach.store import SCHEMA_VERSION, LapStore

STREAM = """\
<Incident et="73.3">Super Noodle(10) reported contact (664.75) with another vehicle Manaus SimRacing(2)</Incident>
<Incident et="150.2">Jayson G(15) reported contact (198.85) with another vehicle Raymond Carter(3)</Incident>
<Incident et="395.3">Jayson G(15) reported contact (201.86) with another vehicle Niklas Weissel(9)</Incident>
<Incident et="395.3">Niklas Weissel(9) reported contact (274.76) with another vehicle Jayson G(15)</Incident>
<Incident et="395.8">Jayson G(15) reported contact (375.42) with another vehicle Niklas Weissel(9)</Incident>
<Incident et="396.1">Jayson G(15) reported contact (667.60) with another vehicle Niklas Weissel(9)</Incident>
<TrackLimits Driver="Jayson G" ID="15" Lap="3" WarningPoints="0" CurrentPoints="0" Resolution="7" et="405.9">No Further Action</TrackLimits>
<Incident et="450.7">Jayson G(15) reported contact (5.91) with Post</Incident>
<TrackLimits Driver="Jayson G" ID="15" Lap="4" WarningPoints="0" CurrentPoints="0" Resolution="7" et="485.0">No Further Action</TrackLimits>
<Incident et="900.0">Jayson G(15) reported contact (13.48) with Post</Incident>
"""

LAPS = """\
<Lap num="1" p="9" et="127.2625" s1="45.6467" s2="15.9556" s3="22.3017">83.9040</Lap>
<Lap num="2" p="8" et="211.1799" s1="37.3128" s2="15.9755" s3="22.2311">75.5194</Lap>
<Lap num="3" p="8" et="286.7000" s1="36.7451" s2="15.9116" s3="22.0331">74.6898</Lap>
<Lap num="4" p="13" et="361.3799" s1="45.8361" s2="16.8015" s3="22.0896">84.7272</Lap>
<Lap num="5" p="14" et="446.1201" s1="36.5849" s2="16.3318" s3="22.6817">75.5984</Lap>
"""

XML = f"""<?xml version="1.0" encoding="utf-8"?>
<rFactorXML version="1.0">
<RaceResults>
<Setting>Multiplayer</Setting>
<TrackCourse>Bahrain Outer Circuit</TrackCourse>
<Race>
<DateTime>1792626148</DateTime>
<Stream>
{STREAM}</Stream>
<Driver>
<Name>Niklas Weissel</Name>
<VehName>Racing Spirit of Leman 2026 #10:LM</VehName>
<CarClass>GT3</CarClass>
<GridPos>9</GridPos><Position>11</Position><ClassGridPos>9</ClassGridPos><ClassPosition>11</ClassPosition>
<BestLapTime>74.3543</BestLapTime>
<FinishStatus>Finished Normally</FinishStatus>
</Driver>
<Driver>
<Name>Jayson G</Name>
<VehName>13 Autosport 2026 #13:LM</VehName>
<CarClass>GT3</CarClass>
<GridPos>15</GridPos><Position>12</Position><ClassGridPos>15</ClassGridPos><ClassPosition>12</ClassPosition>
{LAPS}<BestLapTime>73.9299</BestLapTime>
<FinishStatus>Finished Normally</FinishStatus>
</Driver>
</Race>
</RaceResults>
</rFactorXML>
"""


def _text(tmp_path):
    xml = tmp_path / "bahrain.xml"
    xml.write_text(XML)
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    store.save_result_session(parse_result(xml, "Jayson G"), session_id="log")
    return race_result_text(store, "log")


def test_each_incident_carries_its_et_and_the_lap_it_fell_in(tmp_path):
    xml = tmp_path / "bahrain.xml"
    xml.write_text(XML)
    inc = parse_result(xml, "Jayson G").incidents
    # 395.3 is in lap 4 (starts 361.38, lap 5 at 446.12) -- NOT lap 3, which the
    # TrackLimits Lap="3" attribute (laps completed) would say. 900.0 is after lap 5
    # ended (446.12 + 75.60): no lap for it.
    assert [(i["et"], i["lap"]) for i in inc] == [
        (150.2, 1), (395.3, 4), (395.3, 4), (395.8, 4), (396.1, 4), (450.7, 5),
        (900.0, None), (405.9, 4), (485.0, 5)]
    assert inc[2] == {"kind": "contact", "other": "Niklas Weissel", "car": True,
                      "by_me": False, "et": 395.3, "lap": 4,
                      "text": "Niklas Weissel(9) reported contact (274.76) with another "
                              "vehicle Jayson G(15)"}


def test_incidents_are_grouped_by_the_other_party_with_nothing_hidden(tmp_path):
    out = _text(tmp_path)
    assert "more)" not in out and "Super Noodle" not in out
    assert ("Incident reports in the result log (9), grouped. Every car in a contact "
            "logs its own report, so one touch can be two or more reports, and a report "
            "does not say who was at fault:") in out
    assert ("- contact with Niklas Weissel (another car): 4 reports (3 logged by you, "
            "1 by Niklas Weissel), lap 4") in out
    assert ("- contact with Raymond Carter (another car): 1 report (1 logged by you, "
            "0 by Raymond Carter), lap 1") in out
    assert "- contact with Post (not a car): 2 reports, lap 5; 1 with lap not known" in out
    assert "- track limits: 2 reports (No Further Action), laps 4, 5" in out


def test_net_places_are_computed_not_left_to_the_model(tmp_path, synthetic_lap):
    """The rig arc: P9 after lap 1, P8, dropped to P14, finished P12 from a P15 grid. The
    model said "clawing three of them back" -- two from the lowest point."""
    import dataclasses
    xml = tmp_path / "bahrain.xml"
    xml.write_text(XML)
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for i, p in enumerate([9, 8, 8, 13, 14, 14, 12]):
        store.save(dataclasses.replace(synthetic_lap, session_id="sess", phase="race",
                                       timestamp=1792626200.0 + i, position=p,
                                       lap_no=i + 1, pit=False))
    store.save_result_session(parse_result(xml, "Jayson G"), session_id="sess")
    out = race_result_text(store, "sess")
    assert ("Places (computed, overall): grid P15 to finish P12 = 3 gained; lowest race-lap place "
            "(pit laps left out) P14 to finish P12 = 2 regained.") in out


def test_a_best_lap_is_not_race_pace(tmp_path):
    out = _text(tmp_path)
    assert ("A best lap is one lap, not race pace: a quicker best lap does not mean "
            "quicker over the race -- the finishing order says that.") in out


def test_legacy_string_incidents_render_in_full_with_lap_not_known(tmp_path):
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    xml = tmp_path / "bahrain.xml"
    xml.write_text(XML)
    store.save_result_session(parse_result(xml, "Jayson G"), session_id="log")
    store.conn.execute("UPDATE race_sessions SET incidents = ?",
                       ('["a", "b", "c", "d"]',))
    out = race_result_text(store, "log")
    assert "more)" not in out
    assert "- a, lap not known" in out and "- d, lap not known" in out


def test_v28_db_re_reads_the_logs_to_backfill_lap_and_et(tmp_path):
    db = tmp_path / "db.sqlite"
    LapStore(str(db), str(tmp_path / "telem")).conn.close()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO ingested_files VALUES ('x.xml', 1.0)")
    conn.execute("PRAGMA user_version = 28")
    conn.commit()
    conn.close()
    store = LapStore(str(db), str(tmp_path / "telem"))
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.ingested_files() == set()


def test_a_rival_whose_name_starts_with_the_players_is_not_the_player(tmp_path):
    # "Jayson G" is a substring of "Jayson Gordon": his contact with Bob is not ours.
    xml = tmp_path / "bahrain.xml"
    xml.write_text(XML.replace(
        "<Incident et=\"73.3\">",
        "<Incident et=\"380.0\">Jayson Gordon(4) reported contact (88.10) with another "
        "vehicle Bob(3)</Incident><Incident et=\"73.3\">"))
    inc = parse_result(xml, "Jayson G").incidents
    assert not any("Gordon" in i["text"] for i in inc)
    assert len(inc) == 9


def _rr(**kw):
    return {"grid_position": 15, "position": 12, "class_grid_position": 5,
            "class_position": 4, **kw}


def _arc(positions, pit=None):
    return {"positions": positions, "pit": pit if pit is not None else [False] * len(positions)}


def test_net_places_leave_pit_laps_out_of_the_low():
    # Running P3, P15 while stopped, finished P4: the stop is not a "regained 11".
    arc = _arc([(3, None), (15, None), (4, None)], [False, True, False])
    assert _net_places(_rr(grid_position=3, position=4), arc, False) == (
        "Places (computed, overall): grid P3 to finish P4 = 1 lost; lowest race-lap place "
        "(pit laps left out) P4 to finish P4 = 0 regained.")


def test_net_places_say_pit_stops_are_included_when_a_lap_has_no_pit_flag():
    arc = _arc([(3, None), (15, None), (4, None)], [False, None, False])
    assert "worst captured lap place, pit stops included P15 to finish P12 = 3 regained"         in _net_places(_rr(), arc, False)


def test_net_places_in_class_without_grid_or_lows():
    # Multiclass: class numbers, never overall ones. No class grid, no class laps.
    arc = _arc([(17, None), (19, None)])
    assert _net_places(_rr(class_grid_position=None), arc, True) is None
    assert _net_places(_rr(), arc, True) == (
        "Places (computed, in class): grid P5 to finish P4 = 1 gained.")
    assert _net_places(_rr(), _arc([(17, 6), (19, 4)]), True) == (
        "Places (computed, in class): grid P5 to finish P4 = 1 gained; lowest race-lap "
        "place (pit laps left out) P6 to finish P4 = 2 regained.")


def test_net_places_low_never_better_than_the_finish():
    # Every captured lap better than the finish (the last lap uncaptured): low = finish.
    assert _net_places(_rr(), _arc([(10, None), (11, None)]), False).endswith(
        "lowest race-lap place (pit laps left out) P12 to finish P12 = 0 regained.")
    assert _net_places(_rr(), None, False) == (
        "Places (computed, overall): grid P15 to finish P12 = 3 gained.")


def test_a_contact_with_no_other_party_never_renders_none():
    out = _incident_groups([{"kind": "contact", "other": None, "et": 1.0, "lap": 2,
                             "text": "garbled contact line"},
                            {"kind": "contact", "et": 1.0, "lap": None}])
    assert not any("None" in line for line in out)
    assert out == ["- garbled contact line, lap 2",
                   "- incident (no text logged), lap not known"]


def test_an_untimed_lap_ends_at_the_next_lap_not_at_the_flag(tmp_path):
    """Red team: 2025_07_12_20_35_58-55R1 logs lap 1 as "--.----"; read as open-ended it
    claimed every cooldown contact (ET past the last lap's end) as "lap 1"."""
    xml = tmp_path / "untimed.xml"
    xml.write_text(XML.replace(">83.9040</Lap>", ">--.----</Lap>").replace(
        '<Incident et="900.0">', '<Incident et="530.0">'))
    inc = parse_result(xml, "Jayson G").incidents
    by_et = {i["et"]: i["lap"] for i in inc}
    assert by_et[150.2] == 1        # still inside the untimed lap 1 (ends at lap 2's et)
    assert by_et[530.0] is None     # after lap 5 ended (446.12 + 75.60): no lap


def test_a_lap_1_pit_flag_is_not_a_stop(tmp_path, synthetic_lap):
    """Red team: live capture flags the grid-start lap 1 as pit (4 of 34 races); Road
    Atlanta's P45 on lap 1 became "lowest P38 -> 17 regained"."""
    import dataclasses
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    for i, (p, pit) in enumerate([(45, True), (38, False), (30, True), (28, False)]):
        store.save(dataclasses.replace(synthetic_lap, session_id="s", phase="race",
                                       timestamp=1000.0 + i, position=p, lap_no=i + 1,
                                       pit=pit))
    arc = store.session_position_arc("s", phase="race")
    assert arc["pit"] == [False, False, True, False]
    rr = {"grid_position": 40, "position": 28, "class_grid_position": 40,
          "class_position": 28}
    assert "lowest race-lap place (pit laps left out) P45 to finish P28 = 17 regained" \
        in _net_places(rr, arc, False)
