<script lang="ts">
  import { getLapMap, type TrackMap } from "./api";
  import { nearestDistIndex, nearestPathIndex, layoutLabels, localOutward, type LabelItem, type PlacedLabel } from "./TrackMapEditor.svelte";
  import { CLASS_COLOR } from "./live";
  import { attachWheelZoom, attachPan, type Box } from "./mapzoom";

  let { lapId = null, cars = [], mapData, fill = false }: {
    lapId?: number | null;
    cars?: { id: number; lapDist: number; cls: string; isPlayer: boolean; pos?: number }[];
    // Pit Wall passes the current-track map directly (no lap to route through);
    // when set (incl. null), it drives the pane and the lapId fetch is bypassed.
    mapData?: TrackMap | null;
    // fill: the SVG fills the container's full height (Pit Wall) instead of sizing
    // to its aspect ratio (the Coach rail, where it flows in a column).
    fill?: boolean;
  } = $props();

  let map: TrackMap | null = $state(null);
  let phase: "empty" | "loading" | "none" = $state("empty");
  const propMode = $derived(mapData !== undefined);

  $effect(() => {
    if (mapData !== undefined) {
      map = mapData;
      phase = mapData ? "loading" : "none";
      return;
    }
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

  // Scroll-wheel zoom: viewBox shrinks toward the cursor; resets when the map changes.
  let svgEl: SVGSVGElement | undefined = $state();
  let view: Box | null = $state(null);
  const fullBox = $derived(map ? { x: -pad, y: -pad, w: map.width + pad * 2, h: map.height + pad * 2 } : null);
  const vb = $derived.by(() => {
    const b = view ?? fullBox;
    return b ? `${b.x} ${b.y} ${b.w} ${b.h}` : "0 0 100 100";
  });
  // Element sizes scale by the zoom factor so markers/labels keep a ~constant
  // on-screen size instead of ballooning as you zoom in.
  const z = $derived(fullBox && view ? view.w / fullBox.w : 1);
  const suLive = $derived(u * z);
  // Debounce the size scale: the viewBox (vb) keeps zooming live, but the
  // expensive label re-layout (laidOut) only re-runs 250ms after scrolling stops,
  // not on every wheel tick. Elements momentarily grow while scrolling, then snap
  // back to constant on-screen size.
  let suDeb = $state(0);
  const su = $derived(suDeb || suLive);
  $effect(() => { void map; view = null; suDeb = u; }); // new map -> reset zoom + scale
  $effect(() => {
    const target = suLive;
    const id = setTimeout(() => (suDeb = target), 250);
    return () => clearTimeout(id);
  });
  $effect(() => {
    if (!svgEl || !fullBox) return;
    const box = () => ({ view: view ?? fullBox, full: fullBox });
    const set = (v: Box) => (view = v);
    const offWheel = attachWheelZoom(svgEl, box, set);
    const offPan = attachPan(svgEl, box, set, () => true); // read-only: pan from anywhere
    return () => { offWheel(); offPan(); };
  });

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

  // Path slice + midpoint for a named section span (start_dist..end_dist).
  function sectionSeg(s: { start_dist: number; end_dist: number }) {
    if (!map) return { pts: "", mid: [0, 0] as [number, number] };
    const a = nearestDistIndex(map.dist, s.start_dist);
    const b = nearestDistIndex(map.dist, s.end_dist);
    const seg = map.path.slice(Math.min(a, b), Math.max(a, b) + 1);
    const mid = seg.length ? seg[Math.floor(seg.length / 2)] : map.path[a];
    return { pts: seg.map(([x, y]) => `${x},${y}`).join(" "), mid };
  }

  // Ring-placed corner/section labels (shared with the editor). Corners sit on their
  // perpendicular a few px off the dot; sections hug their dot too, routing around the
  // corner labels (one combined layout), and wrap long names. Results split for the template.
  const laidOut = $derived.by(() => {
    const empty = { corners: [] as PlacedLabel[], sections: [] as PlacedLabel[] };
    if (!map || !map.path.length) return empty;
    const path = map.path, N = path.length, k = Math.max(2, Math.round(N * 0.02));
    const cx = map.width / 2, cy = map.height / 2, gap = su * 0.008;
    const secs = map.sections ?? [];
    const centroidOut = (p: [number, number]): [number, number] => { let dx = p[0] - cx, dy = p[1] - cy; const d = Math.hypot(dx, dy) || 1; return [dx / d, dy / d]; };
    const cornerAnchors = map.corners.map((c) => [c.x, c.y] as [number, number]);
    const sectionAnchors = secs.map((s) => sectionSeg(s).mid);
    const markers: [number, number][] = [...cornerAnchors, ...sectionAnchors];
    const items: LabelItem[] = [
      ...map.corners.map((c, i) => {
        const lo = localOutward(path, nearestPathIndex(path, c.x, c.y), k);
        const outward = lo.curv > 1e-4 ? [lo.ox, lo.oy] as [number, number] : centroidOut(cornerAnchors[i]);
        return { anchor: cornerAnchors[i], text: `T${c.label || c.number}`, outward, fontSize: su * 0.032, gap, wrap: false, priority: 0 };
      }),
      ...secs.map((s, i) => ({ anchor: sectionAnchors[i], text: s.name, outward: centroidOut(sectionAnchors[i]), fontSize: su * 0.027, gap, wrap: true, priority: 1 })),
    ];
    const res = layoutLabels(items, { bounds: [-pad, -pad, map.width + pad, map.height + pad], markers, line: path });
    return { corners: res.slice(0, map.corners.length), sections: res.slice(map.corners.length) };
  });
  const cornerLabels = $derived(laidOut.corners);
  const sectionLabels = $derived(laidOut.sections);

  // Live car positions along the racing line, colored by class; player emphasized.
  const carDots = $derived(
    map
      ? cars.map((c) => {
          const [x, y] = map!.path[nearestDistIndex(map!.dist, c.lapDist)];
          return { id: c.id, x, y, color: CLASS_COLOR[c.cls] ?? "var(--faint)", isPlayer: c.isPlayer, pos: c.pos };
        })
      : [],
  );
</script>

<div class="panel" class:fill>
  {#if lapId === null && !propMode}
    <p class="empty">Select a lap to see the track map</p>
  {:else if map}
    <svg bind:this={svgEl} viewBox={vb} style="width:100%;height:{fill ? '100%' : 'auto'};touch-action:none">
      <polyline
        {points}
        fill="none"
        stroke="var(--muted)"
        stroke-width={su * 0.007}
        stroke-linejoin="round"
        stroke-linecap="round"
      />
      {#if startDir}
        {@const b = sfBar(startDir.x0, startDir.y0, startDir.ang, su * 0.03)}
        <line x1={b.x1} y1={b.y1} x2={b.x2} y2={b.y2} stroke="var(--text)" stroke-width={su * 0.008} />
        <polygon points={arrow(startDir.xj, startDir.yj, startDir.ang, su * 0.05, su * 0.028)} fill="var(--text)" />
        <text class="sf" x={startDir.x0} y={startDir.y0 - su * 0.04} font-size={su * 0.032} fill="var(--text)" text-anchor="middle">S/F</text>
      {/if}
      {#each map.sections ?? [] as sec, i (sec.name + sec.start_dist)}
        {@const seg = sectionSeg(sec)}
        <polyline points={seg.pts} fill="none" stroke="var(--good, #3a7)" stroke-width={su * 0.02} stroke-linecap="round" opacity="0.35" />
        {@const sl = sectionLabels[i]}
        {#if sl}
          {#if sl.leader}
            <line x1={sl.leader[0]} y1={sl.leader[1]} x2={sl.leader[2]} y2={sl.leader[3]} stroke="var(--good, #3a7)" stroke-width={su * 0.004} opacity="0.6" />
          {/if}
          <text class="section-lbl" text-anchor="middle" font-size={sl.fontSize} fill="var(--good, #3a7)" paint-order="stroke" stroke="var(--surface, #1a1a1a)" stroke-width={su * 0.012} stroke-linejoin="round">
            {#each sl.lines as ln, li}<tspan x={sl.cx} y={sl.cy - (sl.lines.length - 1) * sl.fontSize * 0.51 + li * sl.fontSize * 1.02 + sl.fontSize * 0.34}>{ln}</tspan>{/each}
          </text>
        {/if}
      {/each}
      {#each map.corners as c, i (c.number)}
        {#each c.apexes ?? [c.apex_dist] as ad, ai}
          {@const p = ai === 0 ? [c.x, c.y] : map.path[nearestDistIndex(map.dist, ad)]}
          <circle cx={p[0]} cy={p[1]} r={su * 0.011} fill="var(--accent)"><title>{c.name}</title></circle>
        {/each}
        {@const cl = cornerLabels[i]}
        {#if cl}
          {#if cl.leader}
            <line x1={cl.leader[0]} y1={cl.leader[1]} x2={cl.leader[2]} y2={cl.leader[3]} stroke="var(--accent)" stroke-width={su * 0.004} opacity="0.6" />
          {/if}
          <text class="corner-lbl" x={cl.cx} y={cl.cy + cl.fontSize * 0.34} text-anchor="middle" font-size={cl.fontSize} fill="var(--accent)" paint-order="stroke" stroke="var(--surface, #1a1a1a)" stroke-width={su * 0.012} stroke-linejoin="round">T{c.label || c.number}</text>
        {/if}
      {/each}
      {#each carDots as d (d.id)}
        {@const r = d.isPlayer ? su * 0.032 : su * 0.026}
        <circle
          class="car-dot"
          class:player={d.isPlayer}
          cx={d.x}
          cy={d.y}
          r={r}
          fill={d.color}
          stroke={d.isPlayer ? "var(--text)" : "var(--surface)"}
          stroke-width={su * (d.isPlayer ? 0.012 : 0.008)}
        />
        {#if d.pos}
          <text class="car-pos" x={d.x} y={d.y} dy="0.34em" text-anchor="middle" font-size={r * 1.15} fill="#fff">{d.pos}</text>
        {/if}
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
  /* Fill mode (Pit Wall): occupy the full cell; the SVG scales to fit, centered. */
  .panel.fill {
    height: 100%;
    padding: 8px;
    box-sizing: border-box;
    display: grid;
    place-items: center;
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
