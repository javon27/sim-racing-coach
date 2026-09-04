<script lang="ts">
  import {
    getSettings,
    putSettings,
    setApiKey,
    testApiKey,
    clearData,
    ingestHistory,
    type SettingsResponse,
    type IngestResult,
  } from "./api";
  import { nextTheme, applyTheme, THEME_LABELS, type ThemeMode } from "./theme";

  const THEME_KEY = "simcoach-theme";

  let settings: SettingsResponse | null = $state(null);
  let phase: "loading" | "ok" | "error" = $state("loading");

  let theme: ThemeMode = $state("system");

  let apiKeyInput = $state("");
  let keyStatus: string | null = $state(null);
  let testStatus: string | null = $state(null);

  let newPlayerName = $state("");

  let confirmingReset = $state(false);
  let resetStatus: string | null = $state(null);

  let importStatus: string | null = $state(null);
  let importing = $state(false);

  $effect(() => {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      theme = stored;
      applyTheme(theme);
    }
    (async () => {
      try {
        settings = await getSettings();
        phase = "ok";
      } catch {
        phase = "error";
      }
    })();
  });

  function saveConfig() {
    if (!settings) return;
    const cfg = {
      units: settings.units,
      corner_naming: settings.corner_naming,
      input_device: settings.input_device,
      model: settings.model,
      driver: settings.driver,
      lmu_dir: settings.lmu_dir,
      lmu_player_names: settings.lmu_player_names,
      log_level: settings.log_level,
    };
    putSettings(cfg).then((res) => {
      settings = res;
    });
  }

  function toggleTheme() {
    theme = nextTheme(theme);
    applyTheme(theme);
    localStorage.setItem(THEME_KEY, theme);
  }

  async function saveKey() {
    if (!apiKeyInput.trim()) return;
    const res = await setApiKey(apiKeyInput);
    apiKeyInput = "";
    keyStatus = "Key saved.";
    if (settings) settings = { ...settings, api_key_set: res.api_key_set };
  }

  async function runTestKey() {
    testStatus = null;
    const res = await testApiKey();
    testStatus = res.ok ? "✓ valid" : res.error ?? "Key test failed.";
  }

  function addPlayerName() {
    const name = newPlayerName.trim();
    if (!name || !settings) return;
    settings = { ...settings, lmu_player_names: [...settings.lmu_player_names, name] };
    newPlayerName = "";
  }

  function removePlayerName(i: number) {
    if (!settings) return;
    settings = { ...settings, lmu_player_names: settings.lmu_player_names.filter((_, idx) => idx !== i) };
  }

  function saveLmuSettings() {
    saveConfig();
  }

  async function runImport() {
    importing = true;
    importStatus = null;
    try {
      const res: IngestResult = await ingestHistory();
      importStatus = `Imported ${res.imported} sessions, ${res.unmatched} unmatched`;
    } finally {
      importing = false;
    }
  }

  async function confirmReset() {
    const res = await clearData();
    resetStatus = res.cleared ? "Data cleared." : "Reset failed.";
    confirmingReset = false;
  }
</script>

