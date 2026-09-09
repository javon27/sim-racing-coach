<script lang="ts">
  import { listTracks, draftNotes, saveNotes, getTrackNotes, getTrackMap, type TrackRow, type TrackNotes, type TrackMap } from "./api";
  import TrackMapEditor from "./TrackMapEditor.svelte";
  import TrackMapView from "./TrackMap.svelte";

  let tracks: TrackRow[] = $state([]);
  let selected: TrackRow | null = $state(null);
  let notes: TrackNotes = $state({ overview: "", corners: {}, reference_times: {}, source: "" });
  let map: TrackMap | null = $state(null);
  let guides = $state("");
  let drafting = $state(false);
  let saving = $state(false);
  let saveStatus: string | null = $state(null);
  let mapOpen = $state(false);

  let newRefClass = $state("");
  let newRefTime = $state("");

  // The corner-notes list is DRIVEN BY the corner mapping: one row per map corner in
  // map order, keyed on the corner's label. A note may also be keyed by a hyphen range
  // ("10-11") that covers several corners -- an intended construct the coach resolves
  // the same way (coach_tools._corner_note). Such a range collapses its covered corners
  // into a single row; only a key that matches no corner and no covering range is a
  // true orphan (surfaced, never silently dropped). Corners are owned by the map (edit
  // them via the map editor), so there's no free-form add here.
  // A corner number, in a note key and in a corner label: 1-3 ASCII digits, ascending,
  // spanning at most MAX_RANGE_SPAN corners. COUNTERPART of `note_key_binds` in
  // simcoach/track_lookup.py (_MAX_CORNER_DIGITS / _NOTE_RANGE / _MAX_RANGE_SPAN),
  // which is the guard deciding which notes survive a map save. The two must stay
  // identical: a key that guard drops while this screen shows it collapsed under a
  // range row is authored prose deleted with no warning to the person who wrote it.
  const MAX_CORNER_DIGITS = 3;
  const MAX_RANGE_SPAN = 100;
  const NUM = String.raw`\d{1,${MAX_CORNER_DIGITS}}`;
  const CORNER_NUM = new RegExp(`^${NUM}$`);
  const RANGE_KEY = new RegExp(`^(${NUM})-(${NUM})$`);
  const rangeOf = (k: string): [number, number] | null => {
    const m = RANGE_KEY.exec(k);
    if (!m) return null;
    const [lo, hi] = [Number(m[1]), Number(m[2])];
    return hi < lo || hi - lo >= MAX_RANGE_SPAN ? null : [lo, hi];   // '13-11' is not one
  };
  const cornerView = $derived.by(() => {
    const keys = Object.keys(notes.corners);
    if (!map) return { rows: keys.map((k) => ({ key: k, label: k })), orphans: [] as string[] };
    const ranges = keys.map((k) => ({ key: k, r: rangeOf(k) })).filter((x) => x.r) as { key: string; r: [number, number] }[];
    const rows: { key: string; label: string }[] = [];
    const usedRange = new Set<string>();
    for (const c of map.corners) {
      const label = c.label || String(c.number);
      const n = Number(label);
      // CANONICAL decimal only. `Number()` alone coerced '07', ' 7 ', '7.0', '+7', '0x7'
      // and '1e1' to 7 and collapsed that corner under a range covering 7, while the
      // Python guard probes `str(n) in labels` and matches only '7' -- so the note showed
      // as bound here and was dropped on the next map save.
      const cover = CORNER_NUM.test(label) && String(n) === label
        ? ranges.find(({ r }) => n >= r[0] && n <= r[1]) : undefined;
      if (cover) {
        if (!usedRange.has(cover.key)) { usedRange.add(cover.key); rows.push({ key: cover.key, label: cover.key }); }
        continue; // a covered corner shows under its range row, not its own
      }
      rows.push({ key: label, label });
    }
    const valid = new Set(rows.map((row) => row.key));
    return { rows, orphans: keys.filter((k) => !valid.has(k)) };
  });
  // Both note maps are keyed on FREE TEXT: a corner label typed into the map editor,
  // and the reference-time class typed into the Add row below. 'toString' / 'valueOf'
  // resolve through Object.prototype, which broke these objects BOTH ways, so every read
  // goes through `noteOf` (never `in` + a bare index) and every write through
  // `setNote`/`setRefTime` (never `notes.corners[k] = v`):
  //   READ:  `'toString' in notes.corners` was true for a key that did not exist, so the
  //          seed below skipped it and the row read Object.prototype.toString -- a
  //          function, which `?? ""` keeps and wordCount() throws "s.trim is not a
  //          function" on, blanking the screen.
  //   WRITE: Svelte's $state proxy SILENTLY DISCARDS `obj[k] = v` for such a key -- its
  //          set trap only creates a source when the key is absent or an own writable
  //          property, and a prototype-shadowed name is neither. The authored note went
  //          nowhere at all. Replacing the object materializes a real own property.
  //   DELETE: `delete notes.corners[k]` leaves the RAW TARGET's key in place -- Svelte's
  //          deleteProperty never calls Reflect.deleteProperty, it only parks the key's
  //          source at UNINITIALIZED. Object.keys, `in`, a read and JSON.stringify all
  //          correctly report it gone; Object.hasOwn does NOT, because
  //          getOwnPropertyDescriptor hands back the raw target's descriptor. noteOf then
  //          returned undefined and the seed skipped re-seeding, so a deleted orphan note
  //          whose corner later reappeared on the map threw the same TypeError.
  // That last one is the point of this whole shape: NEITHER `in` (wrong after a
  // prototype-key write) NOR Object.hasOwn (wrong after a delete) is correct on its own.
  // One honest read primitive requires an honest target, which is what replacing the
  // object -- on set AND on delete -- is for. Do not "simplify" any of these back.
  // The class input is the WIDER hole of the two -- the corner label is narrowed to
  // exactly {toString, valueOf} by PUT /map's 8-char cap, the class input has no cap.
  const noteOf = (key: string): string =>
    Object.hasOwn(notes.corners, key) ? notes.corners[key] : "";
  const setNote = (key: string, v: string) => { notes.corners = { ...notes.corners, [key]: v }; };
  const setRefTime = (cls: string, v: string) => { notes.reference_times = { ...notes.reference_times, [cls]: v }; };
  // Seed a blank note for every per-corner row so binding + word count are safe and
  // the row shows before it's written (range rows already exist as keys).
  $effect(() => {
    for (const row of cornerView.rows) {
      if (!Object.hasOwn(notes.corners, row.key)) setNote(row.key, "");
    }
  });

  $effect(() => {
    listTracks()
      .then((rows) => (tracks = rows))
      .catch(() => (tracks = []));
  });

  async function selectTrack(t: TrackRow) {
    selected = t;
    guides = "";
    saveStatus = null;
    map = null;
    reloadMap();
    // Pre-fill from the track's existing (migrated) notes so Save can't clobber
    // them with blanks -- fall back to empty fields if the fetch fails.
    try {
      const loaded = await getTrackNotes(t.slug);
      notes.overview = loaded.overview ?? "";
      notes.corners = { ...(loaded.corners ?? {}) };
      notes.reference_times = { ...(loaded.reference_times ?? {}) };
      notes.source = loaded.source ?? "";
    } catch {
      notes.overview = "";
      notes.corners = {};
      notes.reference_times = {};
      notes.source = "";
    }
  }

  // Read-only layout for the map pane. {} (no map yet) -> null so the pane shows
  // its empty state rather than rendering a pathless map. Reused after an editor save.
  function reloadMap() {
    if (!selected) return;
    const s = selected.slug;
    getTrackMap(s)
      .then((m) => { if (selected?.slug === s) map = "path" in m && m.path.length > 0 ? m : null; })
      .catch(() => { if (selected?.slug === s) map = null; });
  }

  function wordCount(s: string): number {
    return s.trim() ? s.trim().split(/\s+/).length : 0;
  }

  function removeCorner(label: string) {
    const rest = { ...notes.corners };        // copy, drop, replace -- see the note above:
    delete rest[label];                       // `delete notes.corners[label]` leaves the
    notes.corners = rest;                     // key on the target and Object.hasOwn lies.
  }

  function addRefTime() {
    const cls = newRefClass.trim();
    if (!cls || !newRefTime.trim()) return;
    setRefTime(cls, newRefTime.trim());
    newRefClass = "";
    newRefTime = "";
  }
  function removeRefTime(cls: string) {
    const rest = { ...notes.reference_times };   // same shape as removeCorner: no visible
    delete rest[cls];                            // consequence here today, but the
    notes.reference_times = rest;                // invariant holds of the file, not of two
  }                                              // functions someone has to remember.

  async function generateDraft() {
    if (!selected) return;
    drafting = true;
    try {
      const { draft } = await draftNotes(selected.slug, selected.name, guides);
      notes.overview = draft.overview ?? "";
      notes.corners = { ...(draft.corners ?? {}) };
      // reference_times is deliberately left untouched: the draft endpoint
      // never returns it, and overwriting would wipe saved community times.
      notes.source = draft.source ?? "";
    } finally {
      drafting = false;
    }
  }

  async function save() {
    if (!selected) return;
    saving = true;
    saveStatus = null;
    try {
      await saveNotes(selected.slug, selected.name, notes);
      saveStatus = "Saved.";
    } catch {
      saveStatus = "Save failed.";
    } finally {
      saving = false;
    }
  }
