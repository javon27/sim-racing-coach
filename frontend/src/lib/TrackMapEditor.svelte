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
  import { onDestroy } from "svelte";
  import {
    getLapMap,
    saveLapMap,
    setMapRotation,
    startMapCapture,
    cancelMapCapture,
    mapCaptureStatus,
    buildRibbon,
    saveRibbon,
    type TrackMap,
    type RibbonPreview,
  } from "./api";

  let {
    lapId,
    open = false,
    onClose,
    onSaved,
    pollMs = 2000,
    rotDebounceMs = 400,
  }: {
    lapId: number;
    open?: boolean;
    onClose?: () => void;
    onSaved?: () => void;
    pollMs?: number;
    rotDebounceMs?: number;
  } = $props();

  type EditCorner = {
    number: number;
    label: string; // "" follows number; a typed value sticks
    name: string;
    direction: string;
    apex_dist: number;
    apexes: number[];
    start_dist?: number;
    end_dist?: number;
  };
  type Section = { name: string; kind: string; start_dist: number; end_dist: number };

  let map: TrackMap | null = $state(null);
  let corners: EditCorner[] = $state([]);
  let sectors: number[] = $state([]);
  let sections: Section[] = $state([]);
  let saving = $state(false);
  let error: string | null = $state(null);
  let svgEl: SVGSVGElement | null = $state(null);

  // What the pointer is currently dragging. `ai` = apex index for kind "apex".
  type DragKind = "corner" | "sector" | "apex" | "secstart" | "secend";
  let drag: { kind: DragKind; i: number; ai?: number } | null = null;
  let addMode = $state(false);
  let addSectionMode = $state(false);

  // Orientation control. `angle` is the target the user is dialing in; the live
  // SVG preview rotates the fitted map by (angle - baked). Changing it persists
  // (debounced) and merges ONLY the rebaked geometry back — never the corners.
  // Net displayed orientation = baked(map.rotation) + preview(angle - map.rotation)
  // = angle, so it always reads 1×, matching what the user entered.
  let angle = $state(0);
  let rotTimer: ReturnType<typeof setTimeout> | undefined;
  function rotateBy(d: number) {
    angle = (((angle + d + 180) % 360) + 360) % 360 - 180;
    scheduleRotation();
  }
  function resetRotation() {
    angle = 0;
    scheduleRotation();
  }
  function scheduleRotation() {
    clearTimeout(rotTimer);
    rotTimer = setTimeout(persistRotation, rotDebounceMs);
  }
  async function persistRotation() {
    if (lapId == null || !map || angle === (map.rotation ?? 0)) return;
    error = null;
    try {
      const res = await setMapRotation(lapId, angle);
      // Non-destructive: take only the rebaked geometry; keep the user's edited
      // corners/sectors/sections (markers re-project onto the new path via apex_dist).
      map = {
        ...map,
        path: res.path,
        dist: res.dist,
        width: res.width,
        height: res.height,
        rotation: res.rotation ?? angle,
      };
      onSaved?.();
    } catch {
      error = "Rotation failed.";
      // Revert to the baked angle so mapPreviewRot returns to 0 and drags realign.
      if (map) angle = map.rotation ?? 0;
    }
  }

  // --- "(Re)generate map layout": guided mapping capture -> ribbon preview.
  type GenPhase = "off" | "left" | "right" | "designate" | "building" | "preview";
  let genPhase: GenPhase = $state("off");
  let capLaps: { idx: number; samples: number; length: number }[] = $state([]);
  let leftIdx: number | null = $state(null);
  let rightIdx: number | null = $state(null);
  let ribbon: RibbonPreview | null = $state(null);
  let genErr: string | null = $state(null);
  let pollTimer: ReturnType<typeof setInterval> | undefined;

  function stopPoll() {
    if (pollTimer !== undefined) clearInterval(pollTimer);
    pollTimer = undefined;
  }
  function resetGen() {
    stopPoll();
    genPhase = "off";
    capLaps = [];
    leftIdx = null;
    rightIdx = null;
    ribbon = null;
    genErr = null;
  }

  async function startGen() {
    genErr = null;
    try {
      await startMapCapture();
    } catch {
      genErr = "Couldn't start mapping capture — is the sim running?";
      return;
    }
    capLaps = [];
    leftIdx = null;
    rightIdx = null;
    ribbon = null;
    genPhase = "left";
    stopPoll();
    pollTimer = setInterval(pollGen, pollMs);
  }

  async function pollGen() {
    try {
      const s = await mapCaptureStatus();
      capLaps = s.laps;
      if (genPhase === "left" && s.laps.length >= 1) {
        leftIdx = s.laps[0].idx;
        genPhase = "right";
      } else if (genPhase === "right" && s.laps.length >= 2) {
        rightIdx = s.laps[1].idx;
        stopPoll();
        genPhase = "designate";
      }
    } catch {
      /* transient poll failure — keep waiting */
    }
  }

  async function buildGen() {
    if (leftIdx == null || rightIdx == null) return;
    genPhase = "building";
    genErr = null;
    try {
      ribbon = await buildRibbon(leftIdx, rightIdx);
      genPhase = "preview";
    } catch {
      genErr = "Couldn't reconstruct the ribbon from those laps.";
      genPhase = "designate";
    }
  }

  async function saveGen() {
    saving = true;
    genErr = null;
    try {
      await saveRibbon();
      onSaved?.();
      resetGen();
      onClose?.();
    } catch {
      genErr = "Save failed.";
    } finally {
      saving = false;
    }
  }

  async function cancelGen() {
    stopPoll();
    try {
      await cancelMapCapture();
    } catch {
      /* best-effort — server drops the buffer on its own too */
    }
    resetGen();
  }

  onDestroy(() => {
    stopPoll();
    clearTimeout(rotTimer);
    // Unmount mid-capture -> tell the server to leave mapping mode, else real
    // laps keep buffering unsaved.
    if (genPhase !== "off") cancelMapCapture().catch(() => {});
  });

  // Ribbon fill = left edge forward, right edge back, closed.
  const ribbonBand = $derived.by(() => {
    if (!ribbon) return "";
    const l = ribbon.left.map(([x, y]) => `${x},${y}`).join(" L");
    const r = [...ribbon.right].reverse().map(([x, y]) => `${x},${y}`).join(" L");
    return `M${l} L${r} Z`;
  });
  const ru = $derived(ribbon ? Math.max(ribbon.width, ribbon.height) : 100);
  const rpad = $derived(ru * 0.09);
  const ribbonStart = $derived.by(() => {
    if (!ribbon || ribbon.center.length < 2) return null;
    const [x0, y0] = ribbon.center[0];
    const j = Math.min(ribbon.center.length - 1, Math.max(1, Math.round(ribbon.center.length * 0.04)));
    const [xj, yj] = ribbon.center[j];
    return { x0, y0, xj, yj, ang: Math.atan2(yj - y0, xj - x0) };
  });

  // Load the AVERAGED outline whenever the modal opens for a lap.
  let loadedFor = -1;
  $effect(() => {
    if (!open || lapId == null) {
      loadedFor = -1;
      // Hiding/unmounting mid-capture must not leave the server buffering real
      // laps into mapping mode (silent data loss) — same teardown as the × button.
      if (genPhase !== "off") cancelGen();
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
      label: c.label ?? String(c.number),
      name: c.name,
      direction: c.direction,
      apex_dist: c.apex_dist,
      apexes: c.apexes && c.apexes.length ? [...c.apexes] : [c.apex_dist],
      start_dist: c.start_dist,
      end_dist: c.end_dist,
    }));
    sectors = [...(res.sectors ?? [])];
    sections = (res.sections ?? []).map((s) => ({ ...s }));
    angle = res.rotation ?? 0;
  }

  // Path slice + midpoint for a named section span (start_dist..end_dist).
  function sectionSeg(s: Section) {
    if (!map) return { pts: "", mid: [0, 0] as [number, number] };
    const a = nearestDistIndex(map.dist, s.start_dist);
    const b = nearestDistIndex(map.dist, s.end_dist);
    const seg = map.path.slice(Math.min(a, b), Math.max(a, b) + 1);
    const mid = seg.length ? seg[Math.floor(seg.length / 2)] : map.path[a];
    return { pts: seg.map(([x, y]) => `${x},${y}`).join(" "), mid };
  }

  // Live preview delta: the fitted map already reflects its baked rotation, so
  // only rotate by the difference. Ribbon is fresh/unbaked -> rotate by `angle`.
  const mapPreviewRot = $derived(angle - (map?.rotation ?? 0));

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
    switch (drag.kind) {
      case "corner":
        corners[drag.i].apex_dist = d;
        corners[drag.i].apexes[0] = d; // primary apex stays synced to apex_dist
        break;
      case "apex":
        corners[drag.i].apexes[drag.ai!] = d;
        break;
      case "sector":
        sectors[drag.i] = d;
        break;
      case "secstart":
        sections[drag.i].start_dist = d;
        break;
      case "secend":
        sections[drag.i].end_dist = d;
        break;
    }
  }
  function endDrag() {
    const wasCorner = drag?.kind === "corner";
    drag = null;
    // Dragging a corner past another changes track order → renumber.
    if (wasCorner) renumber();
  }
  // Corner number = position in track order (by apex_dist). Keeps T-labels
  // sequential when corners are added between others or dragged past each other.
  function renumber() {
    corners = [...corners]
      .sort((a, b) => a.apex_dist - b.apex_dist)
      .map((c, idx) => ({ ...c, number: idx + 1 }));
  }
  function startDrag(kind: DragKind, i: number, e: PointerEvent, ai?: number) {
    e.stopPropagation();
    drag = { kind, i, ai };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }

  function onSvgClick(e: PointerEvent) {
    if (!map || (!addMode && !addSectionMode)) return;
    const [vx, vy] = toViewBox(e.clientX, e.clientY);
    const clicked = map.dist[nearestPathIndex(map.path, vx, vy)];

    if (addSectionMode) {
      // Place a section spanning ±5% of lap length around the click, clamped.
      const total = map.dist[map.dist.length - 1] || 0;
      const half = total * 0.05;
      sections = [
        ...sections,
        {
          name: "Section",
          kind: "section",
          start_dist: Math.max(0, clicked - half),
          end_dist: Math.min(total, clicked + half),
        },
      ];
      addSectionMode = false;
      return;
    }

    // Insert in track order + renumber, so a corner added between T1 and T2
    // becomes T2 (not appended as the highest number). Name defaults empty →
    // the map label shows T{number}; server falls back to "Turn N".
    corners = [...corners, { number: 0, label: "", name: "", direction: "", apex_dist: clicked, apexes: [clicked] }];
    renumber();
    // Prefill "Turn N" using the positional number renumber() just assigned.
    const added = corners.find((c) => c.apex_dist === clicked && !c.name);
    if (added) added.name = `Turn ${added.number}`;
    addMode = false;
  }

  function removeCorner(i: number) {
    corners = corners.filter((_, idx) => idx !== i);
    renumber();
  }

  function seedSectors() {
    if (!map) return;
    const max = map.dist[map.dist.length - 1] || 0;
    sectors = [max / 3, (2 * max) / 3];
  }

  // Double-apex: add/remove a second apex, snapped to the line just past the primary.
  function addApex(i: number) {
    if (!map) return;
    const c = corners[i];
    const j = Math.min(map.dist.length - 1, nearestDistIndex(map.dist, c.apex_dist) + Math.round(map.dist.length * 0.02));
    c.apexes = [...c.apexes, map.dist[j]];
  }
  function removeApex(i: number, ai: number) {
    corners[i].apexes = corners[i].apexes.filter((_, k) => k !== ai);
  }

  // Enter add-section mode; the next click on the line places it (like add-corner).
  function toggleAddSection() {
    addSectionMode = !addSectionMode;
    if (addSectionMode) addMode = false;
  }
  function removeSection(i: number) {
    sections = sections.filter((_, k) => k !== i);
  }

  async function save() {
    if (lapId == null) return;
    saving = true;
    error = null;
    try {
      const res = await saveLapMap(lapId, {
        corners: corners.map((c) => ({
          number: c.number,
          label: c.label,
          name: c.name,
          apex_dist: c.apex_dist,
          apexes: c.apexes,
          ...(c.start_dist != null ? { start_dist: c.start_dist } : {}),
          ...(c.end_dist != null ? { end_dist: c.end_dist } : {}),
        })),
        sectors,
        sections,
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
    if (genPhase !== "off") cancelGen();
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
        {#if map || ribbon}
          <div class="rot" role="group" aria-label="Rotate map">
            <button type="button" aria-label="Rotate 90° left" onclick={() => rotateBy(-90)}>⟲</button>
            <input type="number" step="5" aria-label="Rotation degrees" bind:value={angle} oninput={scheduleRotation} />
            <span>°</span>
            <button type="button" aria-label="Rotate 90° right" onclick={() => rotateBy(90)}>⟳</button>
            <button type="button" onclick={resetRotation}>0°</button>
          </div>
        {/if}
        <button type="button" class="x" aria-label="Close" onclick={close}>×</button>
      </header>

      {#if genPhase === "left" || genPhase === "right" || genPhase === "designate" || genPhase === "building"}
        <div class="genpanel">
          <h3>Generate map layout from boundary laps</h3>
          {#if genPhase === "left"}
            <p class="step"><b>Step 1 — LEFT edge.</b> Drive one slow lap hugging the <b>left</b>
              boundary of the track. Waiting for a completed lap…</p>
          {:else if genPhase === "right"}
            <p class="step"><b>Left edge captured.</b> Step 2 — <b>RIGHT edge</b>. Drive a slow lap
              hugging the <b>right</b> boundary. Waiting for a completed lap…</p>
          {:else if genPhase === "building"}
            <p class="step">Reconstructing the track ribbon…</p>
          {:else}
            <p class="step">Both edges captured. Confirm which buffered lap is which, then build.</p>
            <label>Left edge lap
              <select bind:value={leftIdx} aria-label="Left edge lap">
                {#each capLaps as l (l.idx)}<option value={l.idx}>Lap {l.idx} · {l.samples} pts</option>{/each}
              </select>
            </label>
            <label>Right edge lap
              <select bind:value={rightIdx} aria-label="Right edge lap">
                {#each capLaps as l (l.idx)}<option value={l.idx}>Lap {l.idx} · {l.samples} pts</option>{/each}
              </select>
            </label>
            <button
              type="button"
              class="primary"
              onclick={buildGen}
              disabled={leftIdx == null || rightIdx == null || leftIdx === rightIdx}
            >Build ribbon</button>
          {/if}
          {#if capLaps.length}<p class="cap">{capLaps.length} lap(s) buffered</p>{/if}
        </div>
      {:else if ribbon}
        <div class="body">
          <svg
            viewBox="{-rpad} {-rpad} {ribbon.width + rpad * 2} {ribbon.height + rpad * 2}"
            role="img"
            aria-label="Reconstructed track ribbon"
          >
            <g transform="rotate({angle}, {ribbon.width / 2}, {ribbon.height / 2})">
            <path d={ribbonBand} fill="var(--surface-2, #333)" stroke="var(--muted)" stroke-width={ru * 0.004} stroke-linejoin="round" />
            <polyline
              points={ribbon.center.map(([x, y]) => `${x},${y}`).join(" ")}
              fill="none"
              stroke="var(--muted)"
              stroke-width={ru * 0.004}
              stroke-dasharray="{ru * 0.02} {ru * 0.02}"
            />
            {#if ribbonStart}
              {@const b = sfBar(ribbonStart.x0, ribbonStart.y0, ribbonStart.ang, ru * 0.03)}
              <line x1={b.x1} y1={b.y1} x2={b.x2} y2={b.y2} stroke="var(--text)" stroke-width={ru * 0.008} />
              <polygon points={arrow(ribbonStart.xj, ribbonStart.yj, ribbonStart.ang, ru * 0.05, ru * 0.028)} fill="var(--text)" />
              <text class="sf" x={ribbonStart.x0} y={ribbonStart.y0 - ru * 0.04} font-size={ru * 0.032} fill="var(--text)" text-anchor="middle">S/F</text>
            {/if}
            {#each ribbon.corners as c (c.number)}
              <circle cx={c.x} cy={c.y} r={ru * 0.014} fill="var(--accent)"><title>{c.name}</title></circle>
              <text class="corner-lbl" x={c.x + ru * 0.024} y={c.y - ru * 0.024} font-size={ru * 0.03} fill="var(--accent)" paint-order="stroke" stroke="var(--bg, #1a1a1a)" stroke-width={ru * 0.012} stroke-linejoin="round">T{c.label || c.number}</text>
            {/each}
            </g>
          </svg>
          <aside class="side">
            <p class="hint">Reconstructed layout — review the corners, then Save. Rename/adjust with
              the normal editor after saving.</p>
            <ul class="corners">
              {#each ribbon.corners as c (c.number)}
                <li>
                  <span class="num">T{c.number}</span>
                  <span class="name-ro">{c.name || "—"}</span>
                  <span class="dir" data-dir={c.direction}>{c.direction || "—"}</span>
                </li>
              {/each}
            </ul>
          </aside>
        </div>
      {:else if map}
        <div class="body">
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <svg
            bind:this={svgEl}
            viewBox="{-pad} {-pad} {map.width + pad * 2} {map.height + pad * 2}"
            class:add={addMode || addSectionMode}
            role="application"
            aria-label="Track map — drag corner and sector markers"
            onpointerdown={onSvgClick}
          >
            <g transform="rotate({mapPreviewRot}, {map.width / 2}, {map.height / 2})">
            <polyline
              points={map.path.map(([x, y]) => `${x},${y}`).join(" ")}
              fill="none"
              stroke="var(--muted)"
              stroke-width={u * 0.007}
              stroke-linejoin="round"
              stroke-linecap="round"
            />
            {#each sections as sec, i (i)}
              {@const seg = sectionSeg(sec)}
              {@const ps = ptAtDist(sec.start_dist)}
              {@const pe = ptAtDist(sec.end_dist)}
              <polyline class="section-band" points={seg.pts} fill="none" stroke="var(--good, #3a7)" stroke-width={u * 0.02} stroke-linecap="round" opacity="0.35" />
              <text class="section-lbl" x={seg.mid[0]} y={seg.mid[1] - u * 0.03} font-size={u * 0.026} fill="var(--good, #3a7)" text-anchor="middle" paint-order="stroke" stroke="var(--bg, #1a1a1a)" stroke-width={u * 0.012} stroke-linejoin="round">{sec.name}</text>
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <circle class="sec-handle" cx={ps[0]} cy={ps[1]} r={u * 0.012} fill="var(--good, #3a7)" role="slider" aria-label="{sec.name} start" aria-valuenow={sec.start_dist} tabindex="-1" onpointerdown={(e) => startDrag("secstart", i, e)}><title>{sec.name} start</title></circle>
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <circle class="sec-handle" cx={pe[0]} cy={pe[1]} r={u * 0.012} fill="var(--good, #3a7)" role="slider" aria-label="{sec.name} end" aria-valuenow={sec.end_dist} tabindex="-1" onpointerdown={(e) => startDrag("secend", i, e)}><title>{sec.name} end</title></circle>
            {/each}
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
              {#each c.apexes as ad, ai (ai)}
                {@const p = ptAtDist(ad)}
                <!-- svelte-ignore a11y_no_static_element_interactions -->
                <circle
                  class="marker"
                  cx={p[0]}
                  cy={p[1]}
                  r={u * 0.014}
                  fill={ai === 0 ? "var(--accent)" : "var(--bg, #1a1a1a)"}
                  stroke="var(--accent)"
                  stroke-width={ai === 0 ? 0 : u * 0.005}
                  role="slider"
                  aria-label="{c.name} apex {ai + 1}"
                  aria-valuenow={ad}
                  tabindex="-1"
                  onpointerdown={(e) => startDrag(ai === 0 ? "corner" : "apex", i, e, ai)}
                ><title>{c.name}</title></circle>
              {/each}
              {@const lp = ptAtDist(c.apex_dist)}
              <text class="corner-lbl" x={lp[0] + u * 0.024} y={lp[1] - u * 0.024} font-size={u * 0.03} fill="var(--accent)" paint-order="stroke" stroke="var(--bg, #1a1a1a)" stroke-width={u * 0.012} stroke-linejoin="round">T{c.label || c.number}</text>
            {/each}
            </g>
          </svg>

          <aside class="side">
            <div class="tools">
              <button type="button" class:on={addMode} onclick={() => { addMode = !addMode; if (addMode) addSectionMode = false; }}>
                {addMode ? "Click the line…" : "Add corner"}
              </button>
              {#if sectors.length === 0}
                <button type="button" onclick={seedSectors}>Add sectors</button>
              {/if}
              <button type="button" class:on={addSectionMode} onclick={toggleAddSection}>
                {addSectionMode ? "Click the line…" : "Add section"}
              </button>
              <button type="button" onclick={startGen}>(Re)generate map layout</button>
            </div>
            <ul class="corners">
              {#each corners as c, i (c.number)}
                <li>
                  <span class="num" aria-label="Corner number">T{c.number}</span>
                  <input class="lbl-in" bind:value={c.label} placeholder={String(c.number)} aria-label="Corner label" />
                  <input class="name" bind:value={c.name} aria-label="Corner name" />
                  <span class="dir" data-dir={c.direction}>{c.direction || "—"}</span>
                  {#if c.apexes.length < 2}
                    <button type="button" class="apex-btn" aria-label="Add apex" onclick={() => addApex(i)}>+apex</button>
                  {:else}
                    <button type="button" class="apex-btn" aria-label="Remove apex" onclick={() => removeApex(i, c.apexes.length - 1)}>−apex</button>
                  {/if}
                  <button type="button" class="rm" aria-label="Remove corner" onclick={() => removeCorner(i)}>×</button>
                </li>
              {/each}
            </ul>
            {#if sections.length}
              <ul class="sections">
                {#each sections as s, i (i)}
                  <li>
                    <input class="name" bind:value={s.name} aria-label="Section name" />
                    <select bind:value={s.kind} aria-label="Section kind">
                      <option value="straight">straight</option>
                      <option value="complex">complex</option>
                      <option value="section">section</option>
                      <option value="drs">drs</option>
                    </select>
                    <button type="button" class="rm" aria-label="Remove section" onclick={() => removeSection(i)}>×</button>
                  </li>
                {/each}
              </ul>
            {/if}
          </aside>
        </div>
      {:else}
        <p class="msg">{error ?? "Loading…"}</p>
      {/if}

      {#if error && map && genPhase === "off"}<p class="msg err">{error}</p>{/if}
      {#if genErr}<p class="msg err">{genErr}</p>{/if}

      <footer>
        {#if genPhase !== "off"}
          <button type="button" onclick={cancelGen}>Cancel capture</button>
          {#if genPhase === "preview"}
            <button type="button" class="primary" disabled={saving} onclick={saveGen}>
              {saving ? "Saving…" : "Save ribbon"}
            </button>
          {/if}
        {:else}
          <button type="button" onclick={close}>Cancel</button>
          <button type="button" class="primary" disabled={saving || !map} onclick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
        {/if}
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
    flex-wrap: wrap;
  }
  footer {
    border-bottom: none;
    border-top: 1px solid var(--muted);
    justify-content: flex-end;
  }
  header h2 {
    margin: 0;
    font-size: 15px;
    margin-right: auto;
  }
  .rot {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }
  .rot input {
    width: 56px;
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
    flex-wrap: wrap;
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
    width: 30px;
    font-size: 12px;
    color: var(--faint);
  }
  .lbl-in {
    width: 44px;
    flex: none;
  }
  .apex-btn {
    font-size: 11px;
    flex: none;
  }
  .sec-handle {
    cursor: grab;
  }
  .sections {
    list-style: none;
    margin: 8px 0 0;
    padding: 8px 0 0;
    border-top: 1px solid var(--muted);
  }
  .sections li {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 0;
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
  .genpanel {
    padding: 20px 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-width: 520px;
  }
  .genpanel h3 {
    margin: 0;
    font-size: 14px;
  }
  .genpanel .step {
    margin: 0;
    line-height: 1.5;
  }
  .genpanel label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--faint);
  }
  .genpanel .cap {
    margin: 0;
    font-size: 12px;
    color: var(--faint);
  }
  .hint {
    font-size: 12px;
    color: var(--faint);
    margin: 0 0 8px;
  }
  .name-ro {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 13px;
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
