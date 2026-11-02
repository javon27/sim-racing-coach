"""DEV ONLY. Refresh each shipped track's notes.reference_times from the community LMU
lap-time sheet (by ohne_speed: discord.com/invite/dFAqhnuSXH, youtube.com/@ohne_speed),
whose own sources are beAlien, Hymo and GoSetups. Run before a release, review the diff,
commit:

    .venv/Scripts/python.exe scripts/sync_reference_times.py            # dry run: print changes
    .venv/Scripts/python.exe scripts/sync_reference_times.py --write    # rewrite the track files

The value per class is the sheet's "Laptime" of its "Fastest car" (the class's fastest
hotlap on the current patch). A mapped track's reference_times is replaced whole; a track
not in TRACKS is left alone. Each updated track's notes.source gets one credit sentence
(CREDIT, dated by the sheet's own "Last updated"), replacing any earlier one, so the
credit shows on the Tracks screen; THIRD_PARTY_NOTICES.md carries the project-level
credit. --csv reads a saved copy of the Laptimes tab instead of fetching it (the fetch
uses curl: Python's urllib gets a 400 from Google's redirect).
"""
import argparse
import csv
import io
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from simcoach.track_lookup import dump_track_json

SHEET_CSV = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vTN03UvJDm99byA6vQPZHKOCYVvfx"
             "Lu1zkJAzdaKyROykzEKY2-Xl1rl1q5znZEf36m88dxMKsY2eaO/pub?single=true&output=csv"
             "&gid=1766901750")  # the "Laptimes" tab

# Our track slug -> the sheet's track name. Layouts were checked against LMU's own names:
# the live name "Fuji Speedway" is the WEC layout (4.563 km, the sheet's "chicane");
# "Fuji Speedway Classic" is a separate layout we don't ship.
TRACKS = {
    "algarve_international_circuit": "Portimao",
    "autodromo_enzo_e_dino_ferrari": "Imola",
    "autodromo_jose_carlos_pace": "Interlagos",
    "autodromo_nazionale_monza": "Monza",
    "bahrain_endurance_circuit": "Bahrain (endurance)",
    "bahrain_international_circuit": "Bahrain (wec)",
    "bahrain_outer_circuit": "Bahrain (outer)",
    "bahrain_paddock_circuit": "Bahrain (paddock)",
    "circuit_de_la_sarthe": "Circuit de la Sarthe",
    "circuit_de_spa_francorchamps": "Spa",
    "fuji_speedway": "Fuji (chicane)",
    "grand_prix_of_long_beach": "Long Beach",
    "michelin_raceway_road_atlanta": "Road Atlanta",
    "monza_curva_grande_circuit": "Monza (curvagrande)",
    "sebring_international_raceway": "Sebring",
    "silverstone_national_circuit": "Silverstone (National)",
    "weathertech_raceway_laguna_seca": "Laguna Seca",
}

# The sheet's class -> our reference_times key. The keys are the game's scraped class
# names that coach_tools._class_benchmark matches: WEC LMP2 is "LMP2", ELMS is
# "LMP2_ELMS" (12 s apart at Sarthe, #190), and they are never merged.
CLASSES = {"LMH": "Hypercar", "LMGT3": "LMGT3", "LMP2wec": "LMP2",
           "LMP2elms": "LMP2_ELMS", "LMP3": "LMP3", "GTE": "GTE"}

# One sentence in notes.source; CREDIT_PREFIX finds an earlier run's sentence to replace.
CREDIT_PREFIX = "Reference lap times: "
CREDIT = (CREDIT_PREFIX + "fastest car per class from the community LMU lap-time sheet by "
          "ohne_speed (youtube.com/@ohne_speed; sources beAlien, Hymo, GoSetups), "
          "sheet updated {updated}.")

_TIME = re.compile(r"^\d{1,2}:\d{2}\.\d{2,3}$")


def parse_laptimes(text: str) -> dict[tuple[str, str], str]:
    """{(sheet track, sheet class): 'm:ss.ss'} from the Laptimes tab CSV. Columns are
    found by the header row (Track / Laptime), not by position. Raises ValueError when
    the header is missing, so a re-laid-out sheet fails loudly instead of writing junk."""
    rows = list(csv.reader(io.StringIO(text)))
    for i, row in enumerate(rows):
        if "Track" in row and "Laptime" in row:
            track_col, time_col = row.index("Track"), row.index("Laptime")
            break
    else:
        raise ValueError("Laptimes header (Track ... Laptime) not found: the sheet changed")
    out = {}
    for row in rows[i + 1:]:
        if len(row) <= max(track_col, time_col):
            continue
        key, track, time = row[0].strip(), row[track_col].strip(), row[time_col].strip()
        # Column A is the track name with the class glued on: "Bahrain (outer)LMGT3".
        if not track or not key.startswith(track):
            continue
        cls = key[len(track):]
        # The sheet writes 0:00.00 where a class has no time (GTE at Long Beach).
        if cls in CLASSES and _TIME.match(time) and time.strip("0:.") != "":
            out[(track, cls)] = time
    return out


def sheet_updated(text: str) -> str:
    """The sheet's own 'Last updated: 2026.10.30. 08:48 CEST' as '2026-10-30'."""
    m = re.search(r"Last updated:\s*(\d{4})\.(\d{2})\.(\d{2})", text)
    if not m:
        raise ValueError("'Last updated' date not found: the sheet changed")
    return "-".join(m.groups())


def with_credit(source: str, updated: str) -> str:
    """`source` with exactly one current CREDIT sentence at the end."""
    kept = re.sub(re.escape(CREDIT_PREFIX) + r"[^\n]*?sheet updated \d{4}-\d{2}-\d{2}\.",
                  "", source or "").strip()
    return (kept + " " if kept else "") + CREDIT.format(updated=updated)


def reference_times(sheet: dict, sheet_track: str) -> dict[str, str]:
    """Our reference_times map for one track, in CLASSES order."""
    return {ours: sheet[(sheet_track, cls)] for cls, ours in CLASSES.items()
            if (sheet_track, cls) in sheet}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite the track files")
    ap.add_argument("--csv", help="read a saved Laptimes CSV instead of fetching the sheet")
    a = ap.parse_args()
    if a.csv:
        text = pathlib.Path(a.csv).read_text(encoding="utf-8")
    else:
        text = subprocess.run(["curl", "-sfL", "--max-time", "60", SHEET_CSV], check=True,
                              capture_output=True).stdout.decode("utf-8")
    sheet, updated = parse_laptimes(text), sheet_updated(text)
    tracks_dir = ROOT / "simcoach" / "tracks"
    changed = 0
    for slug, sheet_track in TRACKS.items():
        path = tracks_dir / f"{slug}.json"
        new = reference_times(sheet, sheet_track)
        if not new:
            print(f"SKIP {slug}: the sheet has no times for {sheet_track!r}")
            continue
        data = json.loads(path.read_bytes().decode("utf-8"))
        notes = data.setdefault("notes", {})
        old = notes.get("reference_times") or {}
        source = with_credit(notes.get("source") or "", updated)
        if old == new and source == notes.get("source"):
            continue
        changed += 1
        print(f"{slug} ({sheet_track}):")
        for k in sorted(set(old) | set(new)):
            if old.get(k) != new.get(k):
                print(f"  {k:10} {old.get(k, '-'):>10} -> {new.get(k, '-')}")
        if a.write:
            notes["reference_times"] = new
            notes["source"] = source
            path.write_bytes(dump_track_json(data))
    print(f"{changed} track(s) {'updated' if a.write else 'would change (dry run)'}")


if __name__ == "__main__":
    main()
