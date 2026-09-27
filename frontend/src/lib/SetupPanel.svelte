<script lang="ts">
  import { getSetup, captureSetup, getLapSetup, getSessionSetup, type LapSetup } from "./api";
  import { formatTime } from "./format";

  // #188: a selected lap shows the setup it was driven on; a reviewed session with no
  // lap selected shows its fastest lap's; neither -> the live setup, as before.
  let { lapId = null, sessionId }: { lapId?: number | null; sessionId?: string } = $props();

  let setup: Record<string, unknown> = $state({});
  let phase: "loading" | "ok" | "empty" | "none" | "unrecorded" | "error" = $state("loading");
  let heading = $state("");
  let scope: "lap" | "session" = $state("lap");
  let seq = 0; // drops a response that a newer selection has superseded
  let retryTimer: ReturnType<typeof setTimeout> | undefined;

  function clearRetry() {
    if (retryTimer !== undefined) {
      clearTimeout(retryTimer);
      retryTimer = undefined;
    }
  }

  function showStored(res: Partial<LapSetup>) {
    const lap =
      res.lap_id == null
        ? ""
        : `${res.lap_no != null ? `Lap ${res.lap_no} · ` : "Lap "}${formatTime(res.lap_time ?? null)}`;
    heading =
      scope === "lap" || !lap
        ? lap
        : `${res.fastest ? "fastest lap" : "fastest lap with a recorded setup"} (${lap})`;
    setup = res.setup ?? {};
    phase = !res.setup ? "unrecorded" : adjustable(res.setup).length === 0 ? "none" : "ok";
  }

  $effect(() => {
    const my = ++seq;
    phase = "loading";
    heading = "";
    clearRetry();
    const isLap = lapId != null;
    const stored = isLap ? getLapSetup(lapId!) : sessionId ? getSessionSetup(sessionId) : null;
    if (stored) {
      scope = isLap ? "lap" : "session";
      stored
        .then((res) => {
          if (my !== seq) return;
          showStored(res);
          // #189: the pool links a lap's setup ~1-4s after the lap commits, so a
          // lap selected inside that window shows "unrecorded" and never
          // refreshes on its own. Retry once, only for a lap selection (never
          // session/live) -- a selection change or unmount cancels it below.
          if (isLap && phase === "unrecorded") {
            retryTimer = setTimeout(() => {
              retryTimer = undefined;
              getLapSetup(lapId!)
                .then((r) => my === seq && showStored(r))
                .catch(() => {
                  if (my === seq) phase = "error";
                });
            }, 3000);
          }
        })
        .catch(() => {
          if (my === seq) phase = "error";
        });
      return clearRetry;
    }
    getSetup()
      .then((res) => {
        if (my !== seq) return;
        setup = res;
        if (Object.keys(res).length === 0) {
          phase = "empty";
        } else {
          phase = adjustable(res).length === 0 ? "none" : "ok";
          // Trigger persistence of what the sim currently reports (#51: GET
          // /setup no longer writes; the server re-reads the setup itself
          // rather than trusting this response body). Fire-and-forget --
          // must never block or fail the panel.
          captureSetup().catch(() => {});
        }
      })
      .catch(() => {
        if (my !== seq) return;
        setup = {};
        phase = "empty";
      });
    return clearRetry;
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
  {:else if phase === "unrecorded"}
    <p class="empty">{heading ? `${heading}: no` : "No"} setup recorded for this {scope}.</p>
  {:else if phase === "error"}
    <p class="empty">Couldn't load the setup.</p>
  {:else if phase === "empty"}
    <p class="empty">No setup — LMU not connected</p>
  {:else if phase === "none"}
    <p class="empty">No adjustable settings for this car.</p>
  {:else}
    {#if heading}<p class="empty">Setup on {heading}</p>{/if}
    {#each adjustable(setup) as [key, value] (key)}
      <div class="setrow">
        <span class="lbl">{label(key)}</span>
        <span class="val">{displayValue(value)}</span>
      </div>
    {/each}
  {/if}
</div>
