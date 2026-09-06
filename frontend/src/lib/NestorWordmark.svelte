<script lang="ts">
  // The NESTOR wordmark with the helmet mark standing in for the O — the name is
  // part of the full logo, so render it here everywhere the name appears.
  // `size` is the cap height in px; the helmet scales with it.
  import NestorMark from "./NestorMark.svelte";
  let { size = 17 }: { size?: number } = $props();
  const mark = $derived(Math.round(size * 1.12));
  // Outline width scales with the mark so it reads the same at 17px and 108px.
  const ol = $derived(Math.max(1, Math.round(size * 0.03)));
</script>

<span class="wm" style="font-size:{size}px">
  NEST<span class="o" style="--ol:{ol}px"><NestorMark size={mark} label="O" /></span>R
</span>

<style>
  .wm {
    display: inline-flex;
    align-items: center;
    font-family: var(--display);
    font-weight: 700;
    letter-spacing: 0.08em;
    line-height: 1;
    white-space: nowrap;
    color: var(--text);
  }
  .o {
    display: inline-flex;
    margin: 0 0.04em;
    transform: translateY(0.06em);
    /* White outline so the near-black shell separates from a dark surface.
       Transparent on light (the dark shell already reads there). */
    --olc: transparent;
  }
  .o :global(svg) {
    filter:
      drop-shadow(var(--ol) 0 0 var(--olc))
      drop-shadow(calc(-1 * var(--ol)) 0 0 var(--olc))
      drop-shadow(0 var(--ol) 0 var(--olc))
      drop-shadow(0 calc(-1 * var(--ol)) 0 var(--olc));
  }
  :root[data-theme="dark"] .o {
    --olc: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .o {
      --olc: #ffffff;
    }
  }
</style>
