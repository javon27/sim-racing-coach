"""Deterministic rendering of analysis facts for the coach — corner lines, tire
report, unit conversion. Shared by the legacy injected prompt (coach.build_prompt)
and the on-demand tools (coach_tools), so both speak identically. All numbers are
computed facts; nothing here is model-guessed."""
from __future__ import annotations

import math


def format_laptime(seconds) -> str:
    """Race lap time as m:ss.mmm (e.g. 83.456 -> '1:23.456'), matching the UI lap
    list. Non-finite/negative/None -> '—'."""
    if seconds is None or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return "—"
    # Round to ms BEFORE splitting, else e.g. 119.9996 -> "1:60.000" (the :.3f
    # rounds the seconds field up past 60) instead of "2:00.000".
    total_ms = round(seconds * 1000)
    m, rem_ms = divmod(total_ms, 60_000)
    return f"{m}:{rem_ms / 1000:06.3f}"


def unit_fns(units: str = "metric"):
    """Return (speed_fn, temp_fn, speed_label, temp_label). Distances stay meters."""
    imperial = units == "imperial"
    spd = (lambda v: v * 0.621371) if imperial else (lambda v: v)      # km/h -> mph
    temp = (lambda v: v * 9 / 5 + 32) if imperial else (lambda v: v)   # °C -> °F
    return spd, temp, ("mph" if imperial else "km/h"), ("°F" if imperial else "°C")


# ponytail: mPathLateral/mTrackEdge units + sign are still PENDING LIVE
# VALIDATION (see analysis.py) and the subtraction degrades exactly where the
# car is off-track, so a precise figure past this magnitude implies confidence
# the data can't support. Raise or remove once the channel is rig-validated.
_TRACK_EDGE_RENDER_CAP_M = 2.0


def format_corner_line(cm, units: str = "metric") -> str:
    """One driver-facing corner line: speeds, apex-relative brake/throttle, trail,
    peak g, and the delta vs best. Absolute lap-distance is never shown."""
    spd, _, spd_u, _ = unit_fns(units)
    tag = f"T{cm.label or cm.corner_no}"
    sect = f" [part of {cm.section}]" if cm.section else ""
    if math.isnan(cm.min_spd):
        # No telemetry in this corner's window (e.g. a sparse out-lap): say so
        # rather than render nan speeds + the defaulted (fake) brake/throttle.
        return f"{tag} {cm.name} ({cm.direction}){sect}: no telemetry captured here"
    # None = the driver never braked here. Guard on the field, not on the sign of the
    # subtraction: the sign only stood in for "not found" while the sentinel happened
    # to sit past the apex.
    brake_txt = (
        f"brake {cm.apex_dist - cm.brake_point:.0f}m before apex @ {cm.peak_brake:.0%} peak"
        if cm.brake_point is not None else "no heavy braking"
    )
    # Same for the throttle gate: say nothing rather than turn the corner's own
    # geometry into a claimed driver input.
    thr_txt = (f", back to throttle {cm.throttle_point - cm.apex_dist:.0f}m after apex"
               if cm.throttle_point is not None else "")
    line = (
        f"{tag} {cm.name} ({cm.direction}){sect}: "
        f"{spd(cm.entry_spd):.0f}→min {spd(cm.min_spd):.0f}→{spd(cm.exit_spd):.0f} {spd_u}, "
        f"{brake_txt}{thr_txt}, trail {cm.trailbrake_overlap:.0%}, peak {cm.peak_lat_g:.1f}g"
    )
    if not math.isnan(cm.min_grip):
        line += f", min grip {cm.min_grip:.2f}"   # 1.0 = full grip; low = sliding/locking
    # Extended inputs/loads (guarded — older laps lack these channels).
    if not math.isnan(getattr(cm, "peak_brake_g", float("nan"))):
        line += f", brake {cm.peak_brake_g:.1f}g"
    if not math.isnan(getattr(cm, "peak_accel_g", float("nan"))):
        line += f", accel {cm.peak_accel_g:.1f}g"
    if not math.isnan(getattr(cm, "peak_steer", float("nan"))):
        line += f", steer {cm.peak_steer:.0%}"
    aids = []
    if not math.isnan(getattr(cm, "abs_pct", float("nan"))) and cm.abs_pct > 0:
        aids.append(f"ABS {cm.abs_pct:.0%}")
    if not math.isnan(getattr(cm, "tc_pct", float("nan"))) and cm.tc_pct > 0:
        aids.append(f"TC {cm.tc_pct:.0%}")
    if aids:
        line += ", " + " ".join(aids)
    edge_min = getattr(cm, "track_edge_min", float("nan"))
    if not math.isnan(edge_min):
        # Negative = the car went past the white line. Render that as "past edge"
        # rather than a nonsensical "-0.4m from edge". Says "centre" because the
        # measurement is the car's centre, not its outer wheels — at "centre 0.2m
        # from edge" a ~2m car already has a wheel over the line.
        if edge_min < -_TRACK_EDGE_RENDER_CAP_M:
            # Beyond the cap the magnitude isn't trustworthy (see comment above) —
            # hedge with a phrase instead of a false-precision number.
            line += ", centre well past edge"
        else:
            line += (f", centre {-edge_min:.1f}m past edge" if edge_min < 0
                     else f", centre {abs(edge_min):.1f}m from edge")
    # Double-apex: name it and give the per-apex minima so the coach advises on both.
    aps = cm.apexes or []
    if len(aps) > 1 and cm.apex_min_speeds:
        mins = "/".join(f"{spd(s):.0f}" for s in cm.apex_min_speeds)
        line += f", {len(aps)}-apex (per-apex min {mins} {spd_u})"
    if cm.min_spd_delta is not None:
        line += f" (min vs best: {spd(cm.min_spd_delta):+.1f} {spd_u})"
    return line


