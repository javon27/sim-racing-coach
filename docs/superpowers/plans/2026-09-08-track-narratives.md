# Track Narratives + Track Management View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agentic coach authored, owned per-track narrative (overview + per-corner notes), draftable in-app from geometry + guides and managed in a dedicated Track screen.

**Architecture:** Notes live in a new `notes` key on the track JSON, merged through the existing shipped-authoritative + user-overlay machinery (`load_map_data` / `_ANNOTATION_FIELDS`). `CornerMap` carries them; a new `get_track_notes` coach tool surfaces them on demand (bounded token cost). An author-time `POST /tracks/{slug}/draft-notes` endpoint drafts a narrative via the swappable `Model` — the user verifies and saves. A new `TracksScreen.svelte` hosts the existing `TrackMapEditor` plus notes editing and coverage.

**Tech Stack:** Python 3 / FastAPI / pytest (backend); Svelte + TypeScript + vitest (frontend). Model behind the `Model` protocol (`complete(system, messages) -> str`).

**Spec:** `docs/superpowers/specs/2026-09-08-track-narratives-design.md`

## Global Constraints

- **Owned-data invariant:** track knowledge is owned data — shipped-authoritative in `simcoach/tracks/`, user overlay in `<data_dir>/tracks/`. The LLM drafts notes only at author time (verified before Save); the coach never generates track character at question time, only quotes authored notes.
- **Corner keying:** per-corner notes are keyed by the corner **label** string (`Corner.label`, e.g. `"1"`, `"10-11"`, `"8A"`), the same key the map uses. Notes never redefine corner identity.
- **Two-stage / on-demand:** notes reach the coach only through the `get_track_notes` tool (never injected every turn). Raw telemetry still never reaches the model.
- **Token budget (soft):** overview ≤ ~80 words; each corner note ≤ ~40 words. Enforced in the draft prompt and shown as an editor hint — a knob, not a hard validation gate.
- **Storage reuse:** no new loader, no sibling file — extend the existing merge. Writes reuse `_write_track_json` (dev curates repo tracks dir; installed build writes app-data overlay).
- **Scope out:** automated guide fetching (manual paste only), car-knowledge `.md` migration, `pos_y` elevation in the digest, chat-screen token estimator.

---

# Phase 1 — Notes data model + coach integration

Fixes the coach immediately, lowest risk. No UI. Ships value on its own.

### Task 1: Thread `notes` through storage overlay and CornerMap

**Files:**
- Modify: `simcoach/track_lookup.py:16` (`_ANNOTATION_FIELDS`)
- Modify: `simcoach/corners.py:48-86` (`CornerMap.__init__`, `from_data`)
- Test: `tests/test_track_lookup.py`, `tests/test_corners.py`

**Interfaces:**
- Produces: `CornerMap.notes: dict` — `{"overview": str, "corners": {label: str}, "source": str}`, default `{}`. `load_map_data` merges a user `notes` overlay over the shipped `notes`.

- [ ] **Step 1: Write the failing test — notes merges as an annotation field**

Add to `tests/test_track_lookup.py`:

```python
def test_load_map_data_merges_notes_overlay(tmp_path):
    from simcoach.track_lookup import load_map_data
    bundled = tmp_path / "bundled"; user = tmp_path / "user"
    bundled.mkdir(); user.mkdir()
    (bundled / "spa.json").write_text(json.dumps({
        "path": [[0, 0]], "corners": [], "notes": {"overview": "shipped", "corners": {}}}))
    (user / "spa.json").write_text(json.dumps({
        "notes": {"overview": "edited", "corners": {"1": "brake late"}}}))
    merged = load_map_data("Spa", str(bundled), str(user))
    assert merged["path"] == [[0, 0]]            # geometry from shipped
    assert merged["notes"]["overview"] == "edited"  # notes from overlay
    assert merged["notes"]["corners"]["1"] == "brake late"
```

