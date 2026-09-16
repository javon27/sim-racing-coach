<script lang="ts">
  import { onMount, tick } from "svelte";
  import { liveField } from "./live";
  import { nextTheme, applyTheme, loadTheme, saveTheme, THEME_LABELS, THEME_ICONS, type ThemeMode } from "./theme";
  import SessionBar from "./SessionBar.svelte";
  import RaceStrip from "./RaceStrip.svelte";
  import LapList from "./LapList.svelte";
  import ChatPanel from "./ChatPanel.svelte";
  import InputsPanel from "./InputsPanel.svelte";
  import CornerSetupTabs from "./CornerSetupTabs.svelte";
  import TrackMapPane from "./TrackMapPane.svelte";
  import SessionsScreen from "./SessionsScreen.svelte";
  import SettingsScreen from "./SettingsScreen.svelte";
  import TracksScreen from "./TracksScreen.svelte";
  import PitWall from "./PitWall.svelte";
  import NestorMark from "./NestorMark.svelte";
  import Splash from "./Splash.svelte";

  type Screen = "coach" | "sessions" | "pitwall" | "tracks" | "settings";

  let booted = $state(false);
  let railOpen = $state(false); // narrow-width laps drawer
  let screen: Screen = $state("coach");
  let theme: ThemeMode = $state(loadTheme());
  let selectedLapId: number | undefined = $state(undefined);
  let selectedSessionId: string | undefined = $state(undefined);
  let activeSessionId: string | undefined = $state(undefined);
  // undefined = no session context (ChatPanel shows the latest chat);
  // null = a session was opened that has NO chat (show an empty new chat);
  // string = open that specific chat.
  let activeChatId: string | null | undefined = $state(undefined);

  // Start the shared live feed once, for the whole app lifetime, so the Pit Wall
  // reflects live data no matter which view is active (e.g. while mapping a track).
  onMount(() => liveField.start());

  function toggleTheme() {
    theme = nextTheme(theme);
    applyTheme(theme);
    saveTheme(theme);
  }

  // Draggable divider that grows/shrinks the lap list (its scroll area height).
  let lapsH = $state(280); // px; drives --laps-h on the rail
  let dragStartY = 0;
  let dragStartH = 0;
  let draggingRail = $state(false);
  function railResizeDown(e: PointerEvent) {
    draggingRail = true;
    dragStartY = e.clientY;
    dragStartH = lapsH;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function railResizeMove(e: PointerEvent) {
    if (!draggingRail) return;
    const max = typeof window !== "undefined" ? window.innerHeight * 0.7 : 700;
    lapsH = Math.max(120, Math.min(dragStartH + (e.clientY - dragStartY), max));
  }
  function railResizeUp() {
    draggingRail = false;
  }

  // Draggable divider for the Inputs panel (#95 round 2): the chat bar directly
  // below doubles as the handle, so this state lives here and is threaded down
  // to InputsPanel (height) and ChatPanel (resize callbacks) as props.
  let inputsH = $state(loadInputsHeight()); // px; drives --inputs-h on InputsPanel
  const INPUTS_HEIGHT_KEY = "inputsPanel.height";
  function loadInputsHeight(): number {
    try {
      const v = Number(localStorage.getItem(INPUTS_HEIGHT_KEY));
      return v > 0 ? v : 220;
    } catch {
      return 220;
    }
  }
  function saveInputsHeight(h: number) {
    try {
      localStorage.setItem(INPUTS_HEIGHT_KEY, String(h));
    } catch {
      /* private mode / storage disabled — height just won't persist */
    }
  }
  let inputsStartY = 0;
  let inputsStartH = 0;
  let draggingInputs = $state(false);
  function inputsResizeDown(e: PointerEvent) {
    draggingInputs = true;
    inputsStartY = e.clientY;
    inputsStartH = inputsH;
    const el = e.currentTarget as HTMLElement;
    if (typeof el.setPointerCapture === "function") el.setPointerCapture(e.pointerId);
  }
  function inputsResizeMove(e: PointerEvent) {
    if (!draggingInputs) return;
    const max = typeof window !== "undefined" ? window.innerHeight * 0.7 : 700;
    inputsH = Math.max(120, Math.min(inputsStartH + (e.clientY - inputsStartY), max));
    saveInputsHeight(inputsH);
  }
  function inputsResizeUp() {
    draggingInputs = false;
  }
  const inputsResize = $derived({
    down: inputsResizeDown,
    move: inputsResizeMove,
    up: inputsResizeUp,
    dragging: draggingInputs,
  });

  // Draggable divider above the track-map pane (mirrors the laps handle). The
  // handle sits above the map, so dragging DOWN shrinks it.
  let mapH = $state(200); // px; drives --map-h on the rail
  let mapStartY = 0;
  let mapStartH = 0;
  let draggingMap = $state(false);
  function mapResizeDown(e: PointerEvent) {
    draggingMap = true;
    mapStartY = e.clientY;
    mapStartH = mapH;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }
  function mapResizeMove(e: PointerEvent) {
    if (!draggingMap) return;
    const max = typeof window !== "undefined" ? window.innerHeight * 0.7 : 700;
    mapH = Math.max(60, Math.min(mapStartH - (e.clientY - mapStartY), max));
  }
  function mapResizeUp() {
    draggingMap = false;
  }

  // "New session": clear the selected session, laps, map and chat so the driver
  // starts fresh (back to the live view with an empty chat).
  async function handleNewSession() {
    selectedLapId = undefined;
    selectedSessionId = undefined;
    activeSessionId = undefined;
    railOpen = false;
    // Clear-then-set so ChatPanel re-registers an empty chat even if it was
    // already on one (null = fresh empty chat, not "latest").
    activeChatId = undefined;
    await tick();
    activeChatId = null;
  }

  async function handleSelectSession(sessionId: string, chatId?: string) {
    // Clear then re-set so re-selecting the SAME session is a real prop change,
    // which re-arms LapList's review baseline (it may have snapped to live).
    activeSessionId = undefined;
    await tick();
    activeSessionId = sessionId;
    selectedSessionId = sessionId;
    // Open the chat that discussed this session. A session with NO chat resolves
    // to null (empty new chat) — NOT undefined (which would fall back to "latest"
    // and confusingly open an unrelated chat). Clear-then-set so re-selecting the
    // same session still registers as a prop change in ChatPanel.
    activeChatId = undefined;
    await tick();
    activeChatId = chatId ?? null;
    screen = "coach";
  }
</script>

{#if !booted}
  <Splash onready={() => (booted = true)} />
{/if}

{#if booted}
<div class="shell">
  <nav class="nav">
    <div class="logo"><NestorMark size={26} label="Nestor" outline /></div>
    <button
      type="button"
      class="nav-item"
      class:on={screen === "coach"}
      onclick={() => (screen = "coach")}
    >
      <span class="lbl">Coach</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-11.9 7.6L4 20.5l1.4-4.9A8.4 8.4 0 1 1 21 11.5z"/></svg>
    </button>
    <button
      type="button"
      class="nav-item"
      class:on={screen === "sessions"}
      onclick={() => (screen = "sessions")}
    >
      <span class="lbl">Sessions</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
    </button>
    <button
      type="button"
      class="nav-item"
      class:on={screen === "pitwall"}
      onclick={() => (screen = "pitwall")}
    >
      <span class="lbl">Pit Wall</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5 7 5-14 2.5 7H21"/></svg>
    </button>
    <button
      type="button"
      class="nav-item"
      class:on={screen === "tracks"}
      onclick={() => (screen = "tracks")}
    >
      <span class="lbl">Tracks</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18l7-4 4 2 7-4V3l-7 4-4-2-7 4z"/><path d="M10 5v12M17 7v12"/></svg>
    </button>
    <div class="sp"></div>
    <button type="button" class="nav-item" onclick={toggleTheme}>
      <span class="lbl">Theme · {THEME_LABELS[theme]}</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{@html THEME_ICONS[theme]}</svg>
    </button>
    <button
      type="button"
      class="nav-item"
      class:on={screen === "settings"}
      onclick={() => (screen = "settings")}
    >
      <span class="lbl">Settings</span>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    </button>
  </nav>
  <div class="content">
    <!-- One shared header for Coach + Pit Wall: a single instance (kept mounted
         across a coach<->pitwall switch) so the two views can never show different
         status/track/car for a frame. Sessions/Settings have their own headers. -->
    {#if screen === "coach" || screen === "pitwall"}<SessionBar />{/if}
    <div class="screens">
    <section class="screen" class:on={screen === "coach"}>
      <div class="app">
        <div class="main" class:rail-open={railOpen}>
          <!-- Narrow-width only: the rail is hidden by default; this tab slides
               it in as an overlay drawer, dismissed by the scrim. -->
          <button
            type="button"
            class="rail-tab"
            aria-label={railOpen ? "Hide laps panel" : "Show laps panel"}
            aria-expanded={railOpen}
            onclick={() => (railOpen = !railOpen)}
          >{railOpen ? "‹" : "›"}</button>
          {#if railOpen}
            <div class="rail-scrim" role="presentation" onclick={() => (railOpen = false)}></div>
          {/if}
          <aside class="rail" style="--laps-h: {lapsH}px; --map-h: {mapH}px">
            <RaceStrip sessionId={selectedSessionId} />
            <LapList
              sessionId={activeSessionId}
              selectedLapId={selectedLapId ?? null}
              onselect={(detail) => {
                selectedLapId = detail.lap_id;
                selectedSessionId = detail.session_id;
              }}
              onlive={handleNewSession}
            />
            <div
              class="rail-resizer"
              class:dragging={draggingRail}
              role="separator"
              aria-orientation="horizontal"
              aria-label="Resize lap list"
              onpointerdown={railResizeDown}
              onpointermove={railResizeMove}
              onpointerup={railResizeUp}
            ></div>
            <CornerSetupTabs lapId={selectedLapId ?? null} />
            <div
              class="rail-resizer"
              class:dragging={draggingMap}
              role="separator"
              aria-orientation="horizontal"
              aria-label="Resize track map"
              onpointerdown={mapResizeDown}
              onpointermove={mapResizeMove}
              onpointerup={mapResizeUp}
            ></div>
            <TrackMapPane sessionId={selectedSessionId} />
          </aside>
          <div class="coach-main">
            <InputsPanel lapId={selectedLapId ?? null} height={inputsH} />
            <ChatPanel
              lapId={selectedLapId}
              sessionId={selectedSessionId}
              chatId={activeChatId}
              onnewsession={handleNewSession}
              resize={inputsResize}
            />
          </div>
        </div>
      </div>
    </section>
    <section class="screen" class:on={screen === "sessions"}>
      <SessionsScreen onselectSession={handleSelectSession} />
    </section>
    <section class="screen" class:on={screen === "pitwall"}>
      {#if screen === "pitwall"}<PitWall />{/if}
    </section>
    <section class="screen" class:on={screen === "tracks"}>
      {#if screen === "tracks"}<TracksScreen />{/if}
    </section>
    <section class="screen" class:on={screen === "settings"}>
      <SettingsScreen />
    </section>
    </div>
  </div>
</div>
{/if}
