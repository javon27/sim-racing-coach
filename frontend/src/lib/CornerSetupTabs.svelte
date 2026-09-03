<script lang="ts">
  import CornerPanel from "./CornerPanel.svelte";
  import SetupPanel from "./SetupPanel.svelte";

  let { lapId }: { lapId: number | null } = $props();

  let activeTab: "corners" | "setup" = $state("corners");
</script>

<div class="tabs" role="tablist">
  <button
    type="button"
    class="tab"
    role="tab"
    aria-selected={activeTab === "corners"}
    class:on={activeTab === "corners"}
    onclick={() => (activeTab = "corners")}
  >
    Corners
  </button>
  <button
    type="button"
    class="tab"
    role="tab"
    aria-selected={activeTab === "setup"}
    class:on={activeTab === "setup"}
    onclick={() => (activeTab = "setup")}
  >
    Setup
  </button>
</div>

{#if activeTab === "corners"}
  <CornerPanel {lapId} />
{:else}
  <SetupPanel />
{/if}
