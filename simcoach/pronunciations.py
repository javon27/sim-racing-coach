"""Per-track pronunciation table (Kokoro path, #104), plus a global lexicon (#118/#123).

A track file may carry `"pronunciations": {"Pouhon": "pˈaʊhɑːn"}` -- phonemes in
Kokoro's own vocabulary, harvested once by scripts/harvest_pronunciations.py. `apply`
rewrites each name in the spoken text to misaki's inline markup so G2P copies the
phonemes verbatim instead of guessing (or crashing) on a proper noun it doesn't know.

`simcoach/lexicon.json` is the same shape, shipped once for the whole app (LMU/WEC
vocabulary that isn't tied to one track). `table_for` merges it under the track's own
block, so a track can still override a global entry.

EVERY shipped track's block is also folded into that global table (#149). The coach
talks about a track it isn't currently on all the time -- comparing circuits, answering
before a lap exists, or with LMU closed entirely -- and a name that is only in its own
track's block is out-of-vocabulary in all of those cases, which sends the sentence to
Windows speech mid-answer and makes the voice alternate. There are no cross-track
collisions among the 56 shipped names; `Turn N` entries are skipped (Kokoro says those
unaided, and they are 133 of the entries).
"""
from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

_MARKUP = re.compile(r"\[[^\]]*\]\(/[^)]*/\)")
_LEXICON_PATH = Path(__file__).parent / "lexicon.json"
_TURN_N = re.compile(r"Turn \d+", re.IGNORECASE)


def validated_pronunciations(value) -> dict[str, str]:
    """The block as {name: phonemes}, or TypeError/ValueError naming what's wrong.
    Same contract as corners.validated_sectors: refuses, never repairs."""
    if not isinstance(value, dict):
        raise TypeError("pronunciations must be an object of name -> phonemes")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("pronunciation names must be non-empty strings")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"pronunciation for {k!r} must be a non-empty string")
        out[k.strip()] = v.strip()
    return out


@functools.lru_cache(maxsize=1)
def _shipped_track_names() -> dict[str, str]:
    """Every shipped track file's `pronunciations`, merged (#149). A bad file is
    skipped with a warning -- one unparseable track must not cost every other track
    its names. `Turn N` is skipped: Kokoro says it unaided and it is most of the
    entries by count."""
    out: dict[str, str] = {}
    log = logging.getLogger(__name__)
    for path in sorted((Path(__file__).parent / "tracks").glob("*.json")):
        try:
            block = json.loads(path.read_text(encoding="utf-8")).get("pronunciations")
            if block is None:
                continue
            for name, phonemes in validated_pronunciations(block).items():
                if not _TURN_N.fullmatch(name):
                    out.setdefault(name, phonemes)
        except (OSError, ValueError, TypeError) as e:
            log.warning("ignoring pronunciations in %s: %s", path.name, e)
    return out


@functools.lru_cache(maxsize=1)
def _global_table() -> dict[str, str]:
    """Every shipped track's names, with simcoach/lexicon.json layered on top -- the
    hand-curated global file wins a name both carry. {} on a missing or invalid
    lexicon (logged, never raised -- same contract as table_for)."""
    table = dict(_shipped_track_names())
    try:
        raw = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
        table.update(validated_pronunciations(raw))
    except (OSError, ValueError, TypeError) as e:
        logging.getLogger(__name__).warning("ignoring lexicon.json: %s", e)
    return table


def table_for(track_data: dict) -> dict[str, str]:
    """The global lexicon merged under the track's own table -- a track block wins on
    a name both carry. Call with {} when no track is resolved: the global lexicon still
    applies (#118 -- the coach can talk about a track before a lap exists)."""
    block = track_data.get("pronunciations") if isinstance(track_data, dict) else None
    track_table = {}
    if block is not None:
        try:
            track_table = validated_pronunciations(block)
        except (TypeError, ValueError) as e:
            logging.getLogger(__name__).warning("ignoring pronunciations block: %s", e)
    return {**_global_table(), **track_table}


def apply(text: str, table: dict[str, str]) -> str:
    """Rewrite every whole-word, case-insensitive match of a table key (longest key
    first) to `[match](/phonemes/)`; spans already in markup are left untouched."""
    if not table:
        return text
    keys = sorted(table, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w\]])",
                         re.IGNORECASE)
    lookup = {k.lower(): v for k, v in table.items()}
    out, pos = [], 0
    for m in _MARKUP.finditer(text):           # protect existing markup spans
        out.append(pattern.sub(lambda mm: f"[{mm.group(1)}](/{lookup[mm.group(1).lower()]}/)", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(pattern.sub(lambda mm: f"[{mm.group(1)}](/{lookup[mm.group(1).lower()]}/)", text[pos:]))
    return "".join(out)
