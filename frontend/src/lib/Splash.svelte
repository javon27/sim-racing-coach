<script lang="ts">
  // Branded boot gate. Rides out the sidecar's cold-start bind race, then warms
  // the endpoints the app hits first so the shell reveals already-loaded instead
  // of each pane popping in with its own "connecting" flicker.
  import { onMount } from "svelte";
  import { fade } from "svelte/transition";
  import { getHealth, getStatus, getSettings, getSessions, getChats, withRetry } from "./api";
  import NestorMark from "./NestorMark.svelte";

  let { onready }: { onready: () => void } = $props();

  let phase: "loading" | "error" = $state("loading");
  let step = $state("Warming up…");

  async function boot() {
    phase = "loading";
    try {
      // PyInstaller onefile is slow to launch on first run; wait until it binds.
      step = "Connecting to the coach server…";
      await withRetry(getHealth, 24, 500);
      // Prefetch the first-paint data against the now-warm server.
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
    <div class="mark"><NestorMark size={104} label="Nestor" /></div>
    <div class="wm">NESTOR</div>
    <div class="tag">AI Sim Racing Coach</div>
    {#if phase === "loading"}
      <div class="bar"><span></span></div>
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
  .mark {
    margin-bottom: 14px;
    animation: rise 0.5s ease both;
  }
  .wm {
    font-family: var(--display);
    font-weight: 700;
    font-size: 30px;
    letter-spacing: 0.14em;
    color: var(--text);
  }
  .tag {
    font-family: var(--display);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: var(--faint);
  }
  .bar {
    margin-top: 26px;
    width: 168px;
    height: 3px;
    border-radius: 999px;
    background: var(--line);
    overflow: hidden;
  }
  .bar span {
    display: block;
    width: 40%;
    height: 100%;
    border-radius: 999px;
    background: var(--accent);
    animation: slide 1.15s ease-in-out infinite;
  }
  .step {
    margin-top: 12px;
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
  @keyframes slide {
    0% { transform: translateX(-110%); }
    100% { transform: translateX(310%); }
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .mark { animation: none; }
    .bar span { animation-duration: 2.4s; }
  }
</style>
