<script lang="ts">
  import { getLapMap, type TrackMap } from "./api";
  import { nearestDistIndex } from "./TrackMapEditor.svelte";

  let { lapId }: { lapId: number | null } = $props();

  let map: TrackMap | null = $state(null);
  let phase: "empty" | "loading" | "none" = $state("empty");

  $effect(() => {
    const id = lapId;
    if (id === null) {
      phase = "empty";
      map = null;
      return;
    }
    phase = "loading";
    getLapMap(id)
      .then((res) => {
        if (id !== lapId) return; // stale response, a newer lap was selected meanwhile
        if ("path" in res && res.path.length > 0) {
          map = res;
          phase = "loading"; // rendered via map presence below
        } else {
          map = null;
          phase = "none";
        }
      })
      .catch(() => {
        if (id !== lapId) return;
        map = null;
        phase = "none";
      });
  });

  const points = $derived(map ? map.path.map(([x, y]) => `${x},${y}`).join(" ") : "");

  // Markers/labels sized in world units so they read the same on any track,
  // whatever its physical extent (build_track_map scales the wider axis to width).
  const u = $derived(map ? Math.max(map.width, map.height) : 100);
  // Breathing room so the stroke + edge labels (S/F text, corner labels) aren't clipped.
  const pad = $derived(u * 0.09);

  // Start/finish is the first path sample (lap_distance ~0); heading a few % in
  // gives the driving direction for the arrow.
  const startDir = $derived.by(() => {
    if (!map || map.path.length < 2) return null;
    const [x0, y0] = map.path[0];
    const j = Math.min(map.path.length - 1, Math.max(1, Math.round(map.path.length * 0.04)));
    const [xj, yj] = map.path[j];
    return { x0, y0, xj, yj, ang: Math.atan2(yj - y0, xj - x0) };
  });

  // Perpendicular tick across the racing line = the start/finish line.
  function sfBar(x: number, y: number, ang: number, h: number) {
    const px = -Math.sin(ang), py = Math.cos(ang);
    return { x1: x + px * h, y1: y + py * h, x2: x - px * h, y2: y - py * h };
  }
  // Triangle pointing along `ang`, tip ahead of (x,y).
  function arrow(x: number, y: number, ang: number, len: number, wid: number) {
    const dx = Math.cos(ang), dy = Math.sin(ang), px = -dy, py = dx;
    return `${x + dx * len},${y + dy * len} ${x + px * wid},${y + py * wid} ${x - px * wid},${y - py * wid}`;
  }

  // Place a label OUTWARD from the track centroid so labels sit off the track and
  // cluster less than a fixed offset (mirrors TrackMapEditor.lblPos).
  function lblPos(x: number, y: number, gap: number): { x: number; y: number; anchor: "start" | "middle" | "end" } {
    if (!map) return { x: x + gap, y: y - gap, anchor: "start" };
    let dx = x - map.width / 2;
    let dy = y - map.height / 2;
    const d = Math.hypot(dx, dy) || 1;
    dx /= d;
    dy /= d;
    return { x: x + dx * gap, y: y + dy * gap + gap * 0.28, anchor: dx > 0.3 ? "start" : dx < -0.3 ? "end" : "middle" };
  }

  // Path slice + midpoint for a named section span (start_dist..end_dist).
  function sectionSeg(s: { start_dist: number; end_dist: number }) {
    if (!map) return { pts: "", mid: [0, 0] as [number, number] };
    const a = nearestDistIndex(map.dist, s.start_dist);
    const b = nearestDistIndex(map.dist, s.end_dist);
    const seg = map.path.slice(Math.min(a, b), Math.max(a, b) + 1);
    const mid = seg.length ? seg[Math.floor(seg.length / 2)] : map.path[a];
    return { pts: seg.map(([x, y]) => `${x},${y}`).join(" "), mid };
  }
</script>

<div class="panel">
  {#if lapId === null}
    <p class="empty">Select a lap to see the track map</p>
  {:else if map}
    <svg viewBox="{-pad} {-pad} {map.width + pad * 2} {map.height + pad * 2}" style="width:100%;height:auto">
      <polyline
        {points}
        fill="none"
        stroke="var(--muted)"
        stroke-width={u * 0.007}
        stroke-linejoin="round"
        stroke-linecap="round"
      />
      {#if startDir}
        {@const b = sfBar(startDir.x0, startDir.y0, startDir.ang, u * 0.03)}
        <line x1={b.x1} y1={b.y1} x2={b.x2} y2={b.y2} stroke="var(--text)" stroke-width={u * 0.008} />
        <polygon points={arrow(startDir.xj, startDir.yj, startDir.ang, u * 0.05, u * 0.028)} fill="var(--text)" />
        <text class="sf" x={startDir.x0} y={startDir.y0 - u * 0.04} font-size={u * 0.032} fill="var(--text)" text-anchor="middle">S/F</text>
      {/if}
      {#each map.sections ?? [] as sec (sec.name + sec.start_dist)}
        {@const seg = sectionSeg(sec)}
        <polyline points={seg.pts} fill="none" stroke="var(--good, #3a7)" stroke-width={u * 0.02} stroke-linecap="round" opacity="0.35" />
        {@const sl = lblPos(seg.mid[0], seg.mid[1], u * 0.055)}
        <text class="section-lbl" x={sl.x} y={sl.y} text-anchor={sl.anchor} font-size={u * 0.026} fill="var(--good, #3a7)" paint-order="stroke" stroke="var(--surface, #1a1a1a)" stroke-width={u * 0.012} stroke-linejoin="round">{sec.name}</text>
      {/each}
      {#each map.corners as c (c.number)}
        {#each c.apexes ?? [c.apex_dist] as ad, ai}
          {@const p = ai === 0 ? [c.x, c.y] : map.path[nearestDistIndex(map.dist, ad)]}
          <circle cx={p[0]} cy={p[1]} r={u * 0.011} fill="var(--accent)"><title>{c.name}</title></circle>
        {/each}
        {@const cl = lblPos(c.x, c.y, u * 0.05)}
        <text class="corner-lbl" x={cl.x} y={cl.y} text-anchor={cl.anchor} font-size={u * 0.03} fill="var(--accent)" paint-order="stroke" stroke="var(--surface, #1a1a1a)" stroke-width={u * 0.012} stroke-linejoin="round">T{c.label || c.number}</text>
      {/each}
    </svg>
  {:else if phase === "none"}
    <p class="empty">No map for this track yet. Drive a lap with position capture, then build it in the map editor.</p>
  {:else}
    <p class="empty">Loading…</p>
  {/if}
</div>

<style>
  .panel {
    padding: 12px;
  }
  .empty {
    color: var(--faint);
    font-size: 13px;
    text-align: center;
    padding: 24px 12px;
  }
  /* Labels are decoration, not selectable text — stops the I-beam cursor and
     accidental text selection over the map. Names surface via the dot's tooltip. */
  svg text {
    user-select: none;
    -webkit-user-select: none;
    pointer-events: none;
    cursor: default;
    font-family: var(--mono, monospace);
    font-weight: 600;
  }
</style>
