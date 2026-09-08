"""Load authored per-car knowledge (handling, setup concepts, driver aids) and the
shared LMU setup-concepts doc for the agentic coach. Mirrors `track_lookup.load_map_data`:
car notes are shipped-authoritative in `simcoach/cars/<slug>.json` with an optional user
overlay layered on top; `<slug>` is a deterministic slug of the resolved car model name
(the SAME slug the retired legacy loader used for `knowledge/<slug>.md`). Slug-built
paths only — a crafted car name can't escape the dir."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CARS_DIR = Path(__file__).parent / "cars"
# The shared LMU setup-concepts doc, leading '_' so it never collides with a car slug.
_CONCEPTS_STEM = "_lmu_setup_concepts"


def car_slug(car_name: str) -> str:
    """Deterministic filename stem for a car model name — lowercase, non-alphanumerics
    to '_'. Same slug the legacy knowledge loader used, so the mapping is unchanged."""
    return re.sub(r"[^a-z0-9]+", "_", (car_name or "").lower()).strip("_")


def _read_notes(p: Path) -> dict:
    """The `notes` object from a car-notes JSON file, or {} if absent/unreadable."""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    notes = d.get("notes") if isinstance(d, dict) else None
    return notes if isinstance(notes, dict) else {}


def load_car_notes(car_name: str, cars_dir: str | Path | None = None,
                   user_dir: str | Path | None = None) -> dict:
    """Merged authored `notes` for a car model, or {} if we have none.

    Shipped notes in `cars_dir` (defaults to the packaged `cars/`) are the base; a
    user overlay's `notes` in `user_dir` is layered on top key-by-key so an override
    wins per field. Slug-built paths only — no traversal."""
    slug = car_slug(car_name)
    if not slug or slug == _CONCEPTS_STEM:
        return {}
    base = Path(cars_dir) if cars_dir is not None else CARS_DIR
    bundled = _read_notes(base / f"{slug}.json")
    user = _read_notes(Path(user_dir) / f"{slug}.json") if user_dir else {}
    if not user:
        return bundled
    return {**bundled, **user}


@lru_cache(maxsize=1)
def _load_concepts(cars_dir: str) -> dict:
    try:
        d = json.loads((Path(cars_dir) / f"{_CONCEPTS_STEM}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def load_setup_concepts(cars_dir: str | Path | None = None) -> dict:
    """The shared LMU setup-concepts doc (applies to any car), or {} if missing.
    Cached — it's a fixed shipped file."""
    base = str(cars_dir) if cars_dir is not None else str(CARS_DIR)
    return _load_concepts(base)
