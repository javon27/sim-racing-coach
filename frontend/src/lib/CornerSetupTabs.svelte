<script lang="ts">
  import CornerPanel from "./CornerPanel.svelte";
  import SetupPanel from "./SetupPanel.svelte";
  import TrackMap from "./TrackMap.svelte";

  let { lapId }: { lapId: number | null } = $props();

  let activeTab: "corners" | "setup" | "map" = $state("corners");
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
  <TrackMap {lapId} />
{/if}
