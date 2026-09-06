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
  let step = $state("Warming up…");
  let progress = $state(0); // %; visual fill, deliberately decoupled from boot speed
  let done = $state(false); // switches the bar to the fast final-fill transition

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  async function boot() {
    phase = "loading";
    done = false;
    progress = 0;
    try {
      // Sweep the bar 0 -> 90% over ~1.1s no matter how fast the server is, so
      // the fill is always visible. In dev the sidecar is already warm and boot
      // is near-instant; without this the bar would just flash to 100.
      step = "Connecting to the coach server…";
      requestAnimationFrame(() => (progress = 90));
      const sweep = sleep(1100);

      // PyInstaller onefile is slow to launch on first run; wait until it binds,
      // then prefetch the first-paint data against the now-warm server.
      await withRetry(getHealth, 24, 500);
      step = "Loading your garage…";
      await Promise.allSettled([getStatus(), getSettings(), getSessions(), getChats()]);

      // Only complete the bar once BOTH the sweep and the real work are done.
      await sweep;
      done = true;
      progress = 100;
      await sleep(320); // let the 100% fill land before revealing
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
      <div class="bar"><span class:done style="width:{progress}%"></span></div>
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
    height: 100%;
    border-radius: 999px;
    background: var(--accent);
    /* Sweep to 90% over ~1.1s (matches the JS `sweep` timer), then .done swaps
       to a quick transition for the final fill to 100%. */
    transition: width 1.1s cubic-bezier(0.25, 0.8, 0.35, 1);
  }
  .bar span.done {
    transition: width 0.3s ease;
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
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .logo { animation: none; }
  }
</style>
