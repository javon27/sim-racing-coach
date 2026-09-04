<script lang="ts">
  let { text }: { text: string } = $props();
</script>

<button type="button" class="info" aria-label={text} title={text}>
  <span class="mark" aria-hidden="true">&#9432;</span>
  <span class="bubble" role="tooltip">{text}</span>
</button>

<style>
  .info {
    position: relative;
    display: inline-flex;
    align-items: center;
    margin-left: 5px;
    padding: 0;
    background: none;
    border: none;
    cursor: help;
  }
  .mark {
    color: var(--faint);
    font-size: 12px;
    line-height: 1;
  }
  .info:hover .mark,
  .info:focus .mark {
    color: var(--muted);
  }
  .bubble {
    position: absolute;
    left: 50%;
    bottom: calc(100% + 6px);
    transform: translateX(-50%);
    width: max-content;
    max-width: 220px;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 9px;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.35;
    white-space: normal;
    z-index: 10;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.1s;
  }
  .info:hover .bubble,
  .info:focus .bubble {
    opacity: 1;
  }
</style>
