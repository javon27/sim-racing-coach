<script module lang="ts">
  // Pure, unit-testable: index of the path point nearest (x,y) in viewBox coords.
  export function nearestPathIndex(path: [number, number][], x: number, y: number): number {
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < path.length; i++) {
      const dx = path[i][0] - x;
      const dy = path[i][1] - y;
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }

  // Index of the dist[] entry closest to a target lap_distance.
  export function nearestDistIndex(dist: number[], target: number): number {
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < dist.length; i++) {
      const d = Math.abs(dist[i] - target);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }
</script>

<script lang="ts">
  import { getLapMap, saveLapMap, type TrackMap } from "./api";

  let {
    lapId,
    open = false,
    onClose,
    onSaved,
  }: { lapId: number; open?: boolean; onClose?: () => void; onSaved?: () => void } = $props();

  type EditCorner = {
    number: number;
    name: string;
    direction: string;
    apex_dist: number;
    start_dist?: number;
    end_dist?: number;
  };

  let map: TrackMap | null = $state(null);
  let corners: EditCorner[] = $state([]);
  let sectors: number[] = $state([]);
  let saving = $state(false);
  let error: string | null = $state(null);
  let svgEl: SVGSVGElement | null = $state(null);

  // What the pointer is currently dragging: a corner index, a sector index, or nothing.
  let drag: { kind: "corner" | "sector"; i: number } | null = null;
  let addMode = $state(false);

  // Load the AVERAGED outline whenever the modal opens for a lap.
  let loadedFor = -1;
  $effect(() => {
    if (!open || lapId == null) {
      loadedFor = -1;
      return;
    }
    if (loadedFor === lapId) return;
    loadedFor = lapId;
    const id = lapId;
    error = null;
    getLapMap(id, true)
      .then((res) => {
        if (loadedFor !== id) return;
        if ("path" in res && res.path.length > 0) applyMap(res);
        else {
          map = null;
          error = "No outline for this lap yet (position not captured).";
        }
      })
      .catch(() => {
        if (loadedFor !== id) return;
        map = null;
        error = "Failed to load the map.";
      });
  });

  function applyMap(res: TrackMap) {
    map = res;
    corners = res.corners.map((c) => ({
      number: c.number,
      name: c.name,
      direction: c.direction,
      apex_dist: c.apex_dist,
      start_dist: c.start_dist,
      end_dist: c.end_dist,
    }));
    sectors = [...(res.sectors ?? [])];
  }

  const u = $derived(map ? Math.max(map.width, map.height) : 100);
  // Breathing room so the stroke + edge markers/labels aren't clipped (viewBox-padded;
  // the getScreenCTM pointer transform stays correct because it honors the viewBox).
  const pad = $derived(u * 0.09);

  // World point on the racing line for a given lap_distance (for placing markers).
  function ptAtDist(d: number): [number, number] {
    if (!map) return [0, 0];
    return map.path[nearestDistIndex(map.dist, d)];
  }

  // S/F marker + heading arrow, same construction as TrackMap.svelte.
  const startDir = $derived.by(() => {
    if (!map || map.path.length < 2) return null;
    const [x0, y0] = map.path[0];
    const j = Math.min(map.path.length - 1, Math.max(1, Math.round(map.path.length * 0.04)));
    const [xj, yj] = map.path[j];
    return { x0, y0, xj, yj, ang: Math.atan2(yj - y0, xj - x0) };
  });
  function sfBar(x: number, y: number, ang: number, h: number) {
    const px = -Math.sin(ang), py = Math.cos(ang);
    return { x1: x + px * h, y1: y + py * h, x2: x - px * h, y2: y - py * h };
  }
  function arrow(x: number, y: number, ang: number, len: number, wid: number) {
    const dx = Math.cos(ang), dy = Math.sin(ang), px = -dy, py = dx;
    return `${x + dx * len},${y + dy * len} ${x + px * wid},${y + py * wid} ${x - px * wid},${y - py * wid}`;
  }

  // Pointer client coords -> viewBox coords via the SVG's own CTM, which
  // accounts for preserveAspectRatio letterboxing (portrait tracks are taller
  // than the fitted box). jsdom has no CTM, so guard for null.
  function toViewBox(clientX: number, clientY: number): [number, number] {
    const ctm = svgEl?.getScreenCTM?.();
    if (!svgEl || !ctm) return [0, 0];
    const pt = svgEl.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    const v = pt.matrixTransform(ctm.inverse());
    return [v.x, v.y];
  }

  function onPointerMove(e: PointerEvent) {
    if (!drag || !map) return;
    const [vx, vy] = toViewBox(e.clientX, e.clientY);
    const i = nearestPathIndex(map.path, vx, vy);
    const d = map.dist[i];
    if (drag.kind === "corner") corners[drag.i].apex_dist = d;
    else sectors[drag.i] = d;
  }
  function endDrag() {
    drag = null;
  }
  function startDrag(kind: "corner" | "sector", i: number, e: PointerEvent) {
    e.stopPropagation();
    drag = { kind, i };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }

  function onSvgClick(e: PointerEvent) {
    if (!addMode || !map) return;
    const [vx, vy] = toViewBox(e.clientX, e.clientY);
    const i = nearestPathIndex(map.path, vx, vy);
    const n = corners.length ? Math.max(...corners.map((c) => c.number)) + 1 : 1;
    corners.push({ number: n, name: `T${n}`, direction: "", apex_dist: map.dist[i] });
    addMode = false;
  }

  function removeCorner(i: number) {
    corners.splice(i, 1);
  }

  function seedSectors() {
    if (!map) return;
    const max = map.dist[map.dist.length - 1] || 0;
    sectors = [max / 3, (2 * max) / 3];
  }

  async function save() {
    if (lapId == null) return;
    saving = true;
    error = null;
    try {
      const res = await saveLapMap(lapId, {
        corners: corners.map((c) => ({
          number: c.number,
          name: c.name,
          apex_dist: c.apex_dist,
          ...(c.start_dist != null ? { start_dist: c.start_dist } : {}),
          ...(c.end_dist != null ? { end_dist: c.end_dist } : {}),
        })),
        sectors,
      });
      applyMap(res);
      onSaved?.();
    } catch {
      error = "Save failed.";
    } finally {
      saving = false;
    }
  }

  function close() {
    onClose?.();
  }
  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") close();
  }
