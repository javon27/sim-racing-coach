// Theme toggle: System -> Light -> Dark -> System, ported from docs/design/v1-ui-prototype.html.
// Writes data-theme on <html> (removed entirely for "system", so the OS preference / prefers-color-scheme wins).

export type ThemeMode = "system" | "light" | "dark";

const ORDER: ThemeMode[] = ["system", "light", "dark"];

export const THEME_LABELS: Record<ThemeMode, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

// Raw inner-SVG markup per mode, copied from the prototype's `icons` map.
export const THEME_ICONS: Record<ThemeMode, string> = {
  system:
    '<circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4"/>',
  light:
    '<circle cx="12" cy="12" r="5"/><path d="M12 1v3M12 20v3M4 12H1M23 12h-3M5.6 5.6 3.5 3.5M20.5 20.5l-2.1-2.1M18.4 5.6l2.1-2.1M3.5 20.5l2.1-2.1"/>',
  dark: '<path d="M21 12.8A8.5 8.5 0 1 1 11.2 3 6.6 6.6 0 0 0 21 12.8z"/>',
};

export function nextTheme(mode: ThemeMode): ThemeMode {
  return ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
}

export function applyTheme(mode: ThemeMode): void {
  if (mode === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", mode);
  }
}

const STORE_KEY = "nestor-theme";

export function loadTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(STORE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* storage blocked (private window etc.) — fall back to system */
  }
  return "system";
}

export function saveTheme(mode: ThemeMode): void {
  try {
    localStorage.setItem(STORE_KEY, mode);
  } catch {
    /* non-fatal */
  }
}
