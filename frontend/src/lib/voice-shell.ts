// Bridges the Tauri shell (global hotkey + wheel/gamepad poller, src-tauri/src/voice_input.rs)
// to the browser-side VoiceLoop. No Tauri imports at module load -- ShellApi is injected so
// this is testable without a Tauri runtime, and the real impl dynamic-imports @tauri-apps/api
// so a browser-only build (no shell attached) never touches it.
export interface ShellApi {
  listen(event: string, cb: (payload: unknown) => void): Promise<() => void>;
  invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown>;
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function tauriShell(): ShellApi {
  return {
    async listen(event, cb) {
      const { listen } = await import("@tauri-apps/api/event");
      return listen(event, (ev) => cb(ev.payload));
    },
    async invoke(cmd, args) {
      const { invoke } = await import("@tauri-apps/api/core");
      return invoke(cmd, args);
    },
  };
}

/** Wires voice:press/release to the loop; returns a detach fn. */
export async function attachShell(
  loop: { press(): void; release(): void },
  shell: ShellApi = tauriShell(),
): Promise<() => void> {
  const offPress = await shell.listen("voice:press", () => loop.press());
  const offRelease = await shell.listen("voice:release", () => loop.release());
  return () => { offPress(); offRelease(); };
}

/** Push binds + hotkey to the shell. Resolves the hotkey error text (or null). */
export async function pushBinds(
  v: { ptt_key: string; ptt_wheel: string | null; ptt_gamepad: string | null; ptt_focus_gate?: boolean },
  shell: ShellApi = tauriShell(),
): Promise<string | null> {
  await shell.invoke("voice_set_binds", {
    binds: { wheel: v.ptt_wheel, gamepad: v.ptt_gamepad },
    focus_gate: v.ptt_focus_gate ?? true,
  });
  try {
    await shell.invoke("voice_set_hotkey", { key: v.ptt_key });
    return null;
  } catch (e) {
    return String(e);
  }
}

// #163: the coach never talks over CrewChief. The shell (src-tauri/src/crewchief.rs)
// owns the peak threshold, the ~300 ms quiet tail and the stuck-loud cutoff; the cap
// on how long one answer may be held is here.
// PENDING RIG: most an answer is held in total (all its sentences share it) before
// it's spoken anyway -- a stuck meter costs one answer this much, never more.
export const CC_ANSWER_MAX_WAIT_MS = 8000;
const CC_POLL_MS = 50;

/** A speaker's `hold` (audio.ts): resolves once CrewChief is quiet, when the answer's
 * CC_ANSWER_MAX_WAIT_MS budget is spent, as soon as `cancelled()` turns true, or at
 * once if the shell can't answer -- so a missing shell/command/CrewChief behaves
 * exactly as with no hold at all. A null reply (watcher not read yet) keeps waiting.
 * `reset()` refills the budget: speakers call it on cancel and when they go idle. */
export function crewChiefHold(shell: ShellApi = tauriShell()): ((cancelled: () => boolean) => Promise<void>) & { reset(): void } {
  let leftMs = CC_ANSWER_MAX_WAIT_MS;
  const hold = async (cancelled: () => boolean) => {
    const start = Date.now();
    try {
      while (!cancelled() && Date.now() - start < leftMs) {
        try {
          const active = await shell.invoke("crewchief_active");
          if (active !== true && active !== null) return;
        } catch {
          return;
        }
        await new Promise((r) => setTimeout(r, CC_POLL_MS));
      }
    } finally {
      leftMs = Math.max(0, leftMs - (Date.now() - start));
    }
  };
  return Object.assign(hold, { reset: () => { leftMs = CC_ANSWER_MAX_WAIT_MS; } });
}

export interface DeviceInfo { device: string; name: string }

/** Lists connected wheel/gamepad devices with their friendly names (#104). Empty on error. */
export async function listDevices(shell: ShellApi = tauriShell()): Promise<DeviceInfo[]> {
  try {
    return (await shell.invoke("voice_list_devices")) as DeviceInfo[];
  } catch {
    return [];
  }
}

/** The `<device>` part of a `<device>:<button>` bind id, or null if it doesn't parse. */
export function deviceIdOf(id: string | null): string | null {
  if (id === null) return null;
  const i = id.lastIndexOf(":");
  return i === -1 ? null : id.slice(0, i);
}

/** Mirrors the Rust `describe_bind`: "<name> · button <N+1>". The live device list wins;
 * if the device isn't currently listed, falls back to the name captured at bind time
 * (storedName, #104) with a "(not connected)" hint; else the raw device id as before. */
export function describeBind(id: string | null, devices: DeviceInfo[], storedName?: string | null): string {
  if (id === null) return "not set";
  const i = id.lastIndexOf(":");
  if (i === -1) return id;
  const device = id.slice(0, i);
  const button = Number(id.slice(i + 1));
  if (!Number.isInteger(button)) return id;
  const live = devices.find((d) => d.device === device)?.name;
  if (live) return `${live} · button ${button + 1}`;
  if (storedName) return `${storedName} · button ${button + 1} (not connected)`;
  return `${device} · button ${button + 1}`;
}

/** A bind saved before #144 (raw HID) as a winmm index, which the shell can never
 * resolve now -- winmm is gone. There's no vid/pid to migrate it to, so Settings
 * surfaces it instead of failing silently. */
export function isStaleBind(id: string | null): boolean {
  return id?.startsWith("winmm:") ?? false;
}

/** Arms capture and resolves with the next pressed bind id, or null on timeout. */
export async function captureNext(shell: ShellApi = tauriShell(), timeoutMs = 10000): Promise<string | null> {
  return new Promise((resolve) => {
    let off: (() => void) | null = null;
    let done = false;
    const finish = (id: string | null) => {
      if (done) return;
      done = true;
      off?.();
      resolve(id);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    shell.listen("voice:captured", (payload) => {
      clearTimeout(timer);
      finish((payload as { id: string }).id);
    }).then((detach) => {
      off = detach;
      if (done) detach();
    });
    shell.invoke("voice_capture_next");
  });
}
