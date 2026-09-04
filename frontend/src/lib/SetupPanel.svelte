<script lang="ts">
  import { getSetup } from "./api";

  let setup: Record<string, unknown> = $state({});
  let phase: "loading" | "ok" | "empty" | "none" = $state("loading");

  $effect(() => {
    getSetup()
      .then((res) => {
        setup = res;
        if (Object.keys(res).length === 0) {
          phase = "empty";
        } else {
          phase = adjustable(res).length === 0 ? "none" : "ok";
        }
      })
      .catch(() => {
        setup = {};
        phase = "empty";
      });
  });

  // Mirror simcoach/tiers.py adjustable_from_setup: only entries the coach
  // considers changeable (isFreeSetting === true). Everything else is junk
  // for this car (N/A, Fixed, Non-adjustable, available:false).
  function adjustable(s: Record<string, unknown>): [string, unknown][] {
    return Object.entries(s).filter(
      ([, v]) =>
        !!v && typeof v === "object" && (v as { isFreeSetting?: unknown }).isFreeSetting === true,
    );
  }

  // Show the human-readable stringValue; fall back to value then String(v).
  function displayValue(v: unknown): string {
    if (v && typeof v === "object") {
      const o = v as { stringValue?: unknown; value?: unknown };
      if (o.stringValue !== undefined && o.stringValue !== null) return String(o.stringValue);
      if (o.value !== undefined && o.value !== null) return String(o.value);
    }
    return String(v);
  }

  // Curated names for the worst abbreviations; unlisted keys fall through to
  // the generic prettify (matches tiers.py: strip VM_, _->space, Title Case).
  const NAMES: Record<string, string> = {
    VM_ANTILOCKBRAKESYSTEMMAP: "ABS Map",
    VM_ANTILOCK_BRAKES: "ABS",
    VM_FRONT_ANTISWAY: "Front Anti-roll Bar",
    VM_REAR_ANTISWAY: "Rear Anti-roll Bar",
    VM_FRONT_TOEIN: "Front Toe",
    VM_REAR_TOEIN: "Rear Toe",
    VM_DIFF_PRELOAD: "Diff Preload",
    VM_ENGINE_MIXTURE: "Engine Mixture",
    VM_FRONT_TIRE_COMPOUND: "Front Tyre Compound",
    VM_REAR_TIRE_COMPOUND: "Rear Tyre Compound",
    VM_FUEL_LEVEL: "Fuel Level",
    VM_BRAKE_PRESSURE: "Brake Pressure",
    VM_BRAKE_BALANCE: "Brake Balance",
    VM_FRONT_WING: "Front Wing",
    VM_REAR_WING: "Rear Wing",
  };

  function label(key: string): string {
    if (NAMES[key]) return NAMES[key];
    return key
      .replace(/^VM_/, "")
      .replace(/_/g, " ")
      .toLowerCase()
      .split(" ")
      .map((w) => (w ? w.charAt(0).toUpperCase() + w.slice(1) : w))
      .join(" ")
      .trim();
  }
</script>

<div class="panel">
  {#if phase === "loading"}
    <p class="empty">Loading…</p>
  {:else if phase === "empty"}
    <p class="empty">No setup — LMU not connected</p>
  {:else if phase === "none"}
    <p class="empty">No adjustable settings for this car.</p>
  {:else}
    {#each adjustable(setup) as [key, value] (key)}
      <div class="setrow">
        <span class="lbl">{label(key)}</span>
        <span class="val">{displayValue(value)}</span>
      </div>
    {/each}
  {/if}
</div>
