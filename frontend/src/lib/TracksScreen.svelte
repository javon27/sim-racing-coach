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

  let newCornerLabel = $state("");
  let newRefClass = $state("");
  let newRefTime = $state("");

  $effect(() => {
    listTracks()
      .then((rows) => (tracks = rows))
      .catch(() => (tracks = []));
  });

  async function selectTrack(t: TrackRow) {
    selected = t;
    guides = "";
    saveStatus = null;
    // Read-only layout for the map pane. {} (no map yet) -> null so the pane shows
    // its empty state rather than rendering a pathless map.
    map = null;
    getTrackMap(t.slug)
      .then((m) => (map = "path" in m && m.path.length > 0 ? m : null))
      .catch(() => (map = null));
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

  function wordCount(s: string): number {
    return s.trim() ? s.trim().split(/\s+/).length : 0;
  }

  function addCorner() {
    const label = newCornerLabel.trim();
    if (!label || label in notes.corners) return;
    notes.corners[label] = "";
    newCornerLabel = "";
  }
  function removeCorner(label: string) {
    delete notes.corners[label];
  }

  function addRefTime() {
    const cls = newRefClass.trim();
    if (!cls || !newRefTime.trim()) return;
    notes.reference_times[cls] = newRefTime.trim();
    newRefClass = "";
    newRefTime = "";
  }
  function removeRefTime(cls: string) {
    delete notes.reference_times[cls];
  }

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
        <button type="button" onclick={() => (mapOpen = true)}>Map</button>
      </header>

      <label class="fld">
        <span class="lbl">Overview <span class="wc">{wordCount(notes.overview)}/80 words</span></span>
        <textarea rows="3" bind:value={notes.overview}></textarea>
      </label>

      <div class="fld">
        <span class="lbl">Corner notes</span>
        {#each Object.keys(notes.corners) as label (label)}
          <div class="corner-row">
            <span class="clabel">{label}</span>
            <textarea rows="2" bind:value={notes.corners[label]} aria-label={`corner note ${label}`}></textarea>
            <span class="wc">{wordCount(notes.corners[label])}/40</span>
            <button type="button" class="rm" aria-label={`remove corner note ${label}`} onclick={() => removeCorner(label)}>×</button>
          </div>
        {/each}
        <div class="addrow">
          <input type="text" placeholder="corner label (e.g. T1)" bind:value={newCornerLabel} aria-label="New corner label" />
          <button type="button" onclick={addCorner}>Add corner</button>
        </div>
      </div>

      <div class="fld">
        <span class="lbl">Reference times</span>
        {#each Object.keys(notes.reference_times) as cls (cls)}
          <div class="ref-row">
            <span class="clabel">{cls}</span>
            <input type="text" bind:value={notes.reference_times[cls]} aria-label={`reference time ${cls}`} />
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
      {#if map}
        <TrackMapView mapData={map} fill />
      {:else}
        <p class="nomap">No layout for this track yet.</p>
      {/if}
    </div>

    <TrackMapEditor lapId={null} open={mapOpen} onClose={() => (mapOpen = false)} />
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
    flex: 1 1 0;
    min-width: 0;
    border-left: 1px solid var(--line);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    overflow: hidden;
  }
  .nomap {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
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