<div class="settings">
  <div class="settings-inner">
  {#if phase === "loading"}
    <p class="empty">Loading settings…</p>
  {:else if phase === "error"}
    <p class="empty">Could not load settings.</p>
  {:else if settings}
    <section class="grp">
      <h3>Display</h3>
      <div class="row">
        <span class="lbl">Units</span>
        <select bind:value={settings.units} onchange={saveConfig}>
          <option value="metric">Metric</option>
          <option value="imperial">Imperial</option>
        </select>
      </div>
      <div class="row">
        <span class="lbl">Theme</span>
        <button type="button" onclick={toggleTheme}>{THEME_LABELS[theme]}</button>
      </div>
      <div class="row">
        <span class="lbl">Corner naming</span>
        <select bind:value={settings.corner_naming} onchange={saveConfig}>
          <option value="names">Names</option>
          <option value="numbers">Numbers</option>
        </select>
      </div>
    </section>

    <section class="grp">
      <h3>Driving</h3>
      <div class="row">
        <span class="lbl">Input device</span>
        <select bind:value={settings.input_device} onchange={saveConfig}>
          <option value="wheel">Wheel</option>
          <option value="controller">Controller</option>
        </select>
      </div>
    </section>

    <section class="grp">
      <h3>AI / Coach</h3>
      <div class="row">
        <span class="lbl">Model</span>
        <select bind:value={settings.model} onchange={saveConfig}>
          <option value="opus">Opus</option>
          <option value="sonnet">Sonnet</option>
          <option value="haiku">Haiku</option>
        </select>
      </div>
      <div class="row">
        <span class="lbl">API key</span>
        <span class="badge">{settings.api_key_set ? "set ✓" : "not set"}</span>
      </div>
      <div class="row">
        <input
          type="password"
          aria-label="API key"
          placeholder="sk-ant-..."
          bind:value={apiKeyInput}
        />
        <button type="button" onclick={saveKey}>Save key</button>
        <button type="button" onclick={runTestKey}>Test key</button>
      </div>
      {#if keyStatus}<p class="status">{keyStatus}</p>{/if}
      {#if testStatus}<p class="status">{testStatus}</p>{/if}
    </section>

    <section class="grp">
      <h3>Driver profile</h3>
      <div class="row">
        <span class="lbl">Braking style</span>
        <input type="text" bind:value={settings.driver.braking_style} onchange={saveConfig} />
      </div>
      <div class="row">
        <span class="lbl">Balance preference</span>
        <input type="text" bind:value={settings.driver.balance_preference} onchange={saveConfig} />
      </div>
      <div class="row">
        <span class="lbl">Risk tolerance</span>
        <input type="text" bind:value={settings.driver.risk_tolerance} onchange={saveConfig} />
      </div>
    </section>

    <section class="grp">
      <h3>LMU history</h3>
      <div class="row">
        <span class="lbl">LMU dir</span>
        <input type="text" bind:value={settings.lmu_dir} />
      </div>
      <div class="row names">
        <span class="lbl">Player names</span>
        <ul>
          {#each settings.lmu_player_names as name, i (name + i)}
            <li>
              {name}
              <button type="button" aria-label={`remove ${name}`} onclick={() => removePlayerName(i)}
                >&times;</button
              >
            </li>
          {/each}
        </ul>
      </div>
      <div class="row">
        <input type="text" placeholder="add alias…" bind:value={newPlayerName} />
        <button type="button" onclick={addPlayerName}>Add</button>
      </div>
      <div class="row">
        <button type="button" onclick={saveLmuSettings}>Save LMU settings</button>
        <button type="button" onclick={runImport} disabled={importing}>
          {importing ? "Importing…" : "Import history"}
        </button>
      </div>
      {#if importStatus}<p class="status">{importStatus}</p>{/if}
    </section>

    <section class="grp">
      <h3>Data</h3>
      <div class="row">
        <span class="lbl">Data dir</span>
        <span class="val">{settings.data_dir}</span>
      </div>
      <div class="row">
        <span class="lbl">Logging</span>
        <select bind:value={settings.log_level} onchange={saveConfig}>
          <option value="INFO">Normal</option>
          <option value="DEBUG">Verbose</option>
        </select>
      </div>
      <div class="row">
        {#if confirmingReset}
          <span class="warn">Erase all laps, chats, and telemetry?</span>
          <button type="button" class="danger" onclick={confirmReset}>Confirm</button>
          <button type="button" onclick={() => (confirmingReset = false)}>Cancel</button>
        {:else}
          <button type="button" class="danger" onclick={() => (confirmingReset = true)}>
            Reset / clear data
          </button>
        {/if}
      </div>
      {#if resetStatus}<p class="status">{resetStatus}</p>{/if}
    </section>

    <section class="grp">
      <h3>About</h3>
      <div class="row">
        <span class="lbl">App version</span>
        <span class="val">{settings.app_version}</span>
      </div>
      <div class="row">
        <span class="lbl">LMU build</span>
        <span class="val">{settings.lmu_build ?? "unknown"}</span>
      </div>
      <div class="row">
        <span class="lbl">API key</span>
        <span class="val">{settings.api_key_set ? "set ✓" : "not set"}</span>
      </div>
    </section>
  {/if}
  </div>
</div>

<style>
  .settings {
    overflow-y: auto;
    height: 100%;
  }
  .settings-inner {
    max-width: 680px;
    margin: 0 auto;
    padding: 20px 26px;
  }
  .empty {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
  }
  .grp {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 16px;
  }
  .grp h3 {
    margin: 0 0 10px;
    font-family: var(--display);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--faint);
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 6px 0;
  }
  .row.names {
    align-items: flex-start;
  }
  .lbl {
    color: var(--muted);
    font-size: 13px;
  }
  .val {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 13px;
  }
  .badge {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--good);
  }
  select,
  input[type="text"],
  input[type="password"] {
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 10px;
  }
  button {
    font-family: var(--display);
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.02em;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 12px;
    cursor: pointer;
  }
  button.danger {
    background: var(--bad-soft);
    color: var(--bad);
    border-color: var(--bad);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  ul li {
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 3px 6px;
    font-family: var(--mono);
    font-size: 12px;
  }
  ul li button {
    background: none;
    border: none;
    padding: 0 2px;
    color: var(--faint);
  }
  .status {
    margin: 6px 0 0;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
  .warn {
    color: var(--bad);
    font-size: 13px;
  }
</style>