(Ensure `import json` is present at the top of the test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_track_lookup.py::test_load_map_data_merges_notes_overlay -v`
Expected: FAIL — `notes` not in the merged dict (not an annotation field yet).

- [ ] **Step 3: Add `notes` to the annotation fields**

In `simcoach/track_lookup.py:16`:

```python
_ANNOTATION_FIELDS = ("corners", "sections", "rotation", "notes")
```

Update the docstring/comment on `_ANNOTATION_FIELDS` and in `load_map_data` to include `notes` in the "corners/sections/rotation" enumerations.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_track_lookup.py::test_load_map_data_merges_notes_overlay -v`
Expected: PASS

- [ ] **Step 5: Write the failing test — CornerMap exposes notes**

Add to `tests/test_corners.py`:

```python
def test_corner_map_carries_notes():
    from simcoach.corners import CornerMap
    data = {
        "corners": [{"number": 1, "name": "T1", "direction": "left",
                     "apex_dist": 50.0, "start_dist": 10.0, "end_dist": 90.0}],
        "notes": {"overview": "rhythm track", "corners": {"1": "brake earlier than it looks"},
                  "source": "LMU wiki"},
    }
    cm = CornerMap.from_data(data)
    assert cm.notes["overview"] == "rhythm track"
    assert cm.notes["corners"]["1"].startswith("brake")

def test_corner_map_notes_default_empty():
    from simcoach.corners import CornerMap
    cm = CornerMap.from_data({"corners": []})
    assert cm.notes == {}
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_corners.py::test_corner_map_carries_notes tests/test_corners.py::test_corner_map_notes_default_empty -v`
Expected: FAIL — `CornerMap` has no `notes`.

- [ ] **Step 7: Add `notes` to CornerMap**

In `simcoach/corners.py`, `CornerMap.__init__` (line 51):

```python
def __init__(self, corners: list[Corner], sections: Optional[list[dict]] = None,
             notes: Optional[dict] = None):
    self.corners = corners
    self.sections = sections or []
    self.notes = notes or {}
```

In `from_data` (line 86), pass notes through:

```python
    return cls(corners, _load_sections(data), data.get("notes") or {})
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_corners.py tests/test_track_lookup.py -v`
Expected: PASS (all)

- [ ] **Step 9: Commit**

```bash
git add simcoach/track_lookup.py simcoach/corners.py tests/test_track_lookup.py tests/test_corners.py
git commit -m "feat(tracks): carry authored notes through map overlay + CornerMap"
```

---

### Task 2: `get_track_notes` tool, `get_corner` note append, agentic prompt

**Files:**
- Modify: `simcoach/coach_tools.py` (TOOLS list ~line 57; add method ~line 300; `get_corner` ~line 311; `dispatch` ~line 412)
- Modify: `simcoach/coach.py` (`AGENTIC_SYSTEM_PROMPT`)
- Test: `tests/test_coach_tools.py`

**Interfaces:**
- Consumes: `CornerMap.notes` from Task 1; `CoachContext.cmap`, `CoachContext.track`.
- Produces: `CoachContext.get_track_notes(corner: str | None = None) -> str`; tool name `"get_track_notes"` with optional `corner` arg; `get_corner` output gains a trailing authored-note line when one exists.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_coach_tools.py` (follow the file's existing `CoachContext` construction pattern — build a context with a `cmap` whose `.notes` is set):

```python
def _ctx_with_notes(notes):
    from simcoach.corners import CornerMap, Corner
    cm = CornerMap([Corner(number=1, name="Primeira", direction="left",
                           apex_dist=50.0, start_dist=10.0, end_dist=90.0, label="1")],
                   notes=notes)
    from simcoach.coach_tools import CoachContext
    return CoachContext(store=None, cmap=cm, track="Algarve", car="GT3")

def test_get_track_notes_all():
    ctx = _ctx_with_notes({"overview": "rhythm track",
                           "corners": {"1": "brake earlier than it looks"}})
    out = ctx.get_track_notes()
    assert "rhythm track" in out
    assert "brake earlier" in out
    assert "T1" in out

def test_get_track_notes_single_corner():
    ctx = _ctx_with_notes({"overview": "rhythm track",
                           "corners": {"1": "brake earlier", "2": "flat out"}})
    out = ctx.get_track_notes(corner="1")
    assert "brake earlier" in out
    assert "flat out" not in out          # other corners excluded

def test_get_track_notes_empty():
    ctx = _ctx_with_notes({})
    assert "no authored notes" in ctx.get_track_notes().lower()

def test_get_corner_appends_authored_note():
    ctx = _ctx_with_notes({"corners": {"1": "brake earlier than it looks"}})
    # get_corner needs a lap; reuse the file's existing lap-fixture helper if present.
    # If get_corner returns the no-lap message, this asserts the note-append path via
    # a direct helper instead:
    note = ctx._corner_note("1")
    assert note == "brake earlier than it looks"
    assert ctx._corner_note("99") is None
```

> Note for the implementer: `test_get_corner_appends_authored_note` targets a small helper `_corner_note(label)` (added in Step 3) so it does not require a full lap fixture. If the file already has a lap fixture, additionally assert the appended line appears in `get_corner("1")`'s output.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py -k "track_notes or authored_note" -v`
Expected: FAIL — `get_track_notes` / `_corner_note` not defined.

- [ ] **Step 3: Implement the tool + helper**

In `simcoach/coach_tools.py`, add the method near `list_track_corners` (after line 309):

```python
    def _corner_note(self, label) -> str | None:
        """Authored note for one corner label from the merged map, or None."""
        notes = getattr(self.cmap, "notes", None) or {}
        by_corner = notes.get("corners") or {}
        return by_corner.get(str(label))

    def get_track_notes(self, corner=None):
        notes = getattr(self.cmap, "notes", None) or {}
        overview = (notes.get("overview") or "").strip()
        by_corner = notes.get("corners") or {}
        if not overview and not by_corner:
            return f"No authored notes for {self.track or 'this track'} yet."
        lines = []
        if overview:
            lines.append(overview)
        if corner is not None:
            note = self._corner_note(corner)
            lines.append(f"T{corner}: {note}" if note else f"No authored note for corner {corner}.")
        else:
            for label, text in by_corner.items():
                lines.append(f"T{label}: {text}")
        head = f"Authored notes for {self.track or 'this track'}:\n" if overview or by_corner else ""
        return head + "\n".join(lines)
```

Append the authored note in `get_corner` (after `format_corner_line(cm, self.units)` at line 322): capture the formatted line, add the note if present:

```python
        line = format_corner_line(cm, self.units)
        note = self._corner_note(cm.label or cm.corner_no)
        return f"{line}\nAuthored note: {note}" if note else line
```

- [ ] **Step 4: Register the tool spec + dispatch**

Add to the `TOOLS` list in `coach_tools.py` (after the `list_track_corners` entry, ~line 65):

```python
    {
        "name": "get_track_notes",
        "description": "Authored track knowledge (character, cues, hazards) for the current "
                       "track: a short overview plus per-corner notes, as OWNED data. Call this "
                       "before describing how a track or corner drives. Pass `corner` (number or "
                       "label) to get just that corner's note. Works with NO laps captured. "
                       "Never invent track character from memory — quote these notes.",
        "input_schema": {
            "type": "object",
            "properties": {"corner": {"type": "string",
                                      "description": "Optional corner number/label to scope to."}},
        },
    },
```

Add to the `dispatch` handler dict (~line 416):

```python
            "get_track_notes": lambda: self.get_track_notes(args.get("corner")),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py -k "track_notes or authored_note" -v`
Expected: PASS

- [ ] **Step 6: Add the prompt guidance**

In `simcoach/coach.py`, extend `AGENTIC_SYSTEM_PROMPT` with a line alongside the existing corner-identity ban (near the `list_track_corners` guidance):

```
"- For a track's character, cues, or hazards — or how any corner drives — call "
"get_track_notes and quote the authored notes; NEVER invent track character from "
"memory. If no notes exist, say so and coach from telemetry. "
```

- [ ] **Step 7: Run the full coach-tools suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_coach_tools.py -v`
Expected: PASS (no regressions)

- [ ] **Step 8: Commit**

```bash
git add simcoach/coach_tools.py simcoach/coach.py tests/test_coach_tools.py
git commit -m "feat(coach): get_track_notes tool + per-corner note append + prompt"
```

---

### Task 3: Migrate `knowledge/*.md` → `notes`, retire the legacy track-note path

**Files:**
- Create: `scripts/migrate_track_notes.py`
- Modify: `simcoach/tracks/*.json` (generated output — the tracks with a matching `knowledge/<slug>.md`)
- Modify: `simcoach/coach.py:195-199` (remove the dead track-`.md` branch in `build_prompt`)
- Delete: `simcoach/knowledge/<track-slug>.md` (track files only — NOT car files)
- Test: `tests/test_migrate_track_notes.py`

**Interfaces:**
- Consumes: the `notes` schema from Task 1.
- Produces: `migrate_track_notes.parse_md(md_text: str) -> dict` returning `{"overview": str, "corners": {label: str}, "source": str}`.

- [ ] **Step 1: Write the failing test — parse a known corpus file**

Create `tests/test_migrate_track_notes.py`:

```python
from scripts.migrate_track_notes import parse_md

ALGARVE_HEAD = """# Autodromo Internacional do Algarve (Portimao) notes

4.653 km, 15 turns, WEC layout. Confidence/rhythm track.

## Corner-by-corner
- **T1 Primeira Curva:** Heavy downhill braking, brake earlier than it looks.
- **T10-11 Portimao Curve:** Double-apex right-hander, brake earlier than it looks.

## Common mistakes
Braking too late into downhill zones.

Source: LMU wiki (lemansultimate.wiki.gg), CC-BY-SA, summarized 2026-08-29.
"""

def test_parse_md_extracts_overview_corners_source():
    out = parse_md(ALGARVE_HEAD)
    assert "4.653 km" in out["overview"]
    assert out["corners"]["1"].startswith("Heavy downhill")
    assert out["corners"]["10-11"].startswith("Double-apex")
    assert "LMU wiki" in out["source"]

def test_parse_md_keys_by_label_not_name():
    out = parse_md(ALGARVE_HEAD)
    assert set(out["corners"]) == {"1", "10-11"}   # labels, not "Primeira Curva"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migrate_track_notes.py -v`
Expected: FAIL — `scripts.migrate_track_notes` does not exist.

- [ ] **Step 3: Write the migration script**

Create `scripts/migrate_track_notes.py`:

```python
"""One-time migration: fold each simcoach/knowledge/<track>.md into the matching
simcoach/tracks/<track>.json as a `notes` object (overview + per-corner keyed by
label + source). Corner bullets follow `**T<label> <Name>:** <text>`. Run once;
the generated JSON is committed. Files that don't fit the pattern are reported for
hand-finishing rather than guessed."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_CORNER_RE = re.compile(r"^\s*-\s*\*\*T([0-9]+(?:-[0-9]+)?[A-Za-z]?)\b[^:]*:\*\*\s*(.+)$")
_SOURCE_RE = re.compile(r"^Source:\s*(.+)$", re.MULTILINE)


def parse_md(md_text: str) -> dict:
    lines = md_text.splitlines()
    corners: dict[str, str] = {}
    overview_parts: list[str] = []
    in_corners = False
    cur_label: str | None = None
    for ln in lines:
        if ln.startswith("#"):                       # heading
            in_corners = ln.lower().startswith("## corner")
            cur_label = None
            continue
        m = _CORNER_RE.match(ln)
        if m:
            cur_label, text = m.group(1), m.group(2).strip()
            corners[cur_label] = text
            continue
        if in_corners and cur_label and ln.strip() and not ln.startswith("-"):
            corners[cur_label] += " " + ln.strip()   # wrapped continuation line
            continue
        if not in_corners and ln.strip() and not ln.startswith(("Source:", "`")):
            overview_parts.append(ln.strip())
    src = _SOURCE_RE.search(md_text)
    overview = " ".join(p for p in overview_parts if not p.startswith("#")).strip()
    return {"overview": overview, "corners": corners,
            "source": src.group(1).strip() if src else ""}


def migrate(knowledge_dir: Path, tracks_dir: Path) -> None:
    unmatched = []
    for md in sorted(knowledge_dir.glob("*.md")):
        tj = tracks_dir / f"{md.stem}.json"
        if not tj.exists():                          # car notes / no matching track map
            continue
        notes = parse_md(md.read_text(encoding="utf-8"))
        if not notes["corners"] and not notes["overview"]:
            unmatched.append(md.name); continue
        data = json.loads(tj.read_text(encoding="utf-8"))
        data["notes"] = notes
        tj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"migrated {md.name} -> {tj.name} ({len(notes['corners'])} corners)")
    if unmatched:
        print("HAND-FINISH (did not fit the pattern):", ", ".join(unmatched))


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    migrate(root / "simcoach" / "knowledge", root / "simcoach" / "tracks")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_migrate_track_notes.py -v`
Expected: PASS

- [ ] **Step 5: Run the migration and inspect output**

Run: `.venv/Scripts/python.exe scripts/migrate_track_notes.py`
Expected: prints one `migrated …` line per track that has both a `knowledge/*.md` and a `tracks/*.json`. Open one result (e.g. `simcoach/tracks/algarve_international_circuit.json`) and confirm the `notes` object has a sensible `overview`, per-corner entries keyed by label, and `source`. Hand-finish any file printed under "HAND-FINISH".

- [ ] **Step 6: Verify the migrated JSON still loads as a map**

Run: `.venv/Scripts/python.exe -m pytest tests/test_corners.py -v`
Expected: PASS — all shipped maps still parse (notes are additive; geometry untouched).

- [ ] **Step 7: Retire the dead legacy track-note branch + delete migrated track .md**

In `simcoach/coach.py`, remove the `if track_note:` block (lines ~195-199) that reads `knowledge/<tslug>.md` — the agentic path never used it and notes now live in the map. Leave the **car**-note load (lines ~201-207) intact. Delete only the track `.md` files that were migrated (keep `porsche_*`, `ferrari_*`, and any car files).

```bash
# after confirming each was migrated:
git rm simcoach/knowledge/algarve_international_circuit.md   # ...and the other migrated track files
```

- [ ] **Step 8: Run the full backend suite**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: PASS. If a test asserted the legacy track-`.md` load, update it to the `get_track_notes` path.

- [ ] **Step 9: Commit**

```bash
git add scripts/migrate_track_notes.py tests/test_migrate_track_notes.py simcoach/tracks/ simcoach/coach.py
git add -u simcoach/knowledge/
git commit -m "feat(tracks): migrate knowledge corpus into map notes; retire legacy track .md"
```

---

# Phase 2 — In-app draft generation

Author-time drafting from geometry + pasted guides. Depends on Phase 1's `notes` schema.

### Task 4: `POST /tracks/{slug}/draft-notes` endpoint

**Files:**
- Modify: `simcoach/server/app.py` (new route; reuse `_track_file_data`, `_map_for`, `track_slug`)
- Test: `tests/test_server_tracks.py` (new file)

**Interfaces:**
- Consumes: `coach.model.complete(system, messages) -> str`; `_track_file_data(track)`; `CornerMap` from `_map_for(track)`.
- Produces: `POST /tracks/{slug}/draft-notes` body `{"track": str, "guides": str = ""}` → `{"draft": {"overview": str, "corners": {label: str}, "source": str}}`. **Never writes to disk.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_tracks.py` (follow `tests/test_server_live.py` for app/TestClient construction, using `FakeModel`):

```python
def test_draft_notes_returns_draft_without_writing(tmp_path, monkeypatch):
    # Build an app whose coach.model is a FakeModel returning a JSON notes blob.
    # (Reuse the harness in test_server_live.py; inject a FakeModel via the coach.)
    client, store = _make_client_with_fake_model(tmp_path, model_reply=json.dumps({
        "overview": "rhythm track", "corners": {"1": "brake late"}, "source": "draft"}))
    before = set((tmp_path / "tracks").glob("*.json")) if (tmp_path / "tracks").exists() else set()
    r = client.post("/tracks/algarve_international_circuit/draft-notes",
                    json={"track": "Algarve International Circuit", "guides": "outside line into T1"})
    assert r.status_code == 200
    draft = r.json()["draft"]
    assert draft["overview"] == "rhythm track"
    assert draft["corners"]["1"] == "brake late"
    after = set((tmp_path / "tracks").glob("*.json")) if (tmp_path / "tracks").exists() else set()
    assert before == after                     # endpoint wrote nothing

def test_draft_notes_bad_slug_400(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.post("/tracks/..%2f..%2fetc/draft-notes", json={"track": "../../etc"})
    assert r.status_code in (400, 404)
```

> Implementer note: add a small `_make_client_with_fake_model` helper in this test file (or import the existing app-construction helper from `test_server_live.py` and swap in `FakeModel`). `FakeModel.complete` returns its configured reply.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_tracks.py -v`
Expected: FAIL — route not defined (404).

- [ ] **Step 3: Implement the endpoint**

In `simcoach/server/app.py`, add a route (near the other `/tracks`-style map routes). Assemble a geometry digest from the corner map + merged data, call the model, parse its JSON reply defensively:

```python
    @app.post("/tracks/{slug}/draft-notes")
    def draft_notes(slug: str, body: dict = Body(...)):
        track = body.get("track") or slug
        if track_slug(track) != slug:
            raise HTTPException(400, "slug/track mismatch")
        cmap = _map_for(track)
        if cmap is None or not cmap.corners:
            raise HTTPException(404, "no corner map for this track")
        digest_lines = []
        for c in cmap.corners:
            digest_lines.append(
                f"T{c.label or c.number} {c.name}: {c.direction}, "
                f"apex@{c.apex_dist:.0f}m, span {c.start_dist:.0f}-{c.end_dist:.0f}m")
        guides = (body.get("guides") or "").strip()
        system = (
            "You are drafting OWNED, author-time track notes a human will verify. "
            "Return STRICT JSON: {\"overview\": str, \"corners\": {label: str}, \"source\": str}. "
            "Keep the overview under 80 words and each corner note under 40 words. "
            "Ground every claim in the geometry and provided guides; do NOT invent corner "
            "names, numbers, or directions — those are given.")
        user = "Corner geometry:\n" + "\n".join(digest_lines)
        if guides:
            user += "\n\nExternal guide material (reference):\n" + guides
        raw = app.state.coach.model.complete(system, [{"role": "user", "content": user}])
        try:
            draft = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(502, "model did not return valid notes JSON")
        draft = {"overview": str(draft.get("overview", "")),
                 "corners": {str(k): str(v) for k, v in (draft.get("corners") or {}).items()},
                 "source": str(draft.get("source", ""))}
        return {"draft": draft}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_tracks.py -v`
Expected: PASS

- [ ] **Step 5: Run the server suite for regressions**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_live.py tests/test_server_tracks.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add simcoach/server/app.py tests/test_server_tracks.py
git commit -m "feat(server): author-time POST /tracks/{slug}/draft-notes (never writes)"
```

---

### Task 5: Notes save endpoint + tracks coverage listing

**Files:**
- Modify: `simcoach/server/app.py` (add `PUT /tracks/{slug}/notes`; add `GET /tracks` coverage list; reuse `_write_track_json`, `_track_file_data`, `_map_dirs`)
- Test: `tests/test_server_tracks.py`

**Interfaces:**
- Consumes: `_write_track_json(track, out)`, `_track_file_data(track)`, `track_slug`.
- Produces:
  - `PUT /tracks/{slug}/notes` body `{"track": str, "notes": {overview, corners, source}}` → writes the merged map file's `notes` (overlay in installed builds, repo in dev) and returns `{"ok": true}`.
  - `GET /tracks` → `[{"slug", "name", "has_map": bool, "has_notes": bool, "user_edited": bool}]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server_tracks.py`:

```python
def test_put_notes_persists_and_reads_back(tmp_path):
    client, store = _make_client_with_fake_model(tmp_path, model_reply="{}")
    r = client.put("/tracks/algarve_international_circuit/notes", json={
        "track": "Algarve International Circuit",
        "notes": {"overview": "rhythm", "corners": {"1": "brake late"}, "source": "me"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    # A fresh GET of the track's map data now carries the notes overlay.
    got = client.get("/tracks").json()
    row = next(t for t in got if t["slug"] == "algarve_international_circuit")
    assert row["has_notes"] is True

def test_get_tracks_lists_coverage(tmp_path):
    client, _ = _make_client_with_fake_model(tmp_path, model_reply="{}")
    rows = client.get("/tracks").json()
    assert any(t["slug"] == "algarve_international_circuit" and t["has_map"] for t in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_tracks.py -k "notes_persists or coverage" -v`
Expected: FAIL — routes not defined.

- [ ] **Step 3: Implement `PUT /tracks/{slug}/notes`**

```python
    @app.put("/tracks/{slug}/notes")
    def save_notes(slug: str, body: dict = Body(...)):
        track = body.get("track") or slug
        if track_slug(track) != slug:
            raise HTTPException(400, "slug/track mismatch")
        raw = body.get("notes") or {}
        notes = {"overview": str(raw.get("overview", "")),
                 "corners": {str(k): str(v) for k, v in (raw.get("corners") or {}).items()},
                 "source": str(raw.get("source", ""))}
        data = dict(_track_file_data(track))       # merged shipped+overlay
        if not data:
            raise HTTPException(404, "no track file to annotate")
        data["notes"] = notes
        _write_track_json(track, data)
        _invalidate_cmap(track)                    # reuse existing cache invalidation
        return {"ok": True}
```

> Implementer note: use whatever cmap-cache invalidation the existing `PUT /laps/{id}/map` handler calls (grep for the cache-clear used after a map save) in place of `_invalidate_cmap`. Match the existing pattern exactly.

- [ ] **Step 4: Implement `GET /tracks` coverage listing**

```python
    @app.get("/tracks")
    def list_tracks():
        user_dir, bundled = _map_dirs()
        slugs = set()
        for base in (bundled, user_dir):
            if Path(base).exists():
                slugs.update(p.stem for p in Path(base).glob("*.json"))
        out = []
        for slug in sorted(slugs):
            shipped = _read_map_json(Path(bundled) / f"{slug}.json")
            user = _read_map_json(Path(user_dir) / f"{slug}.json")
            merged = {**shipped, **user}
            out.append({
                "slug": slug,
                "name": merged.get("name") or slug.replace("_", " ").title(),
                "has_map": bool(merged.get("path") or merged.get("corners")),
                "has_notes": bool((merged.get("notes") or {}).get("overview")
                                  or (merged.get("notes") or {}).get("corners")),
                "user_edited": bool(user),
            })
        return out
```

> Implementer note: import `_read_map_json` from `..track_lookup` (it already exists) or reuse the module's existing reader. Keep the `name` field only if track JSONs carry one; otherwise the slug-title fallback stands alone.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_server_tracks.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add simcoach/server/app.py tests/test_server_tracks.py
git commit -m "feat(server): PUT /tracks/{slug}/notes + GET /tracks coverage list"
```

---

# Phase 3 — Track management view (UI)

New nav screen hosting the existing map editor, notes editing, draft generation, and coverage. Depends on Phases 1-2's endpoints.

### Task 6: API client methods

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Test: `frontend/src/lib/api.test.ts` (if the repo tests api.ts; otherwise covered via component tests)

**Interfaces:**
- Produces: `listTracks(): Promise<TrackRow[]>`, `draftNotes(slug, track, guides): Promise<{overview,corners,source}>`, `saveNotes(slug, track, notes): Promise<void>`; `TrackRow = {slug, name, has_map, has_notes, user_edited}`.

- [ ] **Step 1: Add the client methods**

In `frontend/src/lib/api.ts`, following the existing fetch-wrapper pattern (match how `getLapMap`/`saveLapMap` are written):

```ts
export type TrackRow = { slug: string; name: string; has_map: boolean; has_notes: boolean; user_edited: boolean };
export type TrackNotes = { overview: string; corners: Record<string, string>; source: string };

export const listTracks = (): Promise<TrackRow[]> => api("/tracks");
export const draftNotes = (slug: string, track: string, guides: string): Promise<{ draft: TrackNotes }> =>
  api(`/tracks/${slug}/draft-notes`, { method: "POST", body: JSON.stringify({ track, guides }) });
export const saveNotes = (slug: string, track: string, notes: TrackNotes): Promise<{ ok: boolean }> =>
  api(`/tracks/${slug}/notes`, { method: "PUT", body: JSON.stringify({ track, notes }) });
```

> Implementer note: use the module's existing request helper (named `api`/`req`/`json` — match the file) and its base-URL + headers convention rather than raw `fetch`.

- [ ] **Step 2: Run the frontend suite**

Run: `cd frontend && npm test`
Expected: PASS (no regressions; new exports compile).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(ui): api client for tracks list, draft-notes, save-notes"
```

---

### Task 7: `TracksScreen.svelte` — list + notes editor + draft + hosted map editor

**Files:**
- Create: `frontend/src/lib/TracksScreen.svelte`
- Test: `frontend/src/lib/TracksScreen.test.ts`

**Interfaces:**
- Consumes: `listTracks`, `draftNotes`, `saveNotes` (Task 6); hosts `TrackMapEditor.svelte` unchanged.
- Produces: a self-contained screen component with no required props.

- [ ] **Step 1: Write the failing component test**

Create `frontend/src/lib/TracksScreen.test.ts` (follow the existing `*.test.ts` pattern — vitest + `@testing-library/svelte`, mocking `./api`):

```ts
import { render, screen, fireEvent } from "@testing-library/svelte";
import { vi } from "vitest";
import TracksScreen from "./TracksScreen.svelte";
import * as api from "./api";

vi.spyOn(api, "listTracks").mockResolvedValue([
  { slug: "algarve_international_circuit", name: "Algarve", has_map: true, has_notes: false, user_edited: false },
]);

test("lists tracks with coverage badges", async () => {
  render(TracksScreen);
  expect(await screen.findByText("Algarve")).toBeInTheDocument();
  expect(screen.getByText(/no notes/i)).toBeInTheDocument();  // has_notes:false badge
});

test("generate draft fills the notes editor", async () => {
  vi.spyOn(api, "draftNotes").mockResolvedValue({
    draft: { overview: "rhythm track", corners: { "1": "brake late" }, source: "draft" } });
  render(TracksScreen);
  await fireEvent.click(await screen.findByText("Algarve"));
  await fireEvent.click(screen.getByRole("button", { name: /generate draft/i }));
  expect(await screen.findByDisplayValue(/rhythm track/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- TracksScreen`
Expected: FAIL — component does not exist.

- [ ] **Step 3: Implement the component**

Create `frontend/src/lib/TracksScreen.svelte` following the repo's Svelte 5 runes style (see `SessionsScreen.svelte` / `SettingsScreen.svelte` for structure and CSS conventions). It must:
- On mount, call `listTracks()` and render a list; each row shows name + coverage badges (`has_map`, `has_notes` → "notes"/"no notes", `user_edited` → "edited"/"shipped").
- On selecting a track: show the notes editor — `overview` textarea, a per-corner note field per corner (keyed by label), a `source` field, a live word-count hint (overview ≤80, corner ≤40), and a guides paste textarea.
- **Generate draft** button → `draftNotes(slug, name, guides)` → populate the editor fields from the returned draft.
- **Save** button → `saveNotes(slug, name, notes)` (primary-button style — reuse the app's primary button class).
- Host `<TrackMapEditor .../>` for the selected track (import and mount it as-is; pass the props it already expects).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- TracksScreen`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/TracksScreen.svelte frontend/src/lib/TracksScreen.test.ts
git commit -m "feat(ui): TracksScreen — coverage list, notes editor, draft, hosted map editor"
```

---

### Task 8: Wire the Tracks screen into the nav

**Files:**
- Modify: `frontend/src/lib/App.svelte` (nav item + `Screen` type + section)
- Test: `frontend/src/lib/App.test.ts`

**Interfaces:**
- Consumes: `TracksScreen.svelte` (Task 7).
- Produces: a `"tracks"` value on the `Screen` type and a nav button that shows the screen.

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/lib/App.test.ts` (match the file's existing render/click pattern):

```ts
test("Tracks nav shows the tracks screen", async () => {
  render(App);
  await fireEvent.click(screen.getByRole("button", { name: /tracks/i }));
  expect(await screen.findByText(/track/i)).toBeInTheDocument();  // tighten to a TracksScreen-only marker
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- App`
Expected: FAIL — no Tracks nav item.

- [ ] **Step 3: Wire the nav**

In `frontend/src/lib/App.svelte`:
- Add `"tracks"` to the `Screen` type union (line ~21).
- Import `TracksScreen` alongside the other screen imports (line ~11).
- Add a nav `<button class="nav-item" class:on={screen === "tracks"} onclick={() => (screen = "tracks")}>` with a label "Tracks" and an inline SVG icon (copy the pattern of the Sessions/Pit Wall buttons; pick a map/flag glyph).
- Add a `<section class="screen" class:on={screen === "tracks"}>{#if screen === "tracks"}<TracksScreen />{/if}</section>` in the screens block (line ~222).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- App`
Expected: PASS

- [ ] **Step 5: Run the full frontend suite**

Run: `cd frontend && npm test`
Expected: PASS

- [ ] **Step 6: Retire the map-editor popout entry point**

If the map pane's "Edit map" popout is now redundant, point it at the Tracks screen (or remove the popout button) per the design's "map pane's Edit map popout is superseded." Keep `TrackMapEditor.svelte` itself — only the entry point changes. Update any test asserting the old popout.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/App.svelte frontend/src/lib/App.test.ts
git commit -m "feat(ui): add Tracks screen to nav; route map editing through it"
```

---

### Task 9: Final verification

- [ ] **Step 1: Backend suite**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: PASS

- [ ] **Step 2: Lint**

Run: `ruff check`
Expected: clean (fix any net-new findings).

- [ ] **Step 3: Frontend suite**

Run: `cd frontend && npm test`
Expected: PASS

- [ ] **Step 4: Manual smoke (rig or dev)**

Launch the app; open Tracks; confirm the list + coverage badges render, Generate draft fills the editor, Save persists (reopen shows saved notes), and the hosted map editor still works. Ask the coach a track-character question and confirm it calls `get_track_notes` and quotes authored notes (verify in a live transcript — LLM behavior isn't unit-testable).

---

## Self-review notes

- **Spec coverage:** §1 data model → Task 1; §2 coach (tool, get_corner append, prompt, agentic wiring, migration) → Tasks 2-3; §3 draft generation → Task 4; §4 UI (list, coverage, notes editor, draft, hosted map editor, nav) → Tasks 5-8; §5 testing folded per task; §6 boundaries → Global Constraints. Token-mindfulness → Task 2 (`corner=` scoping) + Task 4/7 (budget in prompt + editor hint).
- **Deferred (not tasked, by design):** automated guide fetch, car-`.md` migration, `pos_y` elevation digest, chat token estimator.
- **Type consistency:** `notes = {overview, corners{label:str}, source}` used identically across Tasks 1, 2, 4, 5, 6, 7; `get_track_notes(corner=None)`, `_corner_note(label)`, `TrackRow`, `TrackNotes` names match across tasks.
