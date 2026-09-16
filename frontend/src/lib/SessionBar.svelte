<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { getSettings, withRetry, type StatusResponse, type SettingsResponse } from "./api";
  import { eventFeed } from "./events";
  import NestorWordmark from "./NestorWordmark.svelte";

  let {
    retryTries = 8,
    retryDelayMs = 750,
  }: { retryTries?: number; retryDelayMs?: number } = $props();

  let status: StatusResponse | null = $state(null);
  let units: SettingsResponse["units"] | null = $state(null);
  let phase: "connecting" | "ok" | "unreachable" = $state("connecting");

  // #106: status used to be a 2 s getStatus() poll (with a 60 s settings throttle
  // bolted on top for #98). Status now arrives on the shared /events feed, so the
  // only thing left to fetch is the settings payload, which nothing streams: one
  // shot on mount, plus the `settings-changed` event a save already dispatches.
  async function loadSettings() {
    try {
      units = (await withRetry(getSettings, retryTries, retryDelayMs)).units;
    } catch {
      // keep the last known units; reachability is the feed's story, not this fetch's
    }
  }

  // "Unreachable" is a feed story now: the socket is not delivering and nothing
  // has arrived within the same grace the old retry loop had (tries x delay).
  let deadTimer: ReturnType<typeof setTimeout> | undefined;
  function clearDead() {
    if (deadTimer !== undefined) clearTimeout(deadTimer);
    deadTimer = undefined;
  }
  function armDead() {
    clearDead();
    deadTimer = setTimeout(() => {
      deadTimer = undefined;
      phase = "unreachable";
    }, retryTries * retryDelayMs);
  }

  $effect(() =>
    eventFeed.status.subscribe((s) => {
      if (s === null) return; // nothing has arrived yet
      status = s;
      phase = "ok";
      clearDead();
    }),
  );
  $effect(() =>
    eventFeed.connected.subscribe((live) => {
      if (live) clearDead();
      else armDead();
    }),
  );

  // The socket reconnects itself; Retry just re-arms the header and re-reads
  // settings, so a driver who fixed the server sees it clear without a restart.
  function load() {
    phase = "connecting";
    armDead();
    loadSettings();
  }

  onMount(() => {
    loadSettings();
    window.addEventListener("settings-changed", loadSettings);
  });
  onDestroy(() => {
    window.removeEventListener("settings-changed", loadSettings);
    clearDead();
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
  <div class="stat"><span class="k">Car</span><span class="v">{status?.car_model ?? status?.car ?? "—"}</span></div>
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
