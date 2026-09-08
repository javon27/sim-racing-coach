<script lang="ts">
  import { CLASS_COLOR, type FieldSnapshot, type VehicleState } from "./live";

  let { snapshot }: { snapshot: FieldSnapshot | null } = $props();

  // Auto-follow: in position mode the list scrolls to keep the player row in view
  // as positions change, UNLESS the user scrolls it away. Auto-follow SUSPENDS while
  // the user is actively scrolling and only re-decides once they stop (debounced) —
  // so it never yanks the view back mid-scroll. Re-arms if they left the player in
  // view, stays off if they scrolled the player out.
  let rowsEl: HTMLElement | undefined = $state();
  let playerEl: HTMLElement | undefined = $state();
  let follow = $state(true);
  let userScrolling = false; // read fresh by the effect each snapshot
  let ignoreNextScroll = false; // suppress the scroll event from our own centering
  let scrollTimer: ReturnType<typeof setTimeout> | undefined;

  function playerInView(): boolean {
    if (!rowsEl || !playerEl) return false;
    const top = playerEl.offsetTop - rowsEl.scrollTop;
    return top >= 0 && top + playerEl.offsetHeight <= rowsEl.clientHeight;
  }
  function centerPlayer() {
    if (!rowsEl || !playerEl) return;
    const target = playerEl.offsetTop - rowsEl.clientHeight / 2 + playerEl.offsetHeight / 2;
    if (Math.abs(rowsEl.scrollTop - target) < 1) return; // already centered: no scroll event
    ignoreNextScroll = true;
    rowsEl.scrollTop = target;
  }
  function onScroll() {
    if (ignoreNextScroll) { ignoreNextScroll = false; return; } // our own centering
    userScrolling = true; // suspend follow until scrolling settles
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      userScrolling = false;
      follow = playerInView(); // re-arm only if the player is still in view
      if (follow) centerPlayer();
    }, 500);
  }
  function trackPlayer(node: HTMLElement, isPlayer: boolean) {
    if (isPlayer) playerEl = node;
    return { update: (p: boolean) => { if (p) playerEl = node; } };
  }

  // Two independent toggles: view (Position vs Relative) and, within Position,
  // scope (Class vs Overall — position + delta relative to the class or the field).
  const KEY = "pitwall.tower"; // legacy single key, migrated below
  const K_VIEW = "pitwall.tower.view";
  const K_SCOPE = "pitwall.tower.scope";
  let view: "position" | "relative" = $state("position");
  let scope: "class" | "overall" = $state("class");
  try {
    const legacy = localStorage.getItem(KEY);
    const v = localStorage.getItem(K_VIEW);
    const s = localStorage.getItem(K_SCOPE);
    if (v === "relative" || legacy === "relative" || legacy === "deltas") view = "relative";
    if (s === "overall" || legacy === "overall") scope = "overall";
  } catch { /* private mode */ }

  // Internal 3-way used by the row logic below.
  const mode = $derived(view === "relative" ? "relative" : scope);

  function persist() {
    try {
      localStorage.setItem(K_VIEW, view);
      localStorage.setItem(K_SCOPE, scope);
    } catch { /* private mode */ }
  }
  function toggleView() { view = view === "position" ? "relative" : "position"; persist(); }
  function toggleScope() { scope = scope === "class" ? "overall" : "class"; persist(); }

  const byPlace = $derived(
    snapshot
      ? [...snapshot.cars].sort((a, b) => {
          const rank = (p: number) => (p > 0 ? p : Number.POSITIVE_INFINITY);
          return rank(a.place) - rank(b.place);
        })
      : [],
  );

  // Relative time to the player for EVERY car: signed seconds (ahead negative,
  // behind positive), estimated from the shortest track-distance gap / speed — an
  // approximation, exact only at steady pace. Both the Position and Relative views
  // show this same number; they differ only in ordering. ponytail: per-car speed
  // with a 3 m/s floor; refine with a closing-speed model if it reads jittery.
  const relTime = $derived.by(() => {
    const m = new Map<number, number>();
    const L = snapshot?.session.track_len ?? 0;
    const me = byPlace.find((c) => c.is_player);
    if (!me || L <= 0) return m;
    for (const c of byPlace) {
      const d = ((c.lap_dist - me.lap_dist) % L + L * 1.5) % L - L / 2; // >0 = ahead
      m.set(c.id, -d / Math.max(c.speed, 3));
    }
    return m;
  });

  // Relative view: the field ordered by physical proximity to the player, windowed
  // to a few cars each side. (Position views keep race-position order — byPlace.)
  const relRows = $derived.by(() => {
    if (relTime.size === 0) return byPlace;
    const sorted = [...byPlace].sort((a, b) => (relTime.get(a.id) ?? 0) - (relTime.get(b.id) ?? 0));
    const i = sorted.findIndex((c) => c.is_player);
    return i < 0 ? sorted : sorted.slice(Math.max(0, i - 3), i + 4);
  });

  const rows = $derived(mode === "relative" ? relRows : byPlace);

  // Center the player row on each update while armed (position views only; relative
  // mode already keeps the player windowed in the middle).
  $effect(() => {
    void rows; // re-run when positions change
    if (mode === "relative" || userScrolling || !follow) return;
    centerPlayer();
  });

  // Class scope shows the class position number; overall shows the field position.
  const posOf = (c: VehicleState) => (mode === "class" ? c.class_position : c.place);

  // Fastest lap per class -> the holder's id + time. Shown on that driver's row.
  const classBest = $derived.by(() => {
    const best = new Map<string, { id: number; t: number }>();
    for (const c of snapshot?.cars ?? []) {
      if (c.best_lap == null) continue;
      const cur = best.get(c.cls);
      if (!cur || c.best_lap < cur.t) best.set(c.cls, { id: c.id, t: c.best_lap });
    }
    return best;
  });
  function classBestFor(c: VehicleState): number | null {
    const b = classBest.get(c.cls);
    return b && b.id === c.id ? b.t : null;
  }
  function fmtLap(seconds: number): string {
    const ms = Math.round(seconds * 1000);
    const m = Math.floor(ms / 60000);
    return `${m}:${((ms - m * 60000) / 1000).toFixed(3).padStart(6, "0")}`;
  }

  // Per-car lap-time cell: normally the car's fastest lap (purple if it's the class
  // fastest), but for a few seconds after it crosses the line, its just-completed
  // lap time instead. Flash windows tracked by watching each car's last_lap change.
  const FLASH_MS = 6000;
  const flashUntil = new Map<number, number>(); // non-reactive; `now` drives re-render
  const prevLast = new Map<number, number>();
  let now = $state(Date.now());
  $effect(() => {
    const t = Date.now();
    for (const c of snapshot?.cars ?? []) {
      if (c.last_lap == null) continue;
      const prev = prevLast.get(c.id);
      if (prev !== undefined && c.last_lap !== prev) flashUntil.set(c.id, t + FLASH_MS);
      prevLast.set(c.id, c.last_lap);
    }
    now = t; // snapshots (~10Hz) also expire stale flashes
  });
  function lapCell(c: VehicleState): { t: number; kind: "flash" | "cbest" | "best" } | null {
    if (c.last_lap != null && (flashUntil.get(c.id) ?? 0) > now) return { t: c.last_lap, kind: "flash" };
    if (c.best_lap == null) return null;
    return { t: c.best_lap, kind: classBestFor(c) != null ? "cbest" : "best" };
  }

  // Relative-to-player time for all views (Position and Relative alike).
  function gapText(c: VehicleState): string {
    const t = relTime.get(c.id);
    const g = t == null || c.is_player ? "—" : `${t >= 0 ? "+" : ""}${t.toFixed(1)}`;
    return c.in_pits ? `PIT ${g}` : g;
  }