def format_wheel_report(analysis, units: str = "metric") -> str | None:
    """Lap-level brake temps, hot pressures, and tire wear per wheel — or None if
    the lap predates the extended capture."""
    w = analysis.wheels
    if not w:
        return None
    _, temp, _, temp_u = unit_fns(units)
    bt_m, bt_p = w["brake_temp_mean"], w["brake_temp_peak"]
    lines = [
        f"Brake temps {temp_u} (mean/peak): "
        + " / ".join(f"{k.upper()} {temp(bt_m[k]):.0f}/{temp(bt_p[k]):.0f}" for k in ("fl", "fr", "rl", "rr"))
    ]
    if "tire_press_mean" in w:
        p = w["tire_press_mean"]
        lines.append("Tire pressures kPa (mean): "
                     + " / ".join(f"{k.upper()} {p[k]:.0f}" for k in ("fl", "fr", "rl", "rr")))
    if "tire_wear_end" in w:
        wr = w["tire_wear_end"]
        lines.append("Tire wear remaining: "
                     + " / ".join(f"{k.upper()} {wr[k]:.0%}" for k in ("fl", "fr", "rl", "rr")))
    return "\n".join(lines)


def format_tire_report(analysis, units: str = "metric") -> str | None:
    """Per-wheel mean temps (imbalance) + early→late warm-up, or None if the lap
    was captured without tire channels."""
    if not analysis.tire_means:
        return None
    _, temp, _, temp_u = unit_fns(units)
    m = analysis.tire_means
    out = (
        f"Tire temps {temp_u} (mean over lap): FL {temp(m['fl']):.0f} / FR {temp(m['fr']):.0f} / "
        f"RL {temp(m['rl']):.0f} / RR {temp(m['rr']):.0f}."
    )
    if analysis.tire_warmup:
        early, late = analysis.tire_warmup
        out += (
            f"\nTire warm-up: ~{temp(early):.0f}{temp_u} early in the lap → "
            f"~{temp(late):.0f}{temp_u} late."
        )
    return out


def format_lap_facts(analysis, units: str = "metric") -> str:
    """The full per-corner table + tire report for one lap."""
    lines = ["Corner facts (authoritative, from telemetry):"]
    if analysis.has_reference:
        lines.append("Deltas below are vs the driver's best lap on this track/car.")
    lines += [format_corner_line(cm, units) for cm in analysis.corners]
    tire = format_tire_report(analysis, units)
    if tire:
        lines.append(tire)
    return "\n".join(lines)


def find_corner(analysis, corner) -> object | None:
    """Look up a corner by number (int or numeric string) or name (case-insensitive
    substring), so a tool can fetch just the one the driver asked about."""
    key = str(corner).strip().lower()
    for cm in analysis.corners:
        lbl = str(cm.label or cm.corner_no).lower()
        if key in (str(cm.corner_no), f"t{cm.corner_no}", lbl, f"t{lbl}"):
            return cm
    for cm in analysis.corners:
        if key and key in cm.name.lower():
            return cm
    return None
