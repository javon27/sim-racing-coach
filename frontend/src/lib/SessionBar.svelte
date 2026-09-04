<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { getStatus, getSettings, withRetry, type StatusResponse, type SettingsResponse } from "./api";

  let {
    retryTries = 8,
    retryDelayMs = 750,
    pollMs = 3000,
  }: { retryTries?: number; retryDelayMs?: number; pollMs?: number } = $props();

  let status: StatusResponse | null = $state(null);
  let units: SettingsResponse["units"] | null = $state(null);
  let phase: "connecting" | "ok" | "unreachable" = $state("connecting");
  let pollTimer: ReturnType<typeof setInterval> | undefined;

  async function poll() {
    try {
      const [s, settings] = await Promise.all([getStatus(), getSettings()]);
      status = s;
      units = settings.units;
    } catch {
      // transient poll failure - keep last good data, don't touch phase
    }
  }

  function startPolling() {
    if (pollTimer !== undefined) return;
    pollTimer = setInterval(poll, pollMs);
  }

  async function load() {
    phase = "connecting";
    try {
      const [s, settings] = await withRetry(
        () => Promise.all([getStatus(), getSettings()]),
        retryTries,
        retryDelayMs,
      );
      status = s;
      units = settings.units;
      phase = "ok";
      startPolling();
    } catch {
      phase = "unreachable";
    }
  }

  onMount(load);
  onDestroy(() => {
    if (pollTimer !== undefined) clearInterval(pollTimer);
  });

  // Game connection state for the header indicator.
  const gameStatus = $derived(
    status?.on_track
      ? { label: "On track", cls: "on" }
      : status?.lmu_connected
        ? { label: "Connected", cls: "idle" }
        : { label: "LMU offline", cls: "off" },
  );
</script>

<header class="bar">
  {#if phase === "connecting"}
    <div class="stat"><span class="v">Connecting to the coach server…</span></div>
  {:else if phase === "unreachable"}
    <div class="stat">
      <span class="v">Can't reach the coach server — is it running?</span>
      <button type="button" onclick={load}>Retry</button>
    </div>
  {:else}
    <div class="brand"><span class="dot"></span><b>SIMCOACH</b></div>
    <div class="game-status {gameStatus.cls}">
      <span class="ind"></span>{gameStatus.label}
    </div>
    <div class="stat"><span class="k">Track</span><span class="v">{status?.track ?? "—"}</span></div>
    <div class="stat"><span class="k">Car</span><span class="v">{status?.car ?? "—"}</span></div>
    <div class="stat"><span class="k">Units</span><span class="v">{units ?? "—"}</span></div>
    <div class="spacer"></div>
    <div class="stat"><span class="k">Fuel</span><span class="v">—</span></div>
    <div class="stat"><span class="k">Energy</span><span class="v">—</span></div>
    <div class="stat"><span class="k">Tier</span><span class="v">—</span></div>
  {/if}
</header>

<style>
  .game-status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--display);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 3px 9px;
    border-radius: 999px;
    border: 1px solid var(--line);
  }
  .game-status .ind {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }
  .game-status.on {
    color: var(--good, #2ecc71);
  }
  .game-status.idle {
    color: var(--muted, #888);
  }
  .game-status.off {
    color: var(--bad, #e74c3c);
  }
</style>
