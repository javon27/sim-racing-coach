import itertools
import json
import math
from dataclasses import dataclass, fields

import numpy as np

from simcoach.model_types import LapRecord

# Calibration knob: true value for LMU confirmed by feasibility spike (Task 2)
RIGHT_IS_POSITIVE = True


def in_window(dist, start, end):
    """Is `dist` (scalar or ndarray) inside a corner window? start > end WRAPS the
    S/F seam: [start, length] u [0, end]. Works for floats and numpy arrays."""
    if start <= end:
        return (dist >= start) & (dist <= end)
    return (dist >= start) | (dist <= end)


@dataclass
class Corner:
    """A single corner in a track map."""
    number: int
    name: str
    direction: str
    start_dist: float
    apex_dist: float
    end_dist: float
    label: str = ""                       # displayed designation ("8A"); defaults to str(number)
    apexes: list[float] | None = None  # lap_distances; defaults to [apex_dist] (double-apex -> 2)


def next_corner(corners: list[Corner], dist: float) -> Corner | None:
    """The corner the car is in, else the next corner entry ahead of `dist`, wrapping
    past S/F to the first. None when there are no corners.

    Corner identity is owned data (#151): the Coach rail map's "Next: T10 Pouhon (L)"
    comes from here, server-side, never from the UI. A seam-wrapping window
    (start > end) is handled by in_window; wrap-around needs no lap length because
    "no entry ahead" simply means the lowest entry is next.
    """
    for c in corners:
        if in_window(dist, c.start_dist, c.end_dist):
            return c
    ahead = [c for c in corners if c.start_dist >= dist]
    return min(ahead or corners, key=lambda c: c.start_dist, default=None)


def validated_sectors(sectors, length: float) -> list[float]:
    """Sector splits as floats, or TypeError/ValueError naming what's wrong.

    Sector splits are committed shipped data (#41 baked captured splits into all 11
    maps), so the same rule has to hold wherever one is written: the editor's
    `PUT .../map` and the promote-to-shipped script. Owned here rather than inline in
    the endpoint so the two can't drift -- each caller catches (TypeError, ValueError)
    and translates the message into its own refusal (HTTP 422 / `refused:`). Refuses;
    never clamps or repairs."""
    if not isinstance(sectors, list):
        raise TypeError("sectors must be a list")
    # A bool is refused, not read as 1.0/0.0: `true` in a sectors list is a corrupt
    # file, and float(True) would ship it as a split at 1 m. OverflowError too: float()
    # RAISES it on an integer too large for a double, which JSON happily parses.
    if any(isinstance(s, bool) for s in sectors):
        raise ValueError("sectors must be numbers")
    try:
        out = [float(s) for s in sectors]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("sectors must be numbers") from None
    if any(not math.isfinite(s) or not (0.0 < s < length) for s in out):
        raise ValueError("sector splits must be within (0, length)")
    if any(a >= b for a, b in itertools.pairwise(out)):
        raise ValueError("sector splits must be strictly increasing")
    return out