</script>

<div class="tower">
  <div class="tower-h">
    <span>Live timing</span>
    <div class="toggle">
      {#if view === "position"}
        <button onclick={toggleScope} title="Switch class / overall">
          <span class="swap" aria-hidden="true">⇄</span>{scope === "class" ? "Class" : "Overall"}
        </button>
      {/if}
      <button onclick={toggleView} title="Switch position / relative">
        <span class="swap" aria-hidden="true">⇄</span>{view === "position" ? "Position" : "Relative"}
      </button>
    </div>
  </div>
  <div class="rows" bind:this={rowsEl} onscroll={onScroll}>
  {#each rows as c (c.id)}
    {@const lt = lapCell(c)}
    <div
      class="trow"
      class:you={c.is_player}
      use:trackPlayer={c.is_player}
      style="--cls:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}"
    >
      <div class="pos">{posOf(c)}</div>
      <div class="drv">
        {c.is_player ? "You" : c.driver}
        <span
          class="chip"
          style="color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'};border-color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}"
          >{c.cls}</span
        >
        <small>{c.car}</small>
      </div>
      <div class="gaps">
        {#if lt}<span class="lap {lt.kind}" title={lt.kind === "flash" ? "Last lap" : lt.kind === "cbest" ? "Class fastest lap" : "Fastest lap"}>{fmtLap(lt.t)}</span>{/if}
        <span class="gap">{gapText(c)}</span>
      </div>
    </div>
  {/each}
  </div>
</div>

<style>
  .tower {
    border-left: 1px solid var(--line);
    background: var(--surface);
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  /* Header + mode toggle stay pinned; only the rows scroll. */
  .tower-h {
    flex-shrink: 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 12px 14px 10px;
    border-bottom: 1px solid var(--line);
    font-family: var(--display, inherit);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--faint);
  }
  .rows {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
  .toggle {
    display: flex;
    gap: 4px;
  }
  /* Each button shows the CURRENT setting and switches it on click; the ⇄ glyph
     signals it's a toggle, not a static label. */
  .toggle button {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 3px 8px;
    border-radius: 6px;
    border: 1px solid var(--accent);
    background: var(--accent-soft);
    color: var(--accent);
    cursor: pointer;
  }
  .toggle button:hover { background: var(--accent); color: var(--bg); }
  .toggle .swap { font-size: 11px; opacity: 0.8; }
  .trow {
    display: grid;
    grid-template-columns: 24px 1fr auto;
    gap: 10px;
    align-items: center;
    padding: 9px 14px 9px 11px;
    border-top: 1px solid var(--line);
    border-left: 3px solid var(--cls, transparent);
  }
  .trow.you {
    background: var(--accent-soft);
  }
  .pos {
    font-weight: 700;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
  .drv {
    font-weight: 600;
    font-size: 13px;
  }
  .drv small {
    display: block;
    color: var(--muted);
    font-size: 11px;
    font-weight: 500;
  }
  .chip {
    font-size: 8.5px;
    font-weight: 700;
    text-transform: uppercase;
    border: 1px solid;
    border-radius: 4px;
    padding: 1px 4px;
    margin-left: 6px;
  }
  .gaps {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 1px;
  }
  .lap {
    font-size: 10px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .lap.cbest { color: var(--purple, #a855f7); } /* class fastest */
  .lap.flash { color: var(--good, #2ecc71); }   /* just crossed the line */
  .gap {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
</style>
