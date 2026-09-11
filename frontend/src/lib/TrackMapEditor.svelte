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
  // Start/finish marker geometry, shared by every map view. S/F is path[0]; its bar is
  // perpendicular to the LOCAL tangent there. The heading arrow sits a fixed fraction of
  // the drawing's size down the line (not a fraction of the path's point count) and
  // points along the local tangent. The old construction put the arrow at 4% of the
  // points and aimed it along the chord from S/F: on Spa that is ~280 m in, exactly on
  // La Source, with the chord cutting across the hairpin.
  export function startMarker(path: [number, number][]) {
    const n = path.length;
    if (n < 2) return null;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [x, y] of path) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    const target = Math.max(maxX - minX, maxY - minY) * 0.08;
    let j = 1;
    let acc = Math.hypot(path[1][0] - path[0][0], path[1][1] - path[0][1]);
    while (acc < target && j < n - 1) {
      j++;
      acc += Math.hypot(path[j][0] - path[j - 1][0], path[j][1] - path[j - 1][1]);
    }
    const [x0, y0] = path[0];
    const [xj, yj] = path[j];
    const [ax, ay] = path[j - 1];
    const [bx, by] = path[Math.min(j + 1, n - 1)];
    return {
      x0, y0, xj, yj,
      sfAng: Math.atan2(path[1][1] - y0, path[1][0] - x0),
      ang: Math.atan2(by - ay, bx - ax),
    };
  }

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

  export type PlacedLabel = {
    cx: number; // label box centre (text is drawn centred here)
    cy: number;
    lines: string[]; // 1–2 lines (long names wrap)
    fontSize: number;
    // dot -> nearest box edge, drawn ONLY as a last resort (no valid ring slot).
    leader: [number, number, number, number] | null;
  };

  export type LabelItem = {
    anchor: [number, number]; // the dot this label belongs to
    text: string;
    outward: [number, number]; // unit; preferred direction (a hint, not a rule)
    fontSize: number;
    gap: number; // ring radius — the box's near edge sits this far off the dot
    wrap: boolean; // wrap long names onto two lines
    priority: number; // 0 = corners (placed first), 1 = sections (route around corners)
  };

  // Split a long name into two balanced lines at a word boundary.
  export function wrapLabel(text: string, maxChars = 11): string[] {
    if (text.length <= maxChars || !text.includes(" ")) return [text];
    const w = text.split(" ");
    let bi = 1, bs = Infinity;
    for (let i = 1; i < w.length; i++) {
      const s = Math.max(w.slice(0, i).join(" ").length, w.slice(i).join(" ").length);
      if (s < bs) { bs = s; bi = i; }
    }
    return [w.slice(0, bi).join(" "), w.slice(bi).join(" ")];
  }

  // Unit vector to the OUTSIDE of the bend at path[idx] (opposite the local curvature).
  // curv≈0 on a straight — the caller then falls back to a centroid-outward hint.
  export function localOutward(path: [number, number][], idx: number, k: number): { ox: number; oy: number; curv: number } {
    const N = path.length;
    const a = path[(idx - k + N) % N], b = path[(idx + k) % N], c = path[idx];
    const ix = a[0] + b[0] - 2 * c[0], iy = a[1] + b[1] - 2 * c[1]; // -> inside of the bend
    const il = Math.hypot(ix, iy);
    return il > 1e-6 ? { ox: -ix / il, oy: -iy / il, curv: il } : { ox: 0, oy: 0, curv: 0 };
  }

  // Ring-of-candidates point labeling (the standard point-feature model). Each label may
  // sit anywhere on a ring around its OWN dot; a ring position is INVALID where its box
  // overlaps another label, a dot, or the racing line, or leaves the viewBox. Among valid
  // positions a corner takes the one nearest its outward (perpendicular) direction; a section
  // takes the one closest to its dot (so it reads as attached), both biased away from other
  // dots. The ring is tight (a few px off the dot) and steps out only when blocked, so a
  // section routes around corner labels. A leader is drawn ONLY when no ring slot is valid.
  // Corners are placed first (priority 0) so sections avoid the corner labels.
  export function layoutLabels(
    items: LabelItem[],
    o: { bounds: [number, number, number, number]; markers: [number, number][]; line: [number, number][] },
  ): PlacedLabel[] {
    const [minX, minY, maxX, maxY] = o.bounds, markers = o.markers, line = o.line;
    const ovA = (cx: number, cy: number, ax: number, ay: number, bx: number, by: number, bax: number, bay: number) => {
      const ox = Math.min(cx + ax, bx + bax) - Math.max(cx - ax, bx - bax);
      const oy = Math.min(cy + ay, by + bay) - Math.max(cy - ay, by - bay);
      return ox > 0 && oy > 0 ? ox * oy : 0;
    };
    // half-extent of a hw×hh box from its centre along unit direction (dx,dy)
    const edgeDist = (dx: number, dy: number, hw: number, hh: number) =>
      Math.min(Math.abs(dx) > 1e-9 ? hw / Math.abs(dx) : Infinity, Math.abs(dy) > 1e-9 ? hh / Math.abs(dy) : Infinity);
    const norm = (a: number) => { a %= Math.PI * 2; if (a > Math.PI) a -= Math.PI * 2; if (a < -Math.PI) a += Math.PI * 2; return a; };

    const B = items.map((it) => {
      const fs = it.fontSize;
      const lines = it.wrap ? wrapLabel(it.text) : [it.text];
      const w = Math.max(...lines.map((l) => l.length)) * fs * 0.6, h = lines.length * fs * 1.02;
      return { fs, lines, hw: w / 2 + fs * 0.15, hh: h / 2 + fs * 0.12, mr: fs * 0.6 };
    });
    const order = items.map((_, i) => i).sort((a, b) => items[a].priority - items[b].priority);
    const placed: { cx: number; cy: number; ax: number; ay: number }[] = [];
    const out = new Array<PlacedLabel>(items.length);

    for (const idx of order) {
      const it = items[idx], b = B[idx], [ux, uy] = it.outward;
      const outAng = Math.atan2(uy, ux);
      type Cand = { cx: number; cy: number; ang: number; ti: number };
      const cands: Cand[] = [];
      // ring positions: box near-edge sits `g` off the dot at every angle; `g` grows a
      // label-height per tier so a blocked label steps out past a corner-label cluster.
      for (let ti = 0; ti < 5; ti++) {
        const g = it.gap + ti * b.hh * 1.1;
        for (let ai = 0; ai < 24; ai++) {
          const ang = ai * (Math.PI / 12), dx = Math.cos(ang), dy = Math.sin(ang);
          const dist = g + edgeDist(dx, dy, b.hw, b.hh);
          cands.push({ cx: it.anchor[0] + dx * dist, cy: it.anchor[1] + dy * dist, ang, ti });
        }
      }
      // corners keep a hair-thin tolerance (their layout is dialed in); sections must FULLY
      // clear other labels — so a section never sits on a corner label, it expands instead.
      const tol = it.priority === 0 ? b.fs * b.fs * 0.12 : 4;
      let chosen: Cand | null = null, bestClean = Infinity, bestAny: Cand | null = null, bestAnyScore = Infinity;
      for (const c of cands) {
        let hard = 0;
        if (c.cx - b.hw < minX || c.cx + b.hw > maxX || c.cy - b.hh < minY || c.cy + b.hh > maxY) hard += 1e9;
        for (const p of placed) { const o2 = ovA(c.cx, c.cy, b.hw, b.hh, p.cx, p.cy, p.ax, p.ay); if (o2 > tol) hard += o2; }
        let nearOther = Infinity;
        for (const m of markers) {
          if (Math.abs(m[0] - it.anchor[0]) < 1 && Math.abs(m[1] - it.anchor[1]) < 1) continue; // skip own dot
          const o2 = ovA(c.cx, c.cy, b.hw + b.mr, b.hh + b.mr, m[0], m[1], 0, 0); if (o2 > 0) hard += o2; // never cover another dot
          const d = Math.hypot(c.cx - m[0], c.cy - m[1]); if (d < nearOther) nearOther = d;
        }
        for (const q of line) if (Math.abs(q[0] - c.cx) < b.hw && Math.abs(q[1] - c.cy) < b.hh) { hard += b.hw * b.hh; break; } // on the line = invalid
        const pr = (b.hh + it.gap) * 4, prox = nearOther < pr ? (pr - nearOther) / (b.hh + it.gap) : 0;
        const cdist = Math.hypot(c.cx - it.anchor[0], c.cy - it.anchor[1]);
        // corners: own ring > avoid other dots > face outward. sections: own ring > avoid
        // other dots > hug the dot (closest centre = short edge toward it, i.e. above/below).
        const soft = it.priority === 0
          ? c.ti * 10 + prox * 3 + Math.abs(norm(c.ang - outAng)) * 0.7
          : c.ti * 10 + prox * 3 + (cdist / (b.hh + it.gap)) * 1.2;
        if (hard === 0 && soft < bestClean) { bestClean = soft; chosen = c; }
        const total = hard * 1e-3 + soft;
        if (total < bestAnyScore) { bestAnyScore = total; bestAny = c; }
      }
      const pick = (chosen ?? bestAny)!;
      placed.push({ cx: pick.cx, cy: pick.cy, ax: b.hw, ay: b.hh });
      let leader: [number, number, number, number] | null = null;
      if (!chosen) { // no valid slot -> connect the displaced label back to its dot
        const ex = Math.max(pick.cx - b.hw, Math.min(it.anchor[0], pick.cx + b.hw));
        const ey = Math.max(pick.cy - b.hh, Math.min(it.anchor[1], pick.cy + b.hh));
        leader = [it.anchor[0], it.anchor[1], ex, ey];
      }
      out[idx] = { cx: pick.cx, cy: pick.cy, lines: b.lines, fontSize: b.fs, leader };
    }
    return out;
  }
