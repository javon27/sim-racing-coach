<script lang="ts">
  import { getSessionRace, type RaceResult } from "./api";

  let { sessionId }: { sessionId?: string } = $props();

  let race: RaceResult | null = $state(null);

  // `position` is the presence key: {} (no race data) or a fetch failure -> hidden.
  function hasResult(r: RaceResult | Record<string, never>): r is RaceResult {
    return "position" in r && r.position !== null && r.position !== undefined;
  }

  $effect(() => {
    const sid = sessionId;
    race = null;
    if (!sid) return;
    let stale = false;
    getSessionRace(sid)
      .then((r) => {
        if (!stale) race = hasResult(r) ? r : null;
      })
      .catch(() => {
        if (!stale) race = null;
      });
    return () => {
      stale = true;
    };
  });

  const posLabel = $derived.by(() => {
    if (!race) return "";
    const p = `P${race.position}`;
    return race.class_position != null && race.class_position !== race.position
      ? `${p} (class P${race.class_position})`
      : p;
  });
</script>

{#if race}
  <div class="race-strip">
    <span class="pos">{posLabel}</span>
    {#if race.finish_status}
      <span class="status">{race.finish_status}</span>
    {/if}
    {#if race.incidents.length > 0}
      <span class="inc">{race.incidents.length} incident{race.incidents.length === 1 ? "" : "s"}</span>
    {/if}
    {#if race.penalties.length > 0}
      <span class="pen">{race.penalties.length} penalt{race.penalties.length === 1 ? "y" : "ies"}</span>
    {/if}
  </div>
{/if}

<style>
  .race-strip {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 6px 12px;
    font-size: 12px;
    background: var(--surface-2, var(--surface));
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .pos {
    font-weight: 700;
    color: var(--accent);
  }
  .status {
    color: var(--good);
  }
  .inc,
  .pen {
    color: var(--muted);
  }
</style>
