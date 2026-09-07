<script lang="ts">
  import { CLASS_COLOR, type FieldSnapshot, type VehicleState } from "./live";

  let { snapshot }: { snapshot: FieldSnapshot | null } = $props();

  type Mode = "position" | "deltas";
  const KEY = "pitwall.tower";
  let mode: Mode = $state(
    ((): Mode => {
      try {
        return (localStorage.getItem(KEY) as Mode) || "position";
      } catch {
        return "position";
      }
    })(),
  );
  function setMode(m: Mode) {
    mode = m;
    try {
      localStorage.setItem(KEY, m);
    } catch {
      /* private mode */
    }
  }

  const byPlace = $derived(
    snapshot
      ? [...snapshot.cars].sort((a, b) => {
          const rank = (p: number) => (p > 0 ? p : Number.POSITIVE_INFINITY);
          return rank(a.place) - rank(b.place);
        })
      : [],
  );

  const rows = $derived.by(() => {
    if (mode === "position") return byPlace;
    const i = byPlace.findIndex((c) => c.is_player);
    if (i < 0) return byPlace;
    return byPlace.slice(Math.max(0, i - 3), i + 4);
  });

  function gapText(c: VehicleState): string {
    if (mode === "deltas") {
      if (c.laps_behind_next > 0) return `+${c.laps_behind_next}L`;
      return c.gap_next != null && c.gap_next > 0 ? `+${c.gap_next.toFixed(1)}` : "—";
    }
    if (c.place === 1) return "Leader";
    if (c.laps_behind_leader > 0) return `+${c.laps_behind_leader}L`;
    return c.gap_leader != null ? `+${c.gap_leader.toFixed(1)}` : "—";
  }
</script>

<div class="tower">
  <div class="tower-h">
    <span>Live timing</span>
    <div class="toggle">
      <button
        class:on={mode === "position"}
        onclick={() => setMode("position")}
      >
        Position
      </button>
      <button class:on={mode === "deltas"} onclick={() => setMode("deltas")}>
        Deltas
      </button>
    </div>
  </div>
  {#each rows as c (c.id)}
    <div
      class="trow"
      class:you={c.is_player}
      style="--cls:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}"
    >
      <div class="pos">{c.place}</div>
      <div class="drv">
        {c.is_player ? "You" : c.driver}
        <span
          class="chip"
          style="color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'};border-color:{CLASS_COLOR[c.cls] ?? 'var(--faint)'}"
          >{c.cls}</span
        >
        <small>{c.car}</small>
      </div>
      <div class="gap">{gapText(c)}</div>
    </div>
  {/each}
</div>

<style>
  .tower {
    border-left: 1px solid var(--line);
    background: var(--surface);
    overflow-y: auto;
    min-height: 0;
  }
  .tower-h {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 12px 14px 10px;
    font-family: var(--display, inherit);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--faint);
  }
  .toggle {
    display: flex;
    gap: 4px;
  }
  .toggle button {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 3px 8px;
    border-radius: 6px;
    border: 1px solid var(--line);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }
  .toggle button.on {
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-soft);
  }
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
  .gap {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
</style>
