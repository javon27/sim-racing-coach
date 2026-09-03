<script lang="ts">
  import { nextTheme, applyTheme, THEME_LABELS, THEME_ICONS, type ThemeMode } from "./theme";
  import SessionBar from "./SessionBar.svelte";
  import LapList from "./LapList.svelte";

  type Screen = "coach" | "sessions" | "pitwall" | "settings";

  let screen: Screen = $state("coach");
  let theme: ThemeMode = $state("system");

  function toggleTheme() {
    theme = nextTheme(theme);
    applyTheme(theme);
  }
</script>

<div class="shell">
  <nav class="nav">
    <div class="logo">P</div>
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
        <div class="main">
          <LapList />
          <!-- onselect consumed by the chat/corner panel in Phase 4 -->
        </div>
      </div>
    </section>
    <section class="screen" class:on={screen === "sessions"}>Sessions (coming soon)</section>
    <section class="screen" class:on={screen === "pitwall"}>Pit Wall (coming soon)</section>
    <section class="screen" class:on={screen === "settings"}>Settings (coming soon)</section>
  </div>
</div>
