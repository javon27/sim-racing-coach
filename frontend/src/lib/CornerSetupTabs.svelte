<script lang="ts">
  import CornerPanel from "./CornerPanel.svelte";
  import SetupPanel from "./SetupPanel.svelte";
  import TrackMap from "./TrackMap.svelte";
  import TrackMapEditor from "./TrackMapEditor.svelte";

  let { lapId }: { lapId: number | null } = $props();

  let activeTab: "corners" | "setup" | "map" = $state("corners");
  let editing = $state(false);
</script>

<div class="tabs">
  <button
    type="button"
    class="tab"
    aria-pressed={activeTab === "corners"}
    class:on={activeTab === "corners"}
    onclick={() => (activeTab = "corners")}
  >
    Corners
  </button>
  <button
    type="button"
    class="tab"
    aria-pressed={activeTab === "setup"}
    class:on={activeTab === "setup"}
    onclick={() => (activeTab = "setup")}
  >
    Setup
  </button>
  <button
    type="button"
    class="tab"
    aria-pressed={activeTab === "map"}
    class:on={activeTab === "map"}
    onclick={() => (activeTab = "map")}
  >
    Map
  </button>
</div>

{#if activeTab === "corners"}
  <CornerPanel {lapId} />
{:else if activeTab === "setup"}
  <SetupPanel />
{:else}
  {#if lapId !== null}
    <div class="map-actions">
      <button type="button" onclick={() => (editing = true)}>Edit map</button>
    </div>
    <TrackMapEditor lapId={lapId} open={editing} onClose={() => (editing = false)} />
  {/if}
  <TrackMap {lapId} />
{/if}

<style>
  .map-actions {
    display: flex;
    justify-content: flex-end;
    padding: 8px 12px 0;
  }
</style>
