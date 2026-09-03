<script lang="ts">
  import { getSetup } from "./api";

  let setup: Record<string, unknown> = $state({});
  let phase: "loading" | "ok" | "empty" = $state("loading");

  $effect(() => {
    getSetup()
      .then((res) => {
        setup = res;
        phase = Object.keys(res).length === 0 ? "empty" : "ok";
      })
      .catch(() => {
        setup = {};
        phase = "empty";
      });
  });
</script>

<div class="panel">
  {#if phase === "loading"}
    <p class="empty">Loading…</p>
  {:else if phase === "empty"}
    <p class="empty">No setup — LMU not connected</p>
  {:else}
    {#each Object.entries(setup) as [key, value] (key)}
      <div class="setrow">
        <span class="lbl">{key}</span>
        <span class="val">{value}</span>
      </div>
    {/each}
  {/if}
</div>