</script>

<svelte:window onkeydown={open ? onKeydown : undefined} />

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div
    class="backdrop"
    role="presentation"
    onclick={close}
    onpointermove={onPointerMove}
    onpointerup={endDrag}
  >
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="modal" role="dialog" tabindex="-1" aria-modal="true" aria-label="Edit track map" onclick={(e) => e.stopPropagation()}>
      <header>
        <h2>Edit track map</h2>
        <button type="button" class="x" aria-label="Close" onclick={close}>×</button>
      </header>

      {#if map}
        <div class="body">
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <svg
            bind:this={svgEl}
            viewBox="{-pad} {-pad} {map.width + pad * 2} {map.height + pad * 2}"
            class:add={addMode}
            role="application"
            aria-label="Track map — drag corner and sector markers"
            onpointerdown={onSvgClick}
          >
            <polyline
              points={map.path.map(([x, y]) => `${x},${y}`).join(" ")}
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
            {#each sectors as s, i (i)}
              {@const p = ptAtDist(s)}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <rect
                class="sector"
                x={p[0] - u * 0.014}
                y={p[1] - u * 0.014}
                width={u * 0.028}
                height={u * 0.028}
                fill="var(--text)"
                role="slider"
                aria-label="Sector split {i + 1}"
                aria-valuenow={s}
                tabindex="-1"
                onpointerdown={(e) => startDrag("sector", i, e)}
              ><title>Sector split {i + 1}</title></rect>
            {/each}
            {#each corners as c, i (c.number)}
              {@const p = ptAtDist(c.apex_dist)}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <circle
                class="marker"
                cx={p[0]}
                cy={p[1]}
                r={u * 0.014}
                fill="var(--accent)"
                role="slider"
                aria-label="{c.name} apex position"
                aria-valuenow={c.apex_dist}
                tabindex="-1"
                onpointerdown={(e) => startDrag("corner", i, e)}
              ><title>{c.name}</title></circle>
              <text class="corner-lbl" x={p[0] + u * 0.016} y={p[1] - u * 0.016} font-size={u * 0.03} fill="var(--accent)">T{c.number}</text>
            {/each}
          </svg>

          <aside class="side">
            <div class="tools">
              <button type="button" class:on={addMode} onclick={() => (addMode = !addMode)}>
                {addMode ? "Click the line…" : "Add corner"}
              </button>
              {#if sectors.length === 0}
                <button type="button" onclick={seedSectors}>Add sectors</button>
              {/if}
            </div>
            <ul class="corners">
              {#each corners as c, i (c.number)}
                <li>
                  <input
                    type="number"
                    class="num"
                    bind:value={c.number}
                    aria-label="Corner number"
                  />
                  <input class="name" bind:value={c.name} aria-label="Corner name" />
                  <span class="dir" data-dir={c.direction}>{c.direction || "—"}</span>
                  <button type="button" class="rm" aria-label="Remove corner" onclick={() => removeCorner(i)}>×</button>
                </li>
              {/each}
            </ul>
          </aside>
        </div>
      {:else}
        <p class="msg">{error ?? "Loading…"}</p>
      {/if}

      {#if error && map}<p class="msg err">{error}</p>{/if}

      <footer>
        <button type="button" onclick={close}>Cancel</button>
        <button type="button" class="primary" disabled={saving || !map} onclick={save}>
          {saving ? "Saving…" : "Save"}
        </button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  .modal {
    background: var(--bg, #1a1a1a);
    color: var(--text);
    border: 1px solid var(--muted);
    border-radius: 8px;
    width: min(1000px, 94vw);
    max-height: 92vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header,
  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--muted);
  }
  footer {
    border-bottom: none;
    border-top: 1px solid var(--muted);
    justify-content: flex-end;
  }
  header h2 {
    margin: 0;
    font-size: 15px;
  }
  .body {
    display: flex;
    gap: 12px;
    padding: 12px;
    overflow: auto;
  }
  svg {
    flex: 1 1 auto;
    width: 100%;
    height: auto;
    max-height: 70vh;
  }
  svg.add {
    cursor: crosshair;
  }
  .marker {
    cursor: grab;
  }
  .sector {
    cursor: grab;
  }
  .side {
    flex: 0 0 260px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .tools {
    display: flex;
    gap: 6px;
  }
  .corners {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow: auto;
  }
  .corners li {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 0;
  }
  .num {
    width: 44px;
  }
  .name {
    flex: 1 1 auto;
    min-width: 0;
  }
  .dir {
    font-size: 12px;
    color: var(--faint);
    min-width: 40px;
    text-transform: capitalize;
  }
  button.on {
    outline: 2px solid var(--accent);
  }
  button.primary {
    background: var(--accent);
    color: #000;
  }
  .msg {
    padding: 24px;
    text-align: center;
    color: var(--faint);
  }
  .msg.err {
    color: var(--danger, #e66);
  }
  /* Labels are decoration, not selectable — same rule as TrackMap. Markers keep
     their own pointer handlers (they are not <text>). */
  svg text {
    user-select: none;
    -webkit-user-select: none;
    pointer-events: none;
    cursor: default;
    font-family: var(--mono, monospace);
    font-weight: 600;
  }
</style>
