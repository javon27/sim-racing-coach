<script lang="ts">
  import { onMount } from "svelte";
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
    voiceStatus,
    voiceSetup,
    kokoroStatus,
    kokoroSetup,
    ttsCacheStats,
    ttsCacheClear,
    type SettingsResponse,
    type IngestResult,
    type DriverProfileData,
    type DriverMemory,
    type VoiceStatus,
    type KokoroStatus,
    type TtsCacheStats,
  } from "./api";
  import { eventFeed } from "./events";
  import InfoTip from "./InfoTip.svelte";
  import { isTauri as isTauriShell, captureNext, deviceIdOf, describeBind, isStaleBind, listDevices, pushBinds, type DeviceInfo } from "./voice-shell";
  import { makeSpeaker } from "./audio";

  const KOKORO_PREVIEW_SENTENCE = "Brake later into turn four, the fast left after the long straight.";

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

  // --- Voice (#104) ---
  let voiceStatusInfo: VoiceStatus | null = $state(null);
  let capturingKey = $state(false);
  let capturingWheel = $state(false);
  let capturingGamepad = $state(false);
  let hotkeyError: string | null = $state(null);
  let devices: DeviceInfo[] = $state([]);

  // Model setup dialog: shown only when Enable is turned on and the model
  // isn't installed yet -- an already-installed model just enables in place.
  type VoiceDialogPhase = "ask" | "progress" | "ready" | "error";
  let showSetupDialog = $state(false);
  let dialogPhase: VoiceDialogPhase = $state("ask");
  let dialogError: string | null = $state(null);

  function voiceSetupPct(): number {
    const st = voiceStatusInfo;
    if (!st?.total_bytes) return 0;
    return Math.round((st.progress_bytes / st.total_bytes) * 100);
  }

  function openVoiceDialog(el: HTMLDialogElement) {
    // jsdom (tests) has no showModal -- the `open` attribute alone is enough
    // for the dialog to be findable and visible there.
    if (typeof el.showModal === "function") el.showModal();
    el.setAttribute("open", "");
  }

  function settleVoiceSetup(st: VoiceStatus) {
    voiceStatusInfo = st;
    if (st.installing) return;
    if (st.error) {
      dialogError = st.error;
      dialogPhase = "error";
    } else {
      dialogPhase = "ready";
    }
  }

  // #106: the download used to be watched by a 1 s /voice/status poll inside
  // downloadVoiceModel's loop. The sidecar streams voice_setup frames while an
  // install runs, so progress (and the final ready/error) just arrives.
  // Both installers (whisper + Kokoro) now share this one topic (Kokoro speaker
  // plan), so a frame is only this dialog's if its engine matches -- a frame with
  // no `engine` at all is the original whisper installer (predates the field).
  $effect(() => eventFeed.voiceSetup.subscribe((st) => {
    if (st && (st.engine ?? "whisper") === "whisper" && dialogPhase === "progress") settleVoiceSetup(st);
  }));

  // --- Voice (Kokoro engine) ---
  let kokoroStatusInfo: KokoroStatus | null = $state(null);
  let showKokoroDialog = $state(false);
  let kokoroDialogPhase: VoiceDialogPhase = $state("ask");
  let kokoroDialogError: string | null = $state(null);
  let previewing = $state(false);
  let ttsCacheInfo: TtsCacheStats | null = $state(null);
  let clearingCache = $state(false);

  async function loadTtsCacheStats() {
    try {
      ttsCacheInfo = await ttsCacheStats();
    } catch {
      /* the readout just stays blank until the next successful load */
    }
  }

  async function clearTtsCache() {
    clearingCache = true;
    try {
      await ttsCacheClear();
      await loadTtsCacheStats();
    } finally {
      clearingCache = false;
    }
  }

  function kokoroSetupPct(): number {
    const st = kokoroStatusInfo;
    if (!st?.total_bytes) return 0;
    return Math.round((st.progress_bytes / st.total_bytes) * 100);
  }

  function kokoroStatusText(): string {
    const st = kokoroStatusInfo;
    if (!st) return "";
    if (st.error) return st.error;
    if (st.installing) {
      const pct = st.total_bytes ? Math.round((st.progress_bytes / st.total_bytes) * 100) : 0;
      return `Downloading Kokoro voice… ${pct}%`;
    }
    return st.installed ? "Ready" : "Kokoro voice isn't installed — using Windows voices";
  }

  function settleKokoroSetup(st: KokoroStatus) {
    kokoroStatusInfo = st;
    if (st.installing) return;
    if (st.error) {
      kokoroDialogError = st.error;
      kokoroDialogPhase = "error";
    } else {
      kokoroDialogPhase = "ready";
    }
  }

  $effect(() => eventFeed.voiceSetup.subscribe((st) => {
    if (st && st.engine === "kokoro" && kokoroDialogPhase === "progress") settleKokoroSetup(st as KokoroStatus);
  }));

  async function downloadKokoroModel() {
    kokoroDialogPhase = "progress";
    kokoroDialogError = null;
    try {
      settleKokoroSetup(await kokoroSetup());
    } catch (e) {
      kokoroDialogError = e instanceof Error ? e.message : String(e);
      kokoroDialogPhase = "error";
    }
  }

  function closeKokoroDialog() {
    showKokoroDialog = false;
  }

  async function notNowKokoroSetup() {
    showKokoroDialog = false;
    if (!settings) return;
    settings = { ...settings, voice: { ...settings.voice, tts_engine: "windows" } };
    await saveConfig();
  }

  async function onEngineChange() {
    await saveConfig();
    if (!settings || settings.voice.tts_engine !== "kokoro") return;
    try {
      const st = await kokoroStatus();
      kokoroStatusInfo = st;
      if (st.installed) {
        await loadTtsCacheStats();
        return;
      }
    } catch {
      /* fall through -- still offer the dialog so the driver can retry */
    }
    kokoroDialogPhase = "ask";
    kokoroDialogError = null;
    showKokoroDialog = true;
  }

  async function previewVoice() {
    if (!settings) return;
    previewing = true;
    try {
      const speaker = makeSpeaker(settings.voice, !!kokoroStatusInfo?.installed, () => {});
      await speaker.speak(KOKORO_PREVIEW_SENTENCE);
      if (settings.voice.tts_engine === "kokoro") await loadTtsCacheStats();
    } finally {
      previewing = false;
    }
  }

  async function downloadVoiceModel() {
    dialogPhase = "progress";
    dialogError = null;
    try {
      // POST /voice/setup answers with the status it started from; if it is
      // already finished there will be no frame to wait for.
      settleVoiceSetup(await voiceSetup());
    } catch (e) {
      dialogError = e instanceof Error ? e.message : String(e);
      dialogPhase = "error";
    }
  }

  function closeSetupDialog() {
    showSetupDialog = false;
  }

  async function notNowVoiceSetup() {
    showSetupDialog = false;
    if (!settings) return;
    settings = { ...settings, voice: { ...settings.voice, enabled: false } };
    await saveVoiceConfig();
  }

  // Pushes the current binds + hotkey to the shell (no-op outside Tauri).
  // Called on Settings load and after every voice save (#104 slice 4).
  async function syncVoiceShell() {
    if (!settings || !isTauriShell()) return;
    hotkeyError = await pushBinds(settings.voice);
    devices = await listDevices();
  }

  async function saveVoiceConfig() {
    await saveConfig();
    await syncVoiceShell();
  }

  // Name known at capture time (device just answered) -- persisted alongside the bind
  // so Settings can still show it if the device is asleep/unlisted later (#104).
  async function nameForBind(id: string | null): Promise<string | null> {
    const deviceId = deviceIdOf(id);
    if (deviceId === null) return null;
    devices = await listDevices();
    return devices.find((d) => d.device === deviceId)?.name ?? null;
  }

  async function captureWheel() {
    capturingWheel = true;
    const id = await captureNext();
    capturingWheel = false;
    if (!settings || id === null) return;
    const name = await nameForBind(id);
    settings = { ...settings, voice: { ...settings.voice, ptt_wheel: id, ptt_wheel_name: name } };
    await saveVoiceConfig();
  }

  async function captureGamepad() {
    capturingGamepad = true;
    const id = await captureNext();
    capturingGamepad = false;
    if (!settings || id === null) return;
    const name = await nameForBind(id);
    settings = { ...settings, voice: { ...settings.voice, ptt_gamepad: id, ptt_gamepad_name: name } };
    await saveVoiceConfig();
  }

  function clearWheel() {
    if (!settings) return;
    settings = { ...settings, voice: { ...settings.voice, ptt_wheel: null, ptt_wheel_name: null } };
    saveVoiceConfig();
  }

  function clearGamepad() {
    if (!settings) return;
    settings = { ...settings, voice: { ...settings.voice, ptt_gamepad: null, ptt_gamepad_name: null } };
    saveVoiceConfig();
  }

  function voiceStatusText(): string {
    const st = voiceStatusInfo;
    if (!st) return "";
    if (st.error) return st.error;
    if (st.installing) {
      const pct = st.total_bytes ? Math.round((st.progress_bytes / st.total_bytes) * 100) : 0;
      return `Downloading speech model… ${pct}%`;
    }
    return st.installed ? "Ready" : "";
  }

  async function onVoiceEnabledChange() {
    await saveVoiceConfig();
    if (!settings?.voice.enabled) return;
    try {
      const st = await voiceStatus();
      voiceStatusInfo = st;
      if (st.installed) return;
    } catch {
      /* fall through -- still offer the dialog so the driver can retry */
    }
    dialogPhase = "ask";
    dialogError = null;
    showSetupDialog = true;
  }

  function startCapture() {
    capturingKey = true;
  }

  // getVoices() is empty until the async `voiceschanged` event fires in
  // Chromium/WebView2, so a one-shot read at mount would show a false "no
  // speech voices installed" warning on every startup. Re-read on the event
  // and keep it in state so the template re-renders once the real list lands.
  let ttsVoices: SpeechSynthesisVoice[] = $state(
    typeof speechSynthesis !== "undefined" ? speechSynthesis.getVoices() : [],
  );

  $effect(() => {
    if (typeof speechSynthesis === "undefined") return;
    const refresh = () => { ttsVoices = speechSynthesis.getVoices(); };
    speechSynthesis.addEventListener("voiceschanged", refresh);
    return () => speechSynthesis.removeEventListener("voiceschanged", refresh);
  });

  // Mic input devices for the Microphone select (#104). Labels are empty until
  // mic permission has been granted once (same getUserMedia-gated quirk as
  // ttsVoices above), so refresh whenever that might have changed: on mount, on
  // devicechange, and after a settings save.
  let micDevices: MediaDeviceInfo[] = $state([]);

  async function refreshMicDevices() {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.enumerateDevices) {
      micDevices = [];
      return;
    }
    try {
      micDevices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    } catch {
      micDevices = [];
    }
  }

  $effect(() => {
    refreshMicDevices();
    if (typeof navigator === "undefined" || !navigator.mediaDevices) return;
    navigator.mediaDevices.addEventListener("devicechange", refreshMicDevices);
    return () => navigator.mediaDevices.removeEventListener("devicechange", refreshMicDevices);
  });

  $effect(() => {
    if (!capturingKey) return;
    const onCapture = (e: KeyboardEvent) => {
      capturingKey = false;
      if (!settings) return;
      settings = { ...settings, voice: { ...settings.voice, ptt_key: e.code } };
      saveVoiceConfig();
    };
    window.addEventListener("keydown", onCapture);
    return () => window.removeEventListener("keydown", onCapture);
  });

  $effect(() => {
    (async () => {
      try {
        settings = await getSettings();
        phase = "ok";
        if (settings.voice.tts_engine === "kokoro") {
          try {
            kokoroStatusInfo = await kokoroStatus();
          } catch {
            /* Voice section just shows "not installed" until a retry succeeds */
          }
          await loadTtsCacheStats();
        }
        await syncVoiceShell();
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
      coach: settings.coach,
      driver: settings.driver,
      voice: settings.voice,
      lmu_dir: settings.lmu_dir,
      lmu_player_names: settings.lmu_player_names,
      log_level: settings.log_level,
    };
    return putSettings(cfg).then((res) => {
      settings = res;
      window.dispatchEvent(new CustomEvent("settings-changed"));
      refreshMicDevices();
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

  onMount(() => {
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
      {#if settings.coach}
        <div class="field">
          <span class="lbl">Thinking<InfoTip text="Auto: quick answers while you drive, the coach thinks before answering in the garage, and thinks hard for a race review. Off: always answer at once. Always: think on every answer. Say “think about it” or “quick answer” to override one question." /></span>
          <div class="control">
            <select aria-label="Thinking" bind:value={settings.coach.thinking} onchange={saveConfig}>
              <option value="auto">Auto</option>
              <option value="off">Off</option>
              <option value="always">Always</option>
            </select>
          </div>
        </div>
        <div class="field">
          <span class="lbl">Question sorter<InfoTip text="In Auto, a small, cheap model reads each garage question to decide how hard the coach should think (its tokens count on the chat meter). Off: simple keyword rules decide instead." /></span>
          <div class="control">
            <select aria-label="Question sorter" bind:value={settings.coach.classifier_model} onchange={saveConfig}>
              <option value="cheapest">Cheapest model</option>
              <option value="none">Off (keyword rules)</option>
            </select>
          </div>
        </div>
      {/if}
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
      <h3>Voice</h3>
      <div class="field">
        <span class="lbl">Enable voice<InfoTip text="Push-to-talk and hands-free conversation mode. Downloads a local speech-recognition model on first use." /></span>
        <div class="control">
          <input
            type="checkbox"
            aria-label="Enable voice"
            bind:checked={settings.voice.enabled}
            onchange={onVoiceEnabledChange}
          />
        </div>
      </div>
      {#if voiceStatusInfo && !showSetupDialog}
        <p class="status">{voiceStatusText()}</p>
      {/if}
      {#if showSetupDialog}
        <dialog use:openVoiceDialog aria-label="Set up voice">
          <h3>Set up voice</h3>
          <p>
            Nestor downloads a speech-recognition model (about 250 MB, one time) into your app data
            folder so voice works offline.
          </p>
          {#if dialogPhase === "ask"}
            <div class="dialog-actions">
              <button type="button" onclick={downloadVoiceModel}>Download</button>
              <button type="button" onclick={notNowVoiceSetup}>Not now</button>
            </div>
          {:else if dialogPhase === "progress"}
            <progress value={voiceStatusInfo?.progress_bytes ?? 0} max={voiceStatusInfo?.total_bytes ?? 100}
            ></progress>
            <p>{voiceSetupPct()}%</p>
          {:else if dialogPhase === "ready"}
            <p>Ready</p>
            <div class="dialog-actions">
              <button type="button" onclick={closeSetupDialog}>Done</button>
            </div>
          {:else if dialogPhase === "error"}
            <p class="status">{dialogError}</p>
            <div class="dialog-actions">
              <button type="button" onclick={downloadVoiceModel}>Retry</button>
              <button type="button" onclick={closeSetupDialog}>Close</button>
            </div>
          {/if}
        </dialog>
      {/if}
      <div class="field">
        <span class="lbl">Push-to-talk key<InfoTip text="Held to talk; tap to start hands-free conversation mode." /></span>
        <div class="control">
          <span class="val">{settings.voice.ptt_key}</span>
          <button type="button" onclick={startCapture}>{capturingKey ? "Press a key…" : "Capture"}</button>
        </div>
      </div>
      {#if hotkeyError}
        <p class="status">Couldn't register {settings.voice.ptt_key} globally: {hotkeyError}</p>
      {/if}
      {#if isTauriShell()}
        <div class="field">
          <span class="lbl">Push-to-talk only when Le Mans Ultimate or the coach is in focus<InfoTip text="Turn off if PTT stops working in VR." /></span>
          <div class="control">
            <input
              type="checkbox"
              aria-label="Push-to-talk only when Le Mans Ultimate or the coach is in focus"
              bind:checked={settings.voice.ptt_focus_gate}
              onchange={saveVoiceConfig}
            />
          </div>
        </div>
        <div class="field">
          <span class="lbl">Wait for CrewChief to finish talking<InfoTip text="The coach holds its next sentence while CrewChief is speaking. Does nothing when CrewChief isn't running." /></span>
          <div class="control">
            <input
              type="checkbox"
              aria-label="Wait for CrewChief to finish talking"
              bind:checked={settings.voice.crewchief_wait}
              onchange={saveVoiceConfig}
            />
          </div>
        </div>
        <div class="field">
          <span class="lbl">Wheel button<InfoTip text="A wheel button held to talk, in addition to the keyboard key." /></span>
          <div class="control">
            <span class="val" title={settings.voice.ptt_wheel ?? undefined}>{describeBind(settings.voice.ptt_wheel, devices, settings.voice.ptt_wheel_name)}</span>
            <button type="button" onclick={captureWheel}>{capturingWheel ? "Press a button…" : "Capture"}</button>
            <button type="button" onclick={clearWheel} disabled={!settings.voice.ptt_wheel}>Clear</button>
          </div>
        </div>
        {#if isStaleBind(settings.voice.ptt_wheel)}
          <p class="status">This wheel bind was saved with an older version and needs re-capturing.</p>
        {/if}
        <div class="field">
          <span class="lbl">Gamepad button<InfoTip text="A gamepad button held to talk, in addition to the keyboard key." /></span>
          <div class="control">
            <span class="val" title={settings.voice.ptt_gamepad ?? undefined}>{describeBind(settings.voice.ptt_gamepad, devices, settings.voice.ptt_gamepad_name)}</span>
            <button type="button" onclick={captureGamepad}>{capturingGamepad ? "Press a button…" : "Capture"}</button>
            <button type="button" onclick={clearGamepad} disabled={!settings.voice.ptt_gamepad}>Clear</button>
          </div>
        </div>
        {#if isStaleBind(settings.voice.ptt_gamepad)}
          <p class="status">This gamepad bind was saved with an older version and needs re-capturing.</p>
        {/if}
      {/if}
      <div class="field">
        <span class="lbl">Microphone<InfoTip text="Which input device voice capture records from." /></span>
        <div class="control">
          <select aria-label="Microphone" bind:value={settings.voice.mic_device_id} onchange={saveConfig}>
            <option value={null}>System default</option>
            {#each micDevices as d, i (d.deviceId)}
              <option value={d.deviceId}>{d.label || `Microphone ${i + 1}`}</option>
            {/each}
          </select>
          {#if micDevices.length > 0 && micDevices.every((d) => !d.label)}
            <span class="status inline">Names appear after the first voice use grants mic access.</span>
          {/if}
        </div>
      </div>
      <div class="field">
        <span class="lbl">Engine<InfoTip text="Kokoro is a higher-quality, downloaded voice; Windows uses your system's built-in voices." /></span>
        <div class="control">
          <select aria-label="Engine" bind:value={settings.voice.tts_engine} onchange={onEngineChange}>
            <option value="windows">Windows</option>
            <option value="kokoro">Kokoro</option>
          </select>
        </div>
      </div>
      {#if settings.voice.tts_engine === "kokoro" && kokoroStatusInfo && !showKokoroDialog}
        <p class="status">{kokoroStatusText()}</p>
        {#if ttsCacheInfo}
          <p class="status inline">
            Cached audio: {(ttsCacheInfo.bytes / 1_000_000).toFixed(1)} MB ({ttsCacheInfo.files} clips)
            <button type="button" onclick={clearTtsCache} disabled={clearingCache || ttsCacheInfo.files === 0}>
              {clearingCache ? "Clearing…" : "Clear"}
            </button>
          </p>
        {/if}
      {/if}
      {#if showKokoroDialog}
        <dialog use:openVoiceDialog aria-label="Set up Kokoro voice">
          <h3>Set up Kokoro voice</h3>
          <p>
            Nestor downloads the Kokoro voice model (about 340 MB, one time) into your app data
            folder so Kokoro voices work offline.
          </p>
          {#if kokoroDialogPhase === "ask"}
            <div class="dialog-actions">
              <button type="button" onclick={downloadKokoroModel}>Download</button>
              <button type="button" onclick={notNowKokoroSetup}>Not now</button>
            </div>
          {:else if kokoroDialogPhase === "progress"}
            <progress value={kokoroStatusInfo?.progress_bytes ?? 0} max={kokoroStatusInfo?.total_bytes ?? 100}
            ></progress>
            <p>{kokoroSetupPct()}%</p>
          {:else if kokoroDialogPhase === "ready"}
            <p>Ready</p>
            <div class="dialog-actions">
              <button type="button" onclick={closeKokoroDialog}>Done</button>
            </div>
          {:else if kokoroDialogPhase === "error"}
            <p class="status">{kokoroDialogError}</p>
            <div class="dialog-actions">
              <button type="button" onclick={downloadKokoroModel}>Retry</button>
              <button type="button" onclick={closeKokoroDialog}>Close</button>
            </div>
          {/if}
        </dialog>
      {/if}
      <div class="field">
        <span class="lbl">Voice<InfoTip text="Which voice reads answers aloud." /></span>
        <div class="control">
          {#if settings.voice.tts_engine === "kokoro"}
            {#if kokoroStatusInfo?.voices?.length}
              <select aria-label="Voice" bind:value={settings.voice.kokoro_voice} onchange={saveConfig}>
                {#each kokoroStatusInfo.voices as v (v)}
                  <option value={v}>{v}</option>
                {/each}
              </select>
            {:else}
              <span class="status inline">Kokoro voices appear once the model is installed.</span>
            {/if}
          {:else if ttsVoices.length === 0}
            <span class="status inline">No speech voices installed — answers will be shown only.</span>
          {:else}
            <select aria-label="Voice" bind:value={settings.voice.tts_voice} onchange={saveConfig}>
              <option value={null}>Browser default</option>
              {#each ttsVoices as v (v.name)}
                <option value={v.name}>{v.name}</option>
              {/each}
            </select>
          {/if}
          <button type="button" onclick={previewVoice} disabled={previewing}>{previewing ? "Playing…" : "Preview"}</button>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Rate</span>
        <div class="control">
          <input type="range" min="0.6" max="1.6" step="0.1" bind:value={settings.voice.tts_rate} onchange={saveConfig} />
          <span class="val">{settings.voice.tts_rate.toFixed(1)}x</span>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Silence threshold<InfoTip text="How loud speech must be (0-1) to count as talking." /></span>
        <div class="control">
          <input type="range" min="0.005" max="0.1" step="0.005" bind:value={settings.voice.vad_threshold} onchange={saveConfig} />
          <span class="val">{settings.voice.vad_threshold}</span>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Silence to end (ms)<InfoTip text="How long a pause ends an utterance and sends it." /></span>
        <div class="control"><input type="number" bind:value={settings.voice.vad_silence_ms} onchange={saveConfig} /></div>
      </div>
      <div class="field">
        <span class="lbl">Max utterance (s)<InfoTip text="Hard cap on how long a single utterance can run." /></span>
        <div class="control"><input type="number" bind:value={settings.voice.vad_max_s} onchange={saveConfig} /></div>
      </div>
      <div class="field">
        <span class="lbl">Conversation idle (s)<InfoTip text="How long conversation mode listens with no speech before it stops." /></span>
        <div class="control"><input type="number" bind:value={settings.voice.conversation_idle_s} onchange={saveConfig} /></div>
      </div>
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
            <p class="sub">
              Erases every lap, chat, and telemetry file. Can't be undone. Sessions from LMU's
              result logs are re-imported automatically within about a minute.
            </p>
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
