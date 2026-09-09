<script lang="ts">
  import {
    getSettings,
    putSettings,
    setApiKey,
    testApiKey,
    clearData,
    ingestHistory,
    getStatus,
    getProfile,
    putProfileSummary,
    addMemory,
    updateMemory,
    distillProfile,
    type SettingsResponse,
    type IngestResult,
    type DriverProfileData,
    type DriverMemory,
  } from "./api";
  import InfoTip from "./InfoTip.svelte";

  const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

  let browseStatus: string | null = $state(null);

  let settings: SettingsResponse | null = $state(null);
  let phase: "loading" | "ok" | "error" = $state("loading");

  let apiKeyInput = $state("");
  let keyStatus: string | null = $state(null);
  let testStatus: string | null = $state(null);

  let newPlayerName = $state("");

  let confirmingReset = $state(false);
  let resetStatus: string | null = $state(null);

  let importStatus: string | null = $state(null);
  let importing = $state(false);
  let lmuStatus: string | null = $state(null);

  $effect(() => {
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
      show_cost: settings.show_cost,
      input_device: settings.input_device,
      model: settings.model,
      driver: settings.driver,
      lmu_dir: settings.lmu_dir,
      lmu_player_names: settings.lmu_player_names,
      log_level: settings.log_level,
    };
    return putSettings(cfg).then((res) => {
      settings = res;
    });
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

  async function saveLmuSettings() {
    lmuStatus = "Saving…";
    try {
      await saveConfig();
      lmuStatus = "Saved.";
    } catch {
      lmuStatus = "Save failed.";
    }
  }

  async function browseLmuDir() {
    if (!settings) return;
    browseStatus = null;
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const path = await open({ directory: true, multiple: false });
      if (typeof path === "string") {
        settings.lmu_dir = path;
        saveConfig();
      }
    } catch (e) {
      browseStatus = "Could not open folder picker.";
    }
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

  // Display-only hint; keep in sync with store.active_learnings' default `limit`
  // (simcoach/store.py). Not an enforced constraint — just shows active vs cap.
  const MEMORY_CAP = 50;

  let profile: DriverProfileData | null = $state(null);
  let currentSessionId: string | null = $state(null);
  let summaryText = $state("");
  let summaryStatus: "pending" | "active" = $state("active");
  let newMemoryText = $state("");
  let editingMemoryId: number | null = $state(null);
  let editingText = $state("");
  let distilling = $state(false);
  let distillStatus: string | null = $state(null);

  $effect(() => {
    (async () => {
      try {
        const [p, st] = await Promise.all([getProfile(), getStatus()]);
        profile = p;
        summaryText = p.summary.text;
        summaryStatus = p.summary.status;
        currentSessionId = st.session_id;
      } catch {
        /* profile section just stays empty if this fails */
      }
    })();
  });

  async function saveSummary() {
    // A proposed (pending) summary is persisted only via the explicit Approve
    // action -- auto-saving on blur here would silently overwrite the single
    // active driver_summary row with unreviewed text.
    if (summaryStatus !== "active") return;
    const res = await putProfileSummary(summaryText, summaryStatus);
    if (profile) profile = { ...profile, summary: res };
  }

  async function approveSummary() {
    const res = await putProfileSummary(summaryText, "active");
    if (profile) profile = { ...profile, summary: res };
    summaryStatus = "active";
  }

  function patchMemoryLists(fn: (list: DriverMemory[]) => DriverMemory[]) {
    if (!profile) return;
    profile = {
      ...profile,
      memories: { active: fn(profile.memories.active), pending: fn(profile.memories.pending) },
    };
  }

  async function approveMemory(m: DriverMemory) {
    await updateMemory(m.id, { status: "active" });
    if (!profile) return;
    profile = {
      ...profile,
      memories: {
        active: [...profile.memories.active, { ...m, status: "active" }],
        pending: profile.memories.pending.filter((x) => x.id !== m.id),
      },
    };
  }

  async function retireMemory(m: DriverMemory, fromList: "active" | "pending") {
    await updateMemory(m.id, { status: "retired" });
    if (!profile) return;
    profile = {
      ...profile,
      memories: { ...profile.memories, [fromList]: profile.memories[fromList].filter((x) => x.id !== m.id) },
    };
  }

  function startEditMemory(m: DriverMemory) {
    editingMemoryId = m.id;
    editingText = m.text;
  }

  async function saveMemoryEdit() {
    if (editingMemoryId == null) return;
    const id = editingMemoryId;
    const text = editingText;
    await updateMemory(id, { text });
    patchMemoryLists((list) => list.map((m) => (m.id === id ? { ...m, text } : m)));
    editingMemoryId = null;
  }

  async function addNewMemory() {
    const text = newMemoryText.trim();
    if (!text) return;
    const res = await addMemory({ text });
    newMemoryText = "";
    if (!profile) return;
    const item: DriverMemory = {
      id: res.id,
      text: res.text,
      status: res.status,
      track: res.track,
      car: res.car,
      source: "manual",
      session_id: null,
      created_ts: Date.now() / 1000,
      updated_ts: Date.now() / 1000,
    };
    const list = res.status === "active" ? "active" : "pending";
    profile = { ...profile, memories: { ...profile.memories, [list]: [...profile.memories[list], item] } };
  }

  async function reviewSessionLearnings() {
    if (!currentSessionId) {
      distillStatus = "No active session to review.";
      return;
    }
    distilling = true;
    distillStatus = null;
    try {
      const res = await distillProfile(currentSessionId);
      if (profile) {
        const proposed: DriverMemory[] = res.memories.map((m) => ({
          id: m.id,
          text: m.text,
          status: "pending",
          track: m.track,
          car: m.car,
          source: "distill",
          session_id: currentSessionId,
          created_ts: Date.now() / 1000,
          updated_ts: Date.now() / 1000,
        }));
        profile = {
          ...profile,
          memories: { ...profile.memories, pending: [...profile.memories.pending, ...proposed] },
        };
      }
      summaryText = res.summary;
      summaryStatus = "pending";
      distillStatus = "New proposals ready for review below.";
    } finally {
      distilling = false;
    }
  }
</script>

<div class="settings">
  <div class="settings-inner">
  {#if phase === "loading"}
    <p class="empty">Loading settings…</p>
  {:else if phase === "error"}
    <p class="empty">Could not load settings.</p>
  {:else if settings}
    <header class="page-head">
      <h2>Settings</h2>
      <p>How the coach reads your laps, talks to you, and stores your data.</p>
    </header>

    <section class="grp">
      <h3>Display</h3>
      <div class="field">
        <span class="lbl">Units<InfoTip text="Metric (km/h, °C) or imperial (mph, °F) for coach + telemetry readouts." /></span>
        <div class="control">
          <select bind:value={settings.units} onchange={saveConfig}>
            <option value="metric">Metric</option>
            <option value="imperial">Imperial</option>
          </select>
        </div>
      </div>
    </section>

    <section class="grp">
      <h3>AI / Coach</h3>
      <div class="field">
        <span class="lbl">Model<InfoTip text="Which Claude model answers — Opus (deepest), Sonnet (balanced), Haiku (fastest)." /></span>
        <div class="control">
          <select bind:value={settings.model} onchange={saveConfig}>
            <option value="opus">Opus</option>
            <option value="sonnet">Sonnet</option>
            <option value="haiku">Haiku</option>
          </select>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Cost estimate<InfoTip text="Show the estimated $ next to the chat's token count. Token counts always show; this only hides the dollar figure." /></span>
        <div class="control">
          <input
            type="checkbox"
            aria-label="Cost estimate"
            bind:checked={settings.show_cost}
            onchange={saveConfig}
          />
        </div>
      </div>
      <div class="field">
        <span class="lbl">API key<InfoTip text="Your Anthropic API key. Stored in the OS keyring, never in a file. Save, then Test to verify." /></span>
        <div class="control">
          <input
            type="password"
            aria-label="API key"
            placeholder="sk-ant-..."
            bind:value={apiKeyInput}
          />
          <span class="badge" class:set={settings.api_key_set}>{settings.api_key_set ? "set ✓" : "not set"}</span>
          <div class="btns">
            <button type="button" class="primary" onclick={saveKey}>Save key</button>
            <button type="button" onclick={runTestKey}>Test key</button>
          </div>
        </div>
      </div>
      {#if keyStatus || testStatus}
        <p class="status">{[keyStatus, testStatus].filter(Boolean).join(" · ")}</p>
      {/if}
    </section>

    <section class="grp">
      <h3>Driver Memories</h3>
      <div class="field">
        <span class="lbl">Braking style<InfoTip text="Part of your driver profile — e.g. trail-braker vs early-and-off. The coach factors this in." /></span>
        <div class="control">
          <input type="text" placeholder="e.g. trail-braker" bind:value={settings.driver.braking_style} onchange={saveConfig} />
        </div>
      </div>
      <div class="field">
        <span class="lbl">Balance preference<InfoTip text="Part of your driver profile — e.g. prefer understeer vs oversteer. The coach factors this in." /></span>
        <div class="control">
          <input type="text" placeholder="e.g. slight oversteer" bind:value={settings.driver.balance_preference} onchange={saveConfig} />
        </div>
      </div>
      <div class="field">
        <span class="lbl">Risk tolerance<InfoTip text="Part of your driver profile — e.g. conservative vs aggressive. The coach factors this in." /></span>
        <div class="control">
          <input type="text" placeholder="e.g. aggressive" bind:value={settings.driver.risk_tolerance} onchange={saveConfig} />
        </div>
      </div>

      {#if profile}
        <div class="field mem-summary">
          <span class="lbl">Summary<InfoTip text="The concise, always-on impression of you the coach carries into every conversation." /></span>
          <div class="control mem-summary-control">
            <textarea rows="2" bind:value={summaryText} onchange={saveSummary}></textarea>
            {#if summaryStatus === "pending"}
              <div class="mem-row-actions">
                <span class="badge pending">proposed</span>
                <button type="button" class="primary" onclick={approveSummary}>Approve summary</button>
              </div>
            {/if}
          </div>
        </div>

        <div class="mem-section">
          <div class="mem-head">
            <span class="lbl">Memories</span>
            <span class="status inline">{profile.memories.active.length} / {MEMORY_CAP} active</span>
          </div>
          {#if profile.memories.active.length === 0}
            <p class="help">No memories yet.</p>
          {:else}
            <ul class="mem-list">
              {#each profile.memories.active as m (m.id)}
                <li class="mem-row">
                  {#if editingMemoryId === m.id}
                    <input type="text" bind:value={editingText} />
                    <div class="mem-row-actions">
                      <button type="button" class="primary" onclick={saveMemoryEdit}>Save</button>
                      <button type="button" onclick={() => (editingMemoryId = null)}>Cancel</button>
                    </div>
                  {:else}
                    <p class="mem-text">{m.text}</p>
                    <div class="mem-meta">
                      {#if m.track}<span class="badge">{m.track}</span>{/if}
                      {#if m.car}<span class="badge">{m.car}</span>{/if}
                      <span class="badge">{m.source}</span>
                    </div>
                    <div class="mem-row-actions">
                      <button type="button" onclick={() => startEditMemory(m)}>Edit</button>
                      <button type="button" class="danger" onclick={() => retireMemory(m, "active")}>Delete</button>
                    </div>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
          <div class="addrow">
            <input
              type="text"
              placeholder="add a memory…"
              bind:value={newMemoryText}
              onkeydown={(e) => e.key === "Enter" && addNewMemory()}
            />
            <button type="button" onclick={addNewMemory}>+ Add memory</button>
          </div>
        </div>

        {#if profile.memories.pending.length > 0}
          <div class="mem-section">
            <div class="mem-head"><span class="lbl">Pending review</span></div>
            <ul class="mem-list">
              {#each profile.memories.pending as m (m.id)}
                <li class="mem-row">
                  {#if editingMemoryId === m.id}
                    <input type="text" bind:value={editingText} />
                    <div class="mem-row-actions">
                      <button type="button" class="primary" onclick={saveMemoryEdit}>Save</button>
                      <button type="button" onclick={() => (editingMemoryId = null)}>Cancel</button>
                    </div>
                  {:else}
                    <p class="mem-text">{m.text}</p>
                    <div class="mem-meta">
                      {#if m.track}<span class="badge">{m.track}</span>{/if}
                      {#if m.car}<span class="badge">{m.car}</span>{/if}
                      <span class="badge">{m.source}</span>
                    </div>
                    <div class="mem-row-actions">
                      <button type="button" class="primary" onclick={() => approveMemory(m)}>Approve</button>
                      <button type="button" onclick={() => startEditMemory(m)}>Edit</button>
                      <button type="button" class="danger" onclick={() => retireMemory(m, "pending")}>Reject</button>
                    </div>
                  {/if}
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="actions">
          <button type="button" onclick={reviewSessionLearnings} disabled={distilling}>
            {distilling ? "Reviewing…" : "Review session learnings"}
          </button>
        </div>
        {#if distillStatus}<p class="status">{distillStatus}</p>{/if}
      {/if}
    </section>

    <section class="grp">
      <h3>LMU history</h3>
      <div class="field">
        <span class="lbl">LMU dir<InfoTip text="Folder where LMU stores UserData (result logs). Auto-detected; override if needed." /></span>
        <div class="control">
          <input type="text" placeholder="Auto-detected" bind:value={settings.lmu_dir} />
          {#if isTauri}
            <button type="button" onclick={browseLmuDir}>Browse…</button>
          {/if}
        </div>
      </div>
      {#if browseStatus}<p class="status">{browseStatus}</p>{/if}
      <div class="field">
        <span class="lbl">Player names<InfoTip text="Your in-game driver name(s), used to pick your own laps out of result logs." /></span>
        <div class="control names">
          {#if settings.lmu_player_names?.length}
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
          {/if}
          <div class="addrow">
            <input type="text" placeholder="add alias…" bind:value={newPlayerName} />
            <button type="button" onclick={addPlayerName}>Add</button>
          </div>
        </div>
      </div>
      <p class="help">
        <strong>Import history</strong> scans LMU's result logs in the folder above and
        backfills your past sessions — practice, qualifying and races for your player
        name(s) — with lap times, sector splits, and race results (finishing position,
        incidents). It reads finished-session logs only; it doesn't capture live laps and
        skips sessions already imported.
      </p>
      <div class="actions">
        <button type="button" onclick={saveLmuSettings}>Save LMU settings</button>
        <button type="button" class="primary" onclick={runImport} disabled={importing}>
          {importing ? "Importing…" : "Import history"}
        </button>
        {#if lmuStatus}<span class="status inline">{lmuStatus}</span>{/if}
      </div>
      {#if importStatus}<p class="status">{importStatus}</p>{/if}
    </section>

    <section class="grp">
      <h3>Data</h3>
      <div class="field">
        <span class="lbl">Data dir<InfoTip text="Where SimCoach stores its database, telemetry, and logs." /></span>
        <div class="control"><span class="val">{settings.data_dir}</span></div>
      </div>
      <div class="field">
        <span class="lbl">Logging<InfoTip text="Verbose adds DEBUG detail to the log file for troubleshooting." /></span>
        <div class="control">
          <select bind:value={settings.log_level} onchange={saveConfig}>
            <option value="INFO">Normal</option>
            <option value="DEBUG">Verbose</option>
          </select>
        </div>
      </div>
      <div class="danger-zone">
        {#if confirmingReset}
          <span class="warn">Erase all laps, chats, and telemetry?</span>
          <div class="btns">
            <button type="button" class="danger" onclick={confirmReset}>Confirm</button>
            <button type="button" onclick={() => (confirmingReset = false)}>Cancel</button>
          </div>
        {:else}
          <div>
            <span class="lbl">Reset all data</span>
            <p class="sub">Erases every lap, chat, and telemetry file. Can't be undone.</p>
          </div>
          <button type="button" class="danger" onclick={() => (confirmingReset = true)}>
            Reset / clear data
          </button>
        {/if}
      </div>
      {#if resetStatus}<p class="status">{resetStatus}</p>{/if}
    </section>

    <section class="grp">
      <h3>About</h3>
      <div class="field">
        <span class="lbl">App version</span>
        <div class="control"><span class="val">{settings.app_version}</span></div>
      </div>
      <div class="field">
        <span class="lbl">LMU build</span>
        <div class="control"><span class="val">{settings.lmu_build ?? "unknown"}</span></div>
      </div>
      <div class="field">
        <span class="lbl">API key</span>
        <div class="control"><span class="val">{settings.api_key_set ? "set ✓" : "not set"}</span></div>
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
    max-width: 720px;
    margin: 0 auto;
    padding: 28px 26px 48px;
  }
  .empty {
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
  }

  .page-head {
    margin: 0 0 22px;
  }
  .page-head h2 {
    margin: 0;
    font-family: var(--display);
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .page-head p {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 13px;
  }

  .grp {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: var(--shadow);
    padding: 6px 20px 14px;
    margin-bottom: 18px;
  }
  /* Section title sits on its own baseline row above a hairline, so the aligned
     field grid below reads as one block. */
  .grp h3 {
    margin: 0;
    padding: 14px 0 12px;
    font-family: var(--display);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--accent);
  }

  /* The core fix: every row is a fixed label column + a shared control column,
     so all controls line up on one left edge instead of scattering right. */
  .field {
    display: grid;
    grid-template-columns: 172px minmax(0, 1fr);
    align-items: center;
    gap: 8px 18px;
    padding: 11px 0;
    border-top: 1px solid var(--line);
  }
  .lbl {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    color: var(--text);
    font-size: 13px;
    font-weight: 500;
  }
  .control {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  .control.names {
    flex-direction: column;
    align-items: stretch;
  }
  /* A lone control fills the column up to a sane cap so selects and text inputs
     share one width; buttons/badges keep their natural size beside it. */
  .control > select,
  .control > input[type="text"],
  .control > input[type="password"] {
    flex: 1 1 200px;
    min-width: 0;
    max-width: 340px;
  }

  .val {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 13px;
    color: var(--text);
    word-break: break-all;
  }
  .badge {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 2px 9px;
  }
  .badge.set {
    color: var(--good);
    border-color: var(--good);
    background: var(--good-soft);
  }

  select,
  input[type="text"],
  input[type="password"],
  textarea {
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 7px 10px;
  }
  input::placeholder {
    color: var(--faint);
  }
  select:focus,
  input:focus,
  textarea:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  textarea {
    width: 100%;
    resize: vertical;
    font-family: inherit;
  }

  /* Driver Memories: summary + active/pending lists. */
  .mem-summary-control {
    flex-direction: column;
    align-items: stretch;
  }
  .mem-section {
    padding: 11px 0;
    border-top: 1px solid var(--line);
  }
  .mem-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .mem-list {
    list-style: none;
    margin: 0 0 10px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .mem-row {
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 10px 12px;
  }
  .mem-text {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--text);
  }
  .mem-meta {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .mem-row-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .badge.pending {
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .btns {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
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
    padding: 7px 13px;
    cursor: pointer;
  }
  button:hover:not(:disabled) {
    border-color: var(--faint);
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  button.primary {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  button.primary:hover:not(:disabled) {
    background: var(--accent-2);
    border-color: var(--accent-2);
  }
  :root[data-theme="dark"] button.primary {
    color: #160f2b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) button.primary {
      color: #160f2b;
    }
  }
  button.danger {
    background: var(--bad-soft);
    color: var(--bad);
    border-color: var(--bad);
  }

  /* Player-name aliases: chips wrap, add-row sits under them within the column. */
  .names ul {
    list-style: none;
    margin: 0 0 4px;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .names li {
    display: flex;
    align-items: center;
    gap: 5px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 3px 6px 3px 11px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .names li button {
    background: none;
    border: none;
    padding: 0 3px;
    font-size: 15px;
    line-height: 1;
    color: var(--faint);
  }
  .names li button:hover {
    color: var(--bad);
  }
  .addrow {
    display: flex;
    gap: 8px;
  }
  .addrow input {
    flex: 1 1 auto;
    min-width: 0;
    max-width: 340px;
  }

  /* Per-section action bar: buttons grouped, right-aligned, above a hairline. */
  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-top: 14px;
    margin-top: 4px;
    border-top: 1px solid var(--line);
    justify-content: flex-end;
  }

  .danger-zone {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 0 4px;
    margin-top: 4px;
    border-top: 1px solid var(--line);
  }
  .danger-zone .sub {
    margin: 2px 0 0;
    font-size: 12px;
    color: var(--muted);
  }

  .status {
    margin: 10px 0 0;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
  .status.inline {
    margin: 0;
  }
  .help {
    margin: 14px 0 0;
    font-size: 12px;
    line-height: 1.55;
    color: var(--muted);
  }
  .help strong {
    color: var(--text);
    font-weight: 600;
  }
  .warn {
    color: var(--bad);
    font-size: 13px;
    font-weight: 500;
  }

  /* Stack label over control on narrow widths so nothing gets crushed. */
  @media (max-width: 540px) {
    .field {
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .control > select,
    .control > input[type="text"],
    .control > input[type="password"] {
      max-width: none;
    }
  }
</style>
