<script lang="ts">
  import CornerPanel from "./CornerPanel.svelte";
  import SetupPanel from "./SetupPanel.svelte";

  let { lapId }: { lapId: number | null } = $props();

  let activeTab: "corners" | "setup" = $state("corners");
  // SetupPanel fetches on mount; only mount it once the Setup tab is first
  // opened (then keep it mounted forever, so issue #36's no-remount fix
  // still holds on every switch after that).
  let setupVisited = $state(false);

  function selectTab(tab: "corners" | "setup") {
    activeTab = tab;
    if (tab === "setup") setupVisited = true;
  }
</script>

<div class="tabs">
  <button
    type="button"
    class="tab"
    aria-pressed={activeTab === "corners"}
    class:on={activeTab === "corners"}
    onclick={() => selectTab("corners")}
  >
    Corners
  </button>
  <button
    type="button"
    class="tab"
    aria-pressed={activeTab === "setup"}
    class:on={activeTab === "setup"}
    onclick={() => selectTab("setup")}
  >
    Setup
  </button>
</div>


<div class="tab-pane" hidden={activeTab !== "corners"}>
  <CornerPanel {lapId} />
</div>
{#if setupVisited}
  <div class="tab-pane" hidden={activeTab !== "setup"}>
    <SetupPanel />
  </div>
{/if}
