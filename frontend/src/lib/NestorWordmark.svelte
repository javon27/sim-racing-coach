<script lang="ts">
  // The NESTOR wordmark with the helmet mark standing in for the O — the name is
  // part of the full logo, so render it here everywhere the name appears.
  // `size` is the cap height in px; the helmet scales with it.
  import NestorMark from "./NestorMark.svelte";
  let { size = 17 }: { size?: number } = $props();
  const mark = $derived(Math.round(size * 1.12));
</script>

<span class="wm" style="font-size:{size}px">
  NEST<span class="o"><NestorMark size={mark} label="O" /></span>R
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
  }
</style>