def validated_sections(sections, length: float) -> list[dict]:
    """Named track sections in the normalised shape `CornerMap.from_data` loads, or
    TypeError/ValueError naming what's wrong. `None` is no sections.

    Same contract and same reason as `validated_sectors` above (#65): `from_data`
    RAISES on a bad span, so one unvalidated section commits a map no client can open.
    This lived inline in the editor's save endpoint, and the promote script and the
    server's other three writers wrote `sections` verbatim. Refuses bad input; the only
    change to good input is the normalisation the endpoint always applied (names capped,
    distances to 1 dp), which every shipped section already satisfies."""
    if sections is None:
        return []
    if not isinstance(sections, list):
        raise TypeError("sections must be a list")
    out = []
    for s in sections:
        if not isinstance(s, dict):
            raise TypeError("each section must be an object")
        try:
            if isinstance(s["start_dist"], bool) or isinstance(s["end_dist"], bool):
                raise TypeError
            sd, ed = float(s["start_dist"]), float(s["end_dist"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("section requires numeric start_dist/end_dist") from None
        if not (math.isfinite(sd) and math.isfinite(ed) and 0.0 <= sd < ed <= length):
            raise ValueError("section span must satisfy 0 <= start < end <= length")
        out.append({"name": str(s.get("name", ""))[:60],
                    "kind": str(s.get("kind", "section"))[:16],
                    "start_dist": round(sd, 1), "end_dist": round(ed, 1)})
    return out


def clamp_corner_windows(corners: list[Corner], length: float | None) -> None:
    """Trim each corner's start/end at the midpoint to its neighbors' apexes (by
    apex_dist, circularly around the lap) so adjacent corner windows never
    overlap. A window already inside its bounds is left untouched -- only the
    side that bleeds past a neighbor's apex is trimmed, so an intentionally
    narrow window (or an S/F-seam wrap) survives.

    Root-cause fix for adjacent-corner telemetry contamination (e.g. Laguna's
    T7 window bleeding into the Corkscrew) -- general for ANY track, not
    special-cased. Mutates `corners` in place; no-op without >=2 corners or a
    known lap length (can't do circular midpoint math without it).

    Shared by both layers: mapbuild applies it before writing a new map, so the
    file on disk is already clamped, and from_data applies it on load as a guard
    for maps written before that (or by hand)."""
    n = len(corners)
    if n < 2 or not length:
        return
    length = float(length)
    order = sorted(range(n), key=lambda i: corners[i].apex_dist)
    apexes = [corners[i].apex_dist for i in order]
    eps = 1e-6
    for k, i in enumerate(order):
        c = corners[i]
        a = c.apex_dist
        # Guard each side against the corner's OWN outermost apex on that side, not
        # apex_dist -- a double-apex window has to keep both, and apex_dist sits
        # between them. Compare the ROUNDED candidate, since that is what gets
        # assigned: testing the unrounded midpoint can shove a boundary 0.05 m past
        # an apex. If a candidate would cut an apex out, the circular difference
        # wraps to ~length and the test goes false, so the clamp declines to trim
        # rather than lose an apex.
        own = c.apexes or [a]
        a_first = min(own, key=lambda x: (x - c.start_dist) % length)  # earliest in window
        a_last = min(own, key=lambda x: (c.end_dist - x) % length)     # latest in window
        prev_a, next_a = apexes[k - 1], apexes[(k + 1) % n]
        mid_prev = (prev_a + ((a - prev_a) % length) / 2.0) % length
        mid_next = (a + ((next_a - a) % length) / 2.0) % length

        cand = round(mid_prev, 1)
        if (a_first - c.start_dist) % length > (a_first - cand) % length + eps:
            c.start_dist = cand

        cand = round(mid_next, 1)
        if (c.end_dist - a_last) % length > (cand - a_last) % length + eps:
            c.end_dist = cand


def _load_sections(data: dict) -> list[dict]:
    """Named distance spans (straights, corner-group complexes, DRS zones):
    [{name, kind, start_dist, end_dist}]. Default []; validate start<end in range."""
    length = data.get("length") or data.get("length_m")
    out = []
    for s in data.get("sections", []) or []:
        sd, ed = float(s["start_dist"]), float(s["end_dist"])
        if not (0 <= sd < ed) or (length and ed > float(length) + 1e-6):
            raise ValueError(f"section {s.get('name')!r}: bad span [{sd}, {ed}]")
        out.append({"name": str(s.get("name", "")), "kind": str(s.get("kind", "section")),
                    "start_dist": sd, "end_dist": ed})
    return out


class CornerMap:
    """Loads and queries corner data from a track map JSON file."""

    def __init__(self, corners: list[Corner], sections: list[dict] | None = None,
                 notes: dict | None = None, geometry: dict | None = None,
                 length: float | None = None):
        self.corners = corners
        # Lap length (m) when the map declares one. Kept because anything reasoning
        # ACROSS the S/F seam (see analysis._metrics_for_corner) needs the modulus.
        self.length = float(length) if length else None
        self.sections = sections or []
        self.notes = notes or {}
        # Baked per-corner geometry (radius_m/width_m/banking_deg/elev_delta_m),
        # keyed by corner label. Not corner-identity, so it's a parallel dict
        # rather than Corner fields -- see from_data.
        self.geometry = geometry or {}

    @classmethod
    def load(cls, path: str) -> "CornerMap":
        """Load corner map from JSON file. See from_data for the validation rules."""
        with open(path, encoding="utf-8") as f:
            return cls.from_data(json.load(f))

    @classmethod
    def from_data(cls, data: dict) -> "CornerMap":
        """Build from an already-parsed map dict (e.g. a shipped map merged with a
        user annotation overlay).

        start_dist > end_dist is a valid S/F-seam WRAP (spans end-of-lap into the
        next). Raises ValueError only if start == end (degenerate) or an apex
        falls outside the (possibly wrapped) window.
        """
        # Ignore keys Corner doesn't declare (e.g. baked geometry metrics
        # radius_m/width_m/banking_deg/elev_delta_m live in the JSON for the coach
        # digest but aren't corner-identity fields).
        _cf = {f.name for f in fields(Corner)}
        corners = [Corner(**{k: v for k, v in c.items() if k in _cf}) for c in data["corners"]]
        for c in corners:
            if c.start_dist == c.end_dist:
                raise ValueError(
                    f"corner {c.number}: start_dist == end_dist ({c.start_dist}) is degenerate"
                )
            for a in [c.apex_dist, *(c.apexes or [])]:
                if not in_window(a, c.start_dist, c.end_dist):
                    raise ValueError(
                        f"corner {c.number}: apex {a} outside window "
                        f"[{c.start_dist}, {c.end_dist}]"
                    )
            if not c.label:                       # back-compat: label defaults to the number
                c.label = str(c.number)
            if not c.apexes:                      # back-compat: single-apex default
                c.apexes = [c.apex_dist]
        # Baked geometry metrics (radius_m/width_m/banking_deg/elev_delta_m) aren't
        # Corner fields -- carry them separately, keyed by the corner's final label,
        # so the coach can reach this owned data (issue #35).
        geometry = {}
        for raw, c in zip(data["corners"], corners):
            extra = {k: v for k, v in raw.items() if k not in _cf}
            if extra:
                geometry[c.label] = extra
        length = data.get("length") or data.get("length_m")
        clamp_corner_windows(corners, length)
        return cls(corners, _load_sections(data), data.get("notes") or {}, geometry, length)

    def corner_at(self, dist: float) -> Corner | None:
        """Return the first corner whose window contains dist (inclusive; wrap-aware)."""
        for corner in self.corners:
            if in_window(dist, corner.start_dist, corner.end_dist):
                return corner
        return None


def compute_direction(record: LapRecord, start: float, end: float) -> str:
    """
    Compute corner direction from steering geometry.

    Returns "left" or "right" based on mean steering over [start, end] distance range.
    Sign convention controlled by RIGHT_IS_POSITIVE constant.

    Raises ValueError if the window contains no telemetry or all NaN steering samples.
    """
    lap_distance = record.channels["lap_distance"]
    steering = record.channels["steering"]

    mask = in_window(lap_distance, start, end)

    if not np.any(mask):
        raise ValueError(f"no telemetry in window [{start}, {end}]")

    # Use nanmean to skip NaN samples; result is nan if all samples are NaN
    mean_steering = np.nanmean(steering[mask])

    if np.isnan(mean_steering):
        raise ValueError(f"no telemetry in window [{start}, {end}]")

    if mean_steering > 0:
        return "right" if RIGHT_IS_POSITIVE else "left"
    else:
        return "left" if RIGHT_IS_POSITIVE else "right"


def verify_map_directions(record: LapRecord, cmap: CornerMap) -> list[tuple[int, str, str]]:
    """
    Verify that map directions match computed directions from telemetry.

    Returns list of (corner_no, declared, computed) for any mismatches or undetermined corners.
    Empty list means all directions are consistent and verifiable.
    """
    mismatches = []
    for corner in cmap.corners:
        try:
            computed = compute_direction(record, corner.start_dist, corner.end_dist)
        except ValueError:
            # Out-of-range or no telemetry window
            computed = "undetermined"

        if corner.direction != computed:
            mismatches.append((corner.number, corner.direction, computed))
    return mismatches