</script>

<script lang="ts">
  import { onDestroy } from "svelte";
  import { attachWheelZoom, attachPan, type Box } from "./mapzoom";
  import {
    getLapMap,
    getTrackMap,
    saveLapMap,
    saveTrackMap,
    setMapRotation,
    setTrackMapRotation,
    startMapCapture,
    cancelMapCapture,
    mapCaptureStatus,
    buildRibbon,
    rebuildFromCapture,
    saveRibbon,
    saveMapCloud,
    type TrackMap,
    type RibbonPreview,
  } from "./api";

  let {
    lapId,
    slug = null,
    open = false,
    onClose,
    onSaved,
    pollMs = 2000,
    startMode = "edit",
    canEdit = true,
  }: {
    lapId: number | null; // null = no lap yet; with `slug` set, edit that track's owned map
    slug?: string | null; // Tracks-screen editing: load/save the map by track slug (no lap)
    open?: boolean;
    onClose?: () => void;
    onSaved?: () => void;
    pollMs?: number;
    startMode?: "view" | "edit"; // pane opens the modal in "view"; pencil -> "edit"
    canEdit?: boolean; // false = strictly read-only (no pencil/tools/save); editing lives in Track view
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

  // View vs edit. The pane opens the modal enlarged in "view" (read-only map + corner
  // table); the pencil switches to "edit" (tools + editable table). The effect resets
  // it from startMode each time the modal opens.
  let editMode = $state(true);
  $effect(() => {
    if (open) editMode = canEdit && startMode === "edit";
  });

  // What the pointer is currently dragging. `ai` = apex index for kind "apex".
  type DragKind = "corner" | "sector" | "apex" | "secstart" | "secend";
  let drag: { kind: DragKind; i: number; ai?: number } | null = null;
  // Reactive mirror of `drag` used to FREEZE the label layout mid-drag: the O(n)
  // label-routing pass was the drag lag, so we defer it until the marker is dropped.
  let dragging = $state(false);
  let addMode = $state(false);
  let addSectionMode = $state(false);

  // Orientation control. `angle` is the target the user is dialing in; the live
  // SVG preview rotates the fitted map by (angle - baked). `angle` is a PENDING
  // edit — nudging only updates the live preview; it persists on Save (see save()),
  // like corner/sector edits. Net displayed orientation = baked(map.rotation) +
  // preview(angle - map.rotation) = angle, so it reads 1×, matching what's entered.
  let angle = $state(0);
  function rotateBy(d: number) {
    angle = (((angle + d + 180) % 360) + 360) % 360 - 180;
  }
  function resetRotation() {
    angle = 0;
  }

  // --- "(Re)generate map layout": point-cloud capture -> ribbon preview.
  // Drive across the full track width (weave to both edges) over any number of
  // laps, then Build reconstructs the surface from the whole point cloud.
  type GenPhase = "off" | "capturing" | "building" | "preview";
  let genPhase: GenPhase = $state("off");
  let capLaps: { idx: number; samples: number; length: number }[] = $state([]);
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
    ribbon = null;
    genPhase = "capturing";
    stopPoll();
    pollTimer = setInterval(pollGen, pollMs);
  }

  async function pollGen() {
    try {
      capLaps = (await mapCaptureStatus()).laps;
    } catch {
      /* transient poll failure — keep waiting */
    }
  }

  async function buildGen() {
    if (capLaps.length < 1) return;
    stopPoll();
    genPhase = "building";
    genErr = null;
    try {
      ribbon = await buildRibbon();
      trackName = ribbon.track_name ?? "";
      genPhase = "preview";
    } catch {
      genErr = "Couldn't reconstruct the ribbon from those laps.";
      genPhase = "capturing";
      pollTimer = setInterval(pollGen, pollMs);
    }
  }

  // Re-run the reconstruction on the track's saved raw capture — no re-drive.
  async function rebuildGen() {
    genPhase = "building";
    genErr = null;
    ribbon = null;
    try {
      // Bind by slug when editing from the Tracks screen (no lap): otherwise the
      // sidecar rebuilds the LIVE track over the one on screen (#26).
      ribbon = await rebuildFromCapture(lapId, slug);
      trackName = ribbon.track_name ?? "";
      genPhase = "preview";
    } catch (e) {
      genPhase = "off";
      genErr = String(e).includes("422")
        ? "No saved capture for this track — use Generate map layout to record one."
        : "Rebuild failed.";
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

  // Persist the raw capture only -- keep this track's existing corner map, just
  // record the point cloud so cross-section metrics (width/banking/elevation)
  // can be extracted later. For a track you've already mapped (e.g. Laguna).
  async function saveCloudOnly() {
    saving = true;
    genErr = null;
    try {
      await saveMapCloud();
      resetGen();
      onClose?.();
    } catch {
      genErr = "Couldn't save the capture — need at least two passes with elevation.";
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
  const ribbonStart = $derived(ribbon ? startMarker(ribbon.center) : null);

  // Open with the SAVED map (avg=0) — the frozen/canonical ribbon if one exists.
  // The fresh re-average/rebuild path is only "(Re)generate map layout" (mapcapture).
  let loadedFor = -1;
  let loadedSlug: string | null = null;
  $effect(() => {
    if (!open) {
      loadedFor = -1;
      loadedSlug = null;
      // Closing mid-capture must not leave the server buffering real laps into
      // mapping mode (silent data loss) — same teardown as the × button.
      if (genPhase !== "off") cancelGen();
      return;
    }
    if (lapId == null) {
      if (slug) {
        // Tracks-screen editing: load this track's owned map by slug (no lap). {}
        // (unmapped track) leaves map null -> the capture panel offers to generate.
        if (loadedSlug === slug) return;
        loadedSlug = slug;
        loadedFor = -1;
        const s = slug;
        error = null;
        getTrackMap(s)
          .then((res) => {
            if (loadedSlug !== s) return;
            if ("path" in res && res.path.length > 0) applyMap(res);
            else map = null;
          })
          .catch(() => {
            if (loadedSlug !== s) return;
            map = null;
          });
        return;
      }
      // Capture-only mode (fresh/unmapped track): no lap map to load. Return
      // BEFORE reading genPhase so this effect doesn't re-run on genPhase changes
      // and cancel the capture the instant startGen sets it to "capturing".
      loadedFor = -1;
      return;
    }
    if (loadedFor === lapId) return;
    loadedFor = lapId;
    const id = lapId;
    error = null;
    getLapMap(id)
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
    trackName = res.track_name ?? "";
  }

  // Editable display name for the track. Editable in dev (curating) or for a
  // user-captured (unshipped) map; a shipped built-in's name is read-only in an install.
  let trackName = $state("");
  const nameEditable = $derived(!!(map?.curating || (map && !map.shipped)));

  // What backs the ribbon about to be saved. No elevation-lap warning: width/banking
  // are gated server-side on the cloud's real lateral spread (ribbon._MIN_SPREAD_M) and
  // saved as unknown without it, so a lap count says nothing about whether they are
  // right -- the old "< 2 elevation laps" warning fired where they were closest.
  const provenance = $derived.by(() => {
    const n = ribbon?.lap_count;
    return n ? `from ${n} lap${n === 1 ? "" : "s"}` : "";
  });

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

  // Scroll-wheel zoom (esp. for placing corners on long tracks like Le Mans). The
  // viewBox shrinks toward the cursor; because click→world mapping goes through
  // getScreenCTM, placement stays accurate at any zoom. Resets when the map changes.
  let view: Box | null = $state(null);
  const fullBox = $derived(map ? { x: -pad, y: -pad, w: map.width + pad * 2, h: map.height + pad * 2 } : null);
  const vb = $derived.by(() => {
    const b = view ?? fullBox;
    return b ? `${b.x} ${b.y} ${b.w} ${b.h}` : "0 0 100 100";
  });
  // Markers/labels scale by the zoom factor so they keep a ~constant on-screen
  // size — essential for placing corners in tight sections.
  const z = $derived(fullBox && view ? view.w / fullBox.w : 1);
  const suLive = $derived(u * z);
  // Debounce the size scale: the viewBox keeps zooming live, but the expensive
  // label re-layout only re-runs 250ms after scrolling stops (not per wheel tick).
  let suDeb = $state(0);
  const su = $derived(suDeb || suLive);
  $effect(() => { void map; view = null; suDeb = u; });
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
    // Pan on empty-space drag only: never while placing (add mode) or when the
    // pointer went down on an interactive marker (a <circle>/<rect> handle), so
    // dragging corners still works.
    const offPan = attachPan(svgEl, box, set, (e) => {
      if (addMode || addSectionMode) return false;
      const tag = (e.target as Element).tagName.toLowerCase();
      return tag !== "circle" && tag !== "rect";
    });
    return () => { offWheel(); offPan(); };
  });

  // World point on the racing line for a given lap_distance (for placing markers).
  function ptAtDist(d: number): [number, number] {
    if (!map) return [0, 0];
    return map.path[nearestDistIndex(map.dist, d)];
  }

  // Ring-placed labels: corners on their perpendicular (tight ring), sections routed around
  // the corner labels (same tight ring, expanding only when blocked). One combined layout so
  // sections avoid the corner label boxes; results are split back out for the template.
  function computeLaidOut() {
    const empty = { corners: [] as PlacedLabel[], sections: [] as PlacedLabel[] };
    if (!map || !map.path.length) return empty;
    const path = map.path, N = path.length, k = Math.max(2, Math.round(N * 0.02));
    const cx = map.width / 2, cy = map.height / 2, gap = su * 0.008;
    const centroidOut = (p: [number, number]): [number, number] => { let dx = p[0] - cx, dy = p[1] - cy; const d = Math.hypot(dx, dy) || 1; return [dx / d, dy / d]; };
    const cornerAnchors = corners.map((c) => ptAtDist(c.apex_dist));
    const sectionAnchors = sections.map((s) => sectionSeg(s).mid);
    const markers: [number, number][] = [...corners.flatMap((c) => c.apexes.map((ad) => ptAtDist(ad))), ...sectionAnchors];
    const items: LabelItem[] = [
      ...corners.map((c, i) => {
        const lo = localOutward(path, nearestDistIndex(map!.dist, c.apex_dist), k);
        const outward = lo.curv > 1e-4 ? [lo.ox, lo.oy] as [number, number] : centroidOut(cornerAnchors[i]);
        return { anchor: cornerAnchors[i], text: `T${c.label || c.number}`, outward, fontSize: su * 0.032, gap, wrap: false, priority: 0 };
      }),
      ...sections.map((s, i) => ({ anchor: sectionAnchors[i], text: s.name, outward: centroidOut(sectionAnchors[i]), fontSize: su * 0.027, gap, wrap: true, priority: 1 })),
    ];
    const res = layoutLabels(items, { bounds: [-pad, -pad, map.width + pad, map.height + pad], markers, line: path });
    return { corners: res.slice(0, corners.length), sections: res.slice(corners.length) };
  }
  // Recompute labels on any change EXCEPT while dragging a marker (the expensive
  // routing pass caused the drag lag). Reading `dragging` first and bailing means a
  // mid-drag corner move doesn't retrigger this; dropping the marker flips
  // `dragging` false, which re-runs it once with the final positions.
  let laidOut = $state<{ corners: PlacedLabel[]; sections: PlacedLabel[] }>({ corners: [], sections: [] });
  $effect(() => {
    if (dragging) return;
    laidOut = computeLaidOut();
  });
  const cornerLabels = $derived(laidOut.corners);
  const sectionLabels = $derived(laidOut.sections);

  // S/F marker + heading arrow: the shared startMarker, same as TrackMap.svelte.
  const startDir = $derived(map ? startMarker(map.path) : null);
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
      case "corner": {
        // A corner has no separate start/end handles, so its window must follow the
        // apex — otherwise dragging the apex out of a loaded corner's fixed window
        // makes the save fail ("apex_dist must be inside the window"). Translate the
        // window by the same delta so the apex stays put inside it.
        const c = corners[drag.i];
        const delta = d - c.apex_dist;
        c.apex_dist = d;
        c.apexes[0] = d; // primary apex stays synced to apex_dist
        if (c.start_dist != null) c.start_dist += delta;
        if (c.end_dist != null) c.end_dist += delta;
        break;
      }
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
    dragging = false; // unfreeze -> the label layout recomputes once, at rest
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
    dragging = true; // freeze the label layout until endDrag
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
    // Prefer the game's REAL S1/S2 boundaries (captured from a lap); fall back to
    // naive thirds only when none were captured. Keep just the in-range splits.
    const real = (map.detected_sectors ?? []).filter((d) => d > 0 && d < max).sort((a, b) => a - b);
    sectors = real.length ? real : [max / 3, (2 * max) / 3];
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
    if (lapId == null && !slug) return;
    saving = true;
    error = null;
    try {
      // Rotation is a pending edit too: bake a changed angle in first so the saved
      // corners sit on the rebaked path, then persist the corner map (which
      // preserves the just-written rotation). Route by lap or by slug (Tracks screen).
      if (map && angle !== (map.rotation ?? 0)) {
        if (lapId != null) await setMapRotation(lapId, angle);
        else await setTrackMapRotation(slug!, angle);
      }
      const payload = {
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
        ...(nameEditable && trackName.trim() ? { name: trackName.trim() } : {}),
      };
      const res = lapId != null ? await saveLapMap(lapId, payload) : await saveTrackMap(slug!, payload);
      applyMap(res);
      onSaved?.();
    } catch (e) {
      // Surface the server's reason (e.g. "corners must be a non-empty list")
      // instead of a bare "Save failed" so the fix is obvious.
      const m = /-> \d+: (.+)$/.exec(String((e as Error)?.message ?? ""));
      error = m ? `Save failed — ${m[1]}` : "Save failed.";
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
        {#if map && editMode && nameEditable}
          <input class="ttl-in" bind:value={trackName} placeholder="Track name" aria-label="Track name" />
        {:else}
          <h2>{trackName || (map ? "Untitled track" : editMode ? "Edit track map" : "Track map")}</h2>
        {/if}
        {#if map?.shipped}
          <span class="badge" title="Built-in layout — you can rename/annotate corners, but the layout ships with the app and updates only with the app.">built-in</span>
        {/if}
        {#if editMode && (map || ribbon)}
          <div class="rot" role="group" aria-label="Rotate map">
            <button type="button" aria-label="Rotate 90° left" onclick={() => rotateBy(-90)}>⟲</button>
            <input type="number" step="5" aria-label="Rotation degrees" bind:value={angle} />
            <span>°</span>
            <button type="button" aria-label="Rotate 90° right" onclick={() => rotateBy(90)}>⟳</button>
            <button type="button" onclick={resetRotation}>0°</button>
          </div>
        {/if}
        {#if map && canEdit}
          {#if editMode}
            <button type="button" class="pencil" aria-label="View map" title="View (hide edit tools)" onclick={() => (editMode = false)}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          {:else}
            <button type="button" class="pencil" aria-label="Edit map" title="Edit" onclick={() => (editMode = true)}>✎</button>
          {/if}
        {/if}
        <button type="button" class="x" aria-label="Close" onclick={close}>×</button>
      </header>

      {#if genPhase === "capturing" || genPhase === "building"}
        <div class="genpanel">
          <h3>Generate map layout</h3>
          {#if genPhase === "building"}
            <p class="step">Reconstructing the track surface…</p>
          {:else}
            <p class="step">After your out lap, drive three full laps — one lap hugging the
              <strong>left</strong> boundary, one lightly <strong>weaving</strong> lap, and one lap
              hugging the <strong>right</strong> boundary — then click Build.</p>
            <div class="tools">
              <button
                type="button"
                class="primary"
                onclick={buildGen}
                disabled={capLaps.length < 1}
              >Build</button>
              {#if map}
                <!-- This track already has a corner map: keep it, just save the
                     capture for cross-section metrics (no geometry rebuild). -->
                <button type="button" onclick={saveCloudOnly} disabled={saving || capLaps.length < 2}>
                  {saving ? "Saving…" : "Save capture only"}
                </button>
              {/if}
            </div>
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
              {@const b = sfBar(ribbonStart.x0, ribbonStart.y0, ribbonStart.sfAng, ru * 0.03)}
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
            viewBox={vb}
            style="touch-action:none"
            class:add={editMode && (addMode || addSectionMode)}
            class:ro={!editMode}
            role="application"
            aria-label="Track map — drag corner and sector markers"
            onpointerdown={editMode ? onSvgClick : undefined}
          >
            <g transform="rotate({mapPreviewRot}, {map.width / 2}, {map.height / 2})">
            <polyline
              points={map.path.map(([x, y]) => `${x},${y}`).join(" ")}
              fill="none"
              stroke="var(--muted)"
              stroke-width={su * 0.007}
              stroke-linejoin="round"
              stroke-linecap="round"
            />
            {#each sections as sec, i (i)}
              {@const seg = sectionSeg(sec)}
              {@const ps = ptAtDist(sec.start_dist)}
              {@const pe = ptAtDist(sec.end_dist)}
              <polyline class="section-band" points={seg.pts} fill="none" stroke="var(--good, #3a7)" stroke-width={su * 0.02} stroke-linecap="round" opacity="0.35" />
              {@const sl = sectionLabels[i]}
              {#if sl}
                {#if sl.leader}
                  <line class="lbl-leader" x1={sl.leader[0]} y1={sl.leader[1]} x2={sl.leader[2]} y2={sl.leader[3]} stroke="var(--good, #3a7)" stroke-width={su * 0.004} opacity="0.6" />
                {/if}
                <text class="section-lbl" text-anchor="middle" font-size={sl.fontSize} fill="var(--good, #3a7)" paint-order="stroke" stroke="var(--bg, #1a1a1a)" stroke-width={su * 0.012} stroke-linejoin="round">
                  {#each sl.lines as ln, li}<tspan x={sl.cx} y={sl.cy - (sl.lines.length - 1) * sl.fontSize * 0.51 + li * sl.fontSize * 1.02 + sl.fontSize * 0.34}>{ln}</tspan>{/each}
                </text>
              {/if}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              {#if editMode}
                <circle class="sec-handle" cx={ps[0]} cy={ps[1]} r={su * 0.012} fill="var(--good, #3a7)" role="slider" aria-label="{sec.name} start" aria-valuenow={sec.start_dist} tabindex="-1" onpointerdown={(e) => startDrag("secstart", i, e)}><title>{sec.name} start</title></circle>
                <!-- svelte-ignore a11y_no_static_element_interactions -->
                <circle class="sec-handle" cx={pe[0]} cy={pe[1]} r={su * 0.012} fill="var(--good, #3a7)" role="slider" aria-label="{sec.name} end" aria-valuenow={sec.end_dist} tabindex="-1" onpointerdown={(e) => startDrag("secend", i, e)}><title>{sec.name} end</title></circle>
              {/if}
            {/each}
            {#if startDir}
              {@const b = sfBar(startDir.x0, startDir.y0, startDir.sfAng, su * 0.03)}
              <line x1={b.x1} y1={b.y1} x2={b.x2} y2={b.y2} stroke="var(--text)" stroke-width={su * 0.008} />
              <polygon points={arrow(startDir.xj, startDir.yj, startDir.ang, su * 0.05, su * 0.028)} fill="var(--text)" />
              <text class="sf" x={startDir.x0} y={startDir.y0 - su * 0.04} font-size={su * 0.032} fill="var(--text)" text-anchor="middle">S/F</text>
            {/if}
            {#each sectors as s, i (i)}
              {@const p = ptAtDist(s)}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <rect
                class="sector"
                class:ro={!editMode}
                x={p[0] - su * 0.014}
                y={p[1] - su * 0.014}
                width={su * 0.028}
                height={su * 0.028}
                fill="var(--text)"
                role="slider"
                aria-label="Sector split {i + 1}"
                aria-valuenow={s}
                tabindex="-1"
                onpointerdown={editMode ? (e) => startDrag("sector", i, e) : undefined}
              ><title>Sector split {i + 1}</title></rect>
            {/each}
            {#each corners as c, i (c.number)}
              {#each c.apexes as ad, ai (ai)}
                {@const p = ptAtDist(ad)}
                <!-- svelte-ignore a11y_no_static_element_interactions -->
                <circle
                  class="marker"
                  class:ro={!editMode}
                  cx={p[0]}
                  cy={p[1]}
                  r={su * 0.014}
                  fill={ai === 0 ? "var(--accent)" : "var(--bg, #1a1a1a)"}
                  stroke="var(--accent)"
                  stroke-width={ai === 0 ? 0 : su * 0.005}
                  role="slider"
                  aria-label="{c.name} apex {ai + 1}"
                  aria-valuenow={ad}
                  tabindex="-1"
                  onpointerdown={editMode ? (e) => startDrag(ai === 0 ? "corner" : "apex", i, e, ai) : undefined}
                ><title>{c.name}</title></circle>
              {/each}
              {@const cl = cornerLabels[i]}
              {#if cl}
                {#if cl.leader}
                  <line class="lbl-leader" x1={cl.leader[0]} y1={cl.leader[1]} x2={cl.leader[2]} y2={cl.leader[3]} stroke="var(--accent)" stroke-width={su * 0.004} opacity="0.6" />
                {/if}
                <text class="corner-lbl" x={cl.cx} y={cl.cy + cl.fontSize * 0.34} text-anchor="middle" font-size={cl.fontSize} fill="var(--accent)" paint-order="stroke" stroke="var(--bg, #1a1a1a)" stroke-width={su * 0.012} stroke-linejoin="round">T{c.label || c.number}</text>
              {/if}
            {/each}
            </g>
          </svg>

          <aside class="side">
            {#if editMode}
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
                {#if map?.curating || !map?.shipped}
                  <!-- Layout tools: for user maps, and for built-ins only in dev
                       (curating) — an installed build can't rebuild a shipped layout. -->
                  <button type="button" onclick={startGen}>Generate map layout</button>
                  <button type="button" onclick={rebuildGen}>Rebuild from saved capture</button>
                {/if}
              </div>
            {/if}
            {#if corners.length === 0}
              <p class="hint">No corners mapped for this track yet.</p>
            {/if}
            <ul class="corners">
              {#each corners as c, i (c.number)}
                <li>
                  {#if editMode}
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
                  {:else}
                    <span class="num">T{c.label || c.number}</span>
                    <span class="name-ro">{c.name || "—"}</span>
                    <span class="dir" data-dir={c.direction}>{c.direction || "—"}</span>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if sections.length}
              <ul class="sections">
                {#each sections as s, i (i)}
                  <li>
                    {#if editMode}
                      <input class="name" bind:value={s.name} aria-label="Section name" />
                      <select bind:value={s.kind} aria-label="Section kind">
                        <option value="straight">straight</option>
                        <option value="complex">complex</option>
                        <option value="section">section</option>
                        <option value="drs">drs</option>
                      </select>
                      <button type="button" class="rm" aria-label="Remove section" onclick={() => removeSection(i)}>×</button>
                    {:else}
                      <span class="name-ro">{s.name || "—"}</span>
                      <span class="dir">{s.kind}</span>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          </aside>
        </div>
      {:else if lapId != null && !error}
        <p class="msg">Loading…</p>
      {:else if canEdit}
        <!-- No map loaded (fresh/unmapped track): offer the capture flow directly so
             the first map can be generated without an existing lap. -->
        <div class="genpanel">
          <h3>Generate map layout</h3>
          <p class="step">No saved layout for this track yet. Generate one by driving a few
            boundary passes — the left edge, a weave, then the right edge — or rebuild from a
            capture you already recorded.</p>
          <div class="tools">
            <button type="button" class="primary" onclick={startGen}>Generate map layout</button>
            <button type="button" onclick={rebuildGen}>Rebuild from saved capture</button>
          </div>
          {#if error}<p class="msg err">{error}</p>{/if}
        </div>
      {:else}
        <p class="msg">No map for this track yet.</p>
      {/if}

      {#if error && map && genPhase === "off"}<p class="msg err">{error}</p>{/if}
      {#if genErr}<p class="msg err">{genErr}</p>{/if}

      <footer>
        {#if genPhase !== "off"}
          <button type="button" onclick={cancelGen}>Cancel capture</button>
          {#if genPhase === "preview"}
            {#if provenance}
              <!-- Say what backs the geometry before it is committed: a map off 1-3
                   laps is thin (#41's Imola sector splits rest on a single lap). -->
              <span class="lapcount">{provenance}</span>
            {/if}
            <button type="button" class="primary" disabled={saving} onclick={saveGen}>
              {saving ? "Saving…" : "Save ribbon"}
            </button>
          {/if}
        {:else}
          <button type="button" onclick={close}>{editMode && map ? "Cancel" : "Close"}</button>
          {#if editMode && map}
            <button type="button" class="primary" disabled={saving || !map} onclick={save}>
              {saving ? "Saving…" : "Save"}
            </button>
          {/if}
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
  .lapcount {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
  }
  header h2 {
    margin: 0;
    font-size: 15px;
    margin-right: auto;
  }
  .ttl-in {
    font: inherit;
    font-size: 15px;
    font-weight: 600;
    margin-right: auto;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 3px 8px;
    min-width: 0;
    max-width: 320px;
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
  /* A custom background drops native rounding, leaving colored buttons square.
     Give .primary its own box (8px, app convention) to match the plain ones. */
  button.primary {
    background: var(--accent);
    color: #000;
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 7px 12px;
  }
  .badge {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--faint);
    border: 1px solid var(--muted);
    border-radius: 999px;
    padding: 2px 8px;
    white-space: nowrap;
  }
  .pencil {
    font-size: 15px;
    line-height: 1;
  }
  /* View mode: markers/sectors are shown but not draggable — no grab affordance. */
  .marker.ro,
  .sector.ro {
    cursor: default;
  }
  svg.ro {
    cursor: default;
  }
  .genpanel {
    padding: 20px 16px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 12px;
    max-width: 520px;
    margin: 0 auto;
  }
  .genpanel .tools {
    justify-content: center;
  }
  .genpanel h3 {
    margin: 0;
    font-size: 14px;
  }
  .genpanel .step {
    margin: 0;
    line-height: 1.5;
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
