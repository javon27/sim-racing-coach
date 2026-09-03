<script lang="ts">
  import { onMount } from "svelte";
  import { getStatus, getSettings, type StatusResponse, type SettingsResponse } from "./api";

  let status: StatusResponse | null = $state(null);
  let units: SettingsResponse["units"] | null = $state(null);
  let unreachable = $state(false);

  onMount(async () => {
    try {
      const [s, settings] = await Promise.all([getStatus(), getSettings()]);
      status = s;
      units = settings.units;
    } catch {
      unreachable = true;
    }
  });
</script>

<header class="bar">
  {#if unreachable}
    <div class="stat"><span class="k">Status</span><span class="v">Server not reachable</span></div>
  {:else}
    <div class="brand"><span class="dot"></span><b>PIT WALL</b></div>
    <div class="stat"><span class="k">Track</span><span class="v">{status?.track ?? "—"}</span></div>
    <div class="stat"><span class="k">Car</span><span class="v">{status?.car ?? "—"}</span></div>
    <div class="stat"><span class="k">Units</span><span class="v">{units ?? "—"}</span></div>
    <div class="spacer"></div>
    <div class="stat"><span class="k">Fuel</span><span class="v">—</span></div>
    <div class="stat"><span class="k">Energy</span><span class="v">—</span></div>
    <div class="stat"><span class="k">Tier</span><span class="v">—</span></div>
  {/if}
</header>
