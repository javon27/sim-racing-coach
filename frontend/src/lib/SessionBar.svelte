<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { getStatus, getSettings, withRetry, type StatusResponse, type SettingsResponse } from "./api";
  import NestorWordmark from "./NestorWordmark.svelte";

  let {
    retryTries = 8,
    retryDelayMs = 750,
    pollMs = 2000,
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

  // Game connection state for the header indicator. Prefer the live driving
  // state (track/pits/garage) so it flips the moment the car's state changes,
  // then fall back to connection state.
  const gameStatus = $derived.by(() => {
    switch (status?.activity) {
      case "track":
        return { label: "On track", cls: "on" };
      case "pits":
        return { label: "Pit lane", cls: "warn" };
      case "garage":
        return { label: "Garage", cls: "warn" };
    }
    return status?.lmu_connected
      ? { label: "In menus", cls: "idle" }
      : { label: "LMU offline", cls: "off" };
  });
</script>

<header class="bar">
  <div class="brand">
    <NestorWordmark size={17} />
    <span class="sub">AI Sim Racing Coach</span>
  </div>
  {#if phase === "ok"}
    <div class="game-status {gameStatus.cls}">
      <span class="ind"></span>{gameStatus.label}
    </div>
  {:else if phase === "connecting"}
    <div class="game-status idle"><span class="ind"></span>Connecting…</div>
  {:else}
    <button type="button" class="game-status off" onclick={load} title="Retry">
      <span class="ind"></span>No server · Retry
    </button>
  {/if}
  <div class="stat"><span class="k">Track</span><span class="v">{status?.track ?? "—"}</span></div>
  <div class="stat"><span class="k">Car</span><span class="v">{status?.car ?? "—"}</span></div>
  <div class="stat"><span class="k">Units</span><span class="v">{units ?? "—"}</span></div>
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
  button.game-status {
    background: none;
    cursor: pointer;
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
  .game-status.warn {
    color: var(--warn, #b45c09);
  }
  .game-status.off {
    color: var(--bad, #e74c3c);
  }
</style>