</script>

<div class="page">
  <div class="list">
    <h1>Tracks</h1>
    {#if tracks.length === 0}
      <p class="empty">No tracks yet.</p>
    {:else}
      <ul>
        {#each tracks as t (t.slug)}
          <li>
            <button type="button" class="trow" class:on={selected?.slug === t.slug} onclick={() => selectTrack(t)}>
              <span class="name">{t.name}</span>
              <span class="badges">
                <span class="badge" class:good={t.has_map}>{t.has_map ? "map" : "no map"}</span>
                <span class="badge" class:good={t.has_notes}>{t.has_notes ? "notes" : "no notes"}</span>
                <span class="badge">{t.user_edited ? "edited" : "shipped"}</span>
              </span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  {#if selected}
    <div class="editor">
      <header>
        <h2>{selected.name}</h2>
      </header>

      <label class="fld">
        <span class="lbl">Overview <span class="wc">{wordCount(notes.overview)}/120 words</span></span>
        <textarea rows="3" bind:value={notes.overview}></textarea>
      </label>

      <div class="fld">
        <span class="lbl">Corner notes</span>
        {#if cornerView.rows.length === 0}
          <p class="hint">No corners on this track's map yet. Add them with <strong>Edit</strong> on the map — corner notes follow the map.</p>
        {/if}
        {#each cornerView.rows as row (row.key)}
          <div class="corner-row">
            <span class="clabel">T{row.label}</span>
            <textarea rows="2" aria-label={`corner note ${row.key}`}
              bind:value={() => noteOf(row.key), (v) => setNote(row.key, v)}></textarea>
            <span class="wc">{wordCount(noteOf(row.key))}/50</span>
          </div>
        {/each}
        {#if cornerView.orphans.length}
          <p class="hint warn">These notes match no corner (or corner range) on the map. Rename the corner in the map editor, or remove the note.</p>
          {#each cornerView.orphans as key (key)}
            <div class="corner-row">
              <span class="clabel orphan">{key}</span>
              <textarea rows="2" aria-label={`corner note ${key}`}
                bind:value={() => noteOf(key), (v) => setNote(key, v)}></textarea>
              <span class="wc">{wordCount(noteOf(key))}/50</span>
              <button type="button" class="rm" aria-label={`remove corner note ${key}`} onclick={() => removeCorner(key)}>×</button>
            </div>
          {/each}
        {/if}
      </div>

      <div class="fld">
        <span class="lbl">Reference times</span>
        {#each Object.keys(notes.reference_times) as cls (cls)}
          <div class="ref-row">
            <span class="clabel">{cls}</span>
            <input type="text" aria-label={`reference time ${cls}`}
              bind:value={() => notes.reference_times[cls], (v) => setRefTime(cls, v)} />
            <button type="button" class="rm" aria-label={`remove reference time ${cls}`} onclick={() => removeRefTime(cls)}>×</button>
          </div>
        {/each}
        <div class="addrow">
          <input type="text" placeholder="class (e.g. LMP2)" bind:value={newRefClass} aria-label="New reference-time class" />
          <input type="text" placeholder="m:ss.xxx" bind:value={newRefTime} aria-label="New reference time" />
          <button type="button" onclick={addRefTime}>Add</button>
        </div>
      </div>

      <label class="fld">
        <span class="lbl">Source</span>
        <input type="text" bind:value={notes.source} />
      </label>

      <label class="fld">
        <span class="lbl">Guides (paste for draft)</span>
        <textarea rows="4" placeholder="Paste driving guides, forum notes, etc." bind:value={guides}></textarea>
      </label>

      <div class="actions">
        <button type="button" onclick={generateDraft} disabled={drafting}>
          {drafting ? "Generating…" : "Generate draft"}
        </button>
        <button type="button" class="primary" onclick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        {#if saveStatus}<span class="status">{saveStatus}</span>{/if}
      </div>
    </div>

    <div class="mappane">
      <button type="button" class="edit-map" onclick={() => (mapOpen = true)}>
        {map ? "Edit" : "Add map"}
      </button>
      {#if map}
        <TrackMapView mapData={map} fill />
      {:else}
        <p class="nomap">No layout for this track yet.</p>
      {/if}
    </div>

    <TrackMapEditor
      lapId={null}
      slug={selected.slug}
      open={mapOpen}
      onClose={() => (mapOpen = false)}
      onSaved={() => reloadMap()}
    />
  {/if}
</div>

<style>
  .page {
    height: 100%;
    display: flex;
    min-height: 0;
  }
  .list {
    flex: 0 0 280px;
    border-right: 1px solid var(--line);
    overflow-y: auto;
    padding: 20px 14px;
  }
  .list h1 {
    font-family: var(--display);
    font-size: 16px;
    font-weight: 700;
    margin: 0 0 12px;
  }
  .list ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .trow {
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
    cursor: pointer;
    font: inherit;
    color: inherit;
    text-align: left;
  }
  .trow:hover {
    border-color: var(--accent);
  }
  .trow.on {
    background: var(--accent-soft);
    border-color: var(--accent);
  }
  .trow .name {
    font-weight: 600;
    font-size: 13px;
  }
  .badges {
    display: flex;
    gap: 5px;
    flex-wrap: wrap;
  }
  .badge {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--faint);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 1px 8px;
  }
  .badge.good {
    color: var(--good);
    border-color: var(--good);
    background: var(--good-soft);
  }
  .empty {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
  }

  .editor {
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 20px 26px 48px;
    max-width: 720px;
  }
  .mappane {
    position: relative;
    flex: 1 1 0;
    min-width: 0;
    border-left: 1px solid var(--line);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    overflow: hidden;
  }
  .edit-map {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 1;
  }
  .nomap {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
  }
  .hint {
    color: var(--faint);
    font-size: 12px;
    line-height: 1.5;
    margin: 0 0 8px;
  }
  .hint.warn {
    color: var(--bad, #e66);
    margin-top: 10px;
  }
  .clabel.orphan {
    color: var(--bad, #e66);
  }
  .editor header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }
  .editor h2 {
    margin: 0;
    font-family: var(--display);
    font-size: 20px;
    font-weight: 700;
  }
  .fld {
    display: block;
    margin-bottom: 16px;
  }
  .lbl {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .wc {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    font-weight: 400;
  }
  textarea,
  input[type="text"] {
    width: 100%;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    resize: vertical;
  }
  textarea:focus,
  input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .corner-row,
  .ref-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .corner-row textarea,
  .ref-row input {
    flex: 1 1 auto;
    min-width: 0;
  }
  .clabel {
    flex: 0 0 auto;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--faint);
    min-width: 28px;
  }
  .rm {
    flex: 0 0 auto;
    background: none;
    border: none;
    color: var(--faint);
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    padding: 0 4px;
  }
  .rm:hover {
    color: var(--bad);
  }
  .addrow {
    display: flex;
    gap: 8px;
  }
  .addrow input {
    flex: 1 1 auto;
    min-width: 0;
  }
  .addrow button {
    flex: 0 0 auto;
  }

  button {
    font-family: var(--display);
    font-weight: 600;
    font-size: 12px;
    color: var(--text);
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 13px;
    cursor: pointer;
  }
  button:hover:not(:disabled) {
    border-color: var(--faint);
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  button.primary {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  :root[data-theme="dark"] button.primary {
    color: #160f2b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) button.primary {
      color: #160f2b;
    }
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 20px;
  }
  .status {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
</style>
