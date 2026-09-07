<script lang="ts">
  import { tick } from "svelte";
  import { nextTheme, applyTheme, loadTheme, saveTheme, THEME_LABELS, THEME_ICONS, type ThemeMode } from "./theme";
  import SessionBar from "./SessionBar.svelte";
  import RaceStrip from "./RaceStrip.svelte";
  import LapList from "./LapList.svelte";
  import ChatPanel from "./ChatPanel.svelte";
  import CornerSetupTabs from "./CornerSetupTabs.svelte";
  import TrackMapPane from "./TrackMapPane.svelte";
  import SessionsScreen from "./SessionsScreen.svelte";
  import SettingsScreen from "./SettingsScreen.svelte";
  import NestorMark from "./NestorMark.svelte";
  import Splash from "./Splash.svelte";

  type Screen = "coach" | "sessions" | "pitwall" | "settings";

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
  <div class="screens">
    <section class="screen" class:on={screen === "coach"}>
      <div class="app">
        <SessionBar />
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
          <aside class="rail" style="--laps-h: {lapsH}px">
            <RaceStrip sessionId={selectedSessionId} />
            <LapList
              sessionId={activeSessionId}
              selectedLapId={selectedLapId ?? null}
              onselect={(detail) => {
                selectedLapId = detail.lap_id;
                selectedSessionId = detail.session_id;
              }}
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
            <TrackMapPane sessionId={selectedSessionId} />
          </aside>
          <ChatPanel lapId={selectedLapId} sessionId={selectedSessionId} chatId={activeChatId} onnewsession={handleNewSession} />
        </div>
      </div>
    </section>
    <section class="screen" class:on={screen === "sessions"}>
      <SessionsScreen onselectSession={handleSelectSession} />
    </section>
    <section class="screen" class:on={screen === "pitwall"}>Pit Wall (coming soon)</section>
    <section class="screen" class:on={screen === "settings"}>
      <SettingsScreen />
    </section>
  </div>
</div>
{/if}
