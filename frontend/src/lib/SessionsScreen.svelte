<script lang="ts">
  // Ported from docs/design/v1-ui-prototype.html #s-sessions. The prototype's
  // mock has game/cfg/cls/type fields the real /sessions payload doesn't --
  // this builds against the real SessionRow shape (session_id, track, car,
  // laps, best, started, last) and filters/groups by track + car only.
  import { onMount } from "svelte";
  import { getSessions, getSessionChats, withRetry, type SessionRow } from "./api";
  import TrendChart from "./TrendChart.svelte";

  let { onselectSession }: { onselectSession?: (sessionId: string, chatId?: string) => void } = $props();

  // Open a session: resolve its most-recent linked chat (if any) and hand both up.
  async function selectSession(sessionId: string) {
    let chatId: string | undefined;
    try {
      const linked = await getSessionChats(sessionId);
      chatId = linked[0]?.chat_id;
    } catch {
      chatId = undefined;
    }
    onselectSession?.(sessionId, chatId);
  }

  const ALL = "All";

  let sessions: SessionRow[] = $state([]);
  let loaded = $state(false);
  let selectedTrack = $state(ALL);
  let selectedCar = $state(ALL);

  onMount(async () => {
    // Retry through the sidecar startup race — all screens mount at app start,
    // so this fetch can fire before the server is bound (like LapList/SessionBar).
    try {
      const res = await withRetry(getSessions);
      sessions = Array.isArray(res) ? res : [];
    } catch {
      sessions = [];
    }
    loaded = true;
  });

  function formatBest(seconds: number | null): string {
    if (seconds == null) return "—";
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  function formatDate(epochSeconds: number): string {
    return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  }

  const tracks = $derived(
    [...new Set(sessions.map((s) => s.track))].sort((a, b) => a.localeCompare(b)),
  );
  const cars = $derived([...new Set(sessions.map((s) => s.car))]);

  const filtered = $derived(
    sessions.filter(
      (s) => (selectedTrack === ALL || s.track === selectedTrack) && (selectedCar === ALL || s.car === selectedCar),
    ),
  );

  const filteredCars = $derived([...new Set(filtered.map((s) => s.car))]);

  const bests = $derived(filtered.map((s) => s.best).filter((b): b is number => b != null));

  const personalBest = $derived(bests.length ? Math.min(...bests) : null);

  const improvement = $derived.by(() => {
    if (filteredCars.length !== 1) return null;
    const byDate = [...filtered].filter((s) => s.best != null).sort((a, b) => a.started - b.started);
    if (byDate.length < 2) return null;
    return byDate[0].best! - byDate[byDate.length - 1].best!;
  });

  const consistency = $derived(bests.length >= 2 ? (Math.max(...bests) - Math.min(...bests)) / 2 : null);

  const chartSeries = $derived(
    selectedTrack === ALL
      ? []
      : filteredCars.map((car) => ({
          name: car,
          points: filtered
            .filter((s) => s.car === car && s.best != null)
            .sort((a, b) => a.started - b.started)
            // x = the session start timestamp so same-day sessions each get their
            // own slot (spread out); label shows the date.
            .map((s) => ({ x: s.started, label: formatDate(s.started), value: s.best as number })),
        })),
  );
</script>

<div class="page">
  <div class="page-h">
    <h1>Sessions</h1>
    <div class="filters">
      <select aria-label="Track" bind:value={selectedTrack}>
        <option value={ALL}>All tracks</option>
        {#each tracks as t}
          <option value={t}>{t}</option>
        {/each}
      </select>
      <select aria-label="Car" bind:value={selectedCar}>
        <option value={ALL}>All cars</option>
        {#each cars as c}
          <option value={c}>{c}</option>
        {/each}
      </select>
    </div>
  </div>

  {#if loaded && sessions.length === 0}
    <p class="empty">No sessions yet &mdash; drive some laps or import your history in Settings.</p>
  {:else}
    <div class="trend-wrap">
      <div class="chart-card">
        <h3>Best lap</h3>
        <div class="cap">lower is better</div>
        <TrendChart series={chartSeries} placeholder="Select a track to see the best-lap trend" />
      </div>

      <div class="stat-cards">
        <div class="scard">
          <div class="k">Personal best</div>
          <div class="v">{personalBest != null ? formatBest(personalBest) : "—"}</div>
        </div>
        <div class="scard">
          <div class="k">Improvement</div>
          <div class="v" class:good={improvement != null && improvement > 0}>
            {improvement != null ? `${improvement >= 0 ? "−" : "+"}${Math.abs(improvement).toFixed(3)}s` : "—"}
          </div>
        </div>
        <div class="scard">
          <div class="k">Sessions</div>
          <div class="v">{filtered.length}</div>
        </div>
        <div class="scard">
          <div class="k">{filteredCars.length > 1 ? "Cars" : "Consistency"}</div>
          <div class="v">
            {filteredCars.length > 1 ? filteredCars.length : consistency != null ? `±${consistency.toFixed(3)}s` : "—"}
          </div>
        </div>
      </div>

      <div class="sess-list-h">Recorded sessions <span>&middot; {filtered.length}</span></div>
      <div class="sess-list">
        {#if filtered.length === 0}
          <p class="empty">No sessions match these filters.</p>
        {:else}
          {#each filtered as s (s.session_id)}
            <div class="srow" role="button" tabindex="0" onclick={() => selectSession(s.session_id)} onkeydown={(e) => e.key === "Enter" && selectSession(s.session_id)}>
              <div class="d">{formatDate(s.started)}</div>
              <div class="tk">{s.track}<div class="ln"><small>{s.car}</small></div></div>
              <div class="lp">{s.laps} laps</div>
              <div class="bt">{formatBest(s.best)}</div>
            </div>
          {/each}
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    height: 100%;
    overflow-y: auto;
    padding: 24px 30px;
  }
  .page-h {
    max-width: 1040px;
    margin: 0 auto 20px;
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .page-h h1 {
    font-family: var(--display);
    font-size: 20px;
    font-weight: 700;
    margin: 0;
  }
  .filters {
    margin-left: auto;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  select {
    font-family: var(--body);
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 8px 11px;
    cursor: pointer;
  }
  select:focus {
    outline: none;
    border-color: var(--accent);
  }
  .empty {
    max-width: 1040px;
    margin: 40px auto;
    text-align: center;
    color: var(--faint);
    font-family: var(--mono);
    font-size: 13px;
  }
  .trend-wrap {
    max-width: 1000px;
    margin: 0 auto;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: var(--shadow);
    margin-bottom: 16px;
  }
  .chart-card h3 {
    font-family: var(--display);
    font-size: 14px;
    font-weight: 600;
    margin: 0 0 2px;
  }
  .chart-card .cap {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    margin-bottom: 12px;
  }
  .stat-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
  }
  .scard {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .scard .k {
    font-family: var(--display);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--faint);
  }
  .scard .v {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 20px;
    margin-top: 4px;
  }
  .scard .v.good {
    color: var(--good);
  }
  .sess-list-h {
    font-family: var(--display);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--faint);
    margin: 26px 0 10px;
  }
  .sess-list-h span {
    font-family: var(--mono);
    color: var(--muted);
  }
  .sess-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .srow {
    display: grid;
    grid-template-columns: 80px 1fr auto auto;
    gap: 14px;
    align-items: center;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 11px 15px;
    cursor: pointer;
  }
  .srow:hover {
    border-color: var(--accent);
  }
  .srow .d {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
  }
  .srow .tk {
    font-weight: 600;
    font-size: 13px;
  }
  .srow .tk small {
    color: var(--muted);
    font-weight: 500;
  }
  .srow .tk .ln {
    margin-top: 2px;
  }
  .srow .lp {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
  .srow .bt {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }
</style>
