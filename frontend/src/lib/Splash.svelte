<script lang="ts">
  // Branded boot gate. Rides out the sidecar's cold-start bind race, then warms
  // the endpoints the app hits first so the shell reveals already-loaded instead
  // of each pane popping in with its own "connecting" flicker.
  import { onMount } from "svelte";
  import { fade } from "svelte/transition";
  import { getHealth, getStatus, getSettings, getSessions, getChats, withRetry } from "./api";
  import NestorWordmark from "./NestorWordmark.svelte";

  let { onready }: { onready: () => void } = $props();

  let phase: "loading" | "error" = $state("loading");
  let step = $state("Connecting to the coach server…");

  async function boot() {
    phase = "loading";
    try {
      // PyInstaller onefile is slow to launch on first run; wait until it binds,
      // then prefetch the first-paint data against the now-warm server.
      step = "Connecting to the coach server…";
      await withRetry(getHealth, 24, 500);
      step = "Loading your garage…";
      await Promise.allSettled([getStatus(), getSettings(), getSessions(), getChats()]);
      onready();
    } catch {
      phase = "error";
    }
  }

  onMount(boot);
</script>

<div class="splash" out:fade={{ duration: 280 }}>
  <div class="stack">
    <div class="logo"><NestorWordmark size={108} /></div>
    <div class="tag">AI Sim Racing Coach</div>
    {#if phase === "loading"}
      <div class="step">{step}</div>
    {:else}
      <div class="err">Can't reach the coach server.</div>
      <button type="button" onclick={boot}>Retry</button>
    {/if}
  </div>
</div>

<style>
  .splash {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: grid;
    place-items: center;
    background:
      radial-gradient(120% 80% at 50% 30%, var(--surface) 0%, var(--bg) 70%);
  }
  .stack {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }
  .logo {
    margin-bottom: 4px;
    animation: rise 0.5s ease both;
  }
  .tag {
    font-family: var(--display);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: var(--faint);
  }
  .step {
    margin-top: 26px;
    font-size: 12px;
    color: var(--muted);
  }
  .err {
    margin-top: 26px;
    font-size: 13px;
    color: var(--bad);
  }
  button {
    margin-top: 12px;
    font-family: var(--display);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #fff;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    padding: 8px 18px;
    cursor: pointer;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .logo { animation: none; }
  }
</style>
