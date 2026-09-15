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
  v: { ptt_key: string; ptt_wheel: string | null; ptt_gamepad: string | null },
  shell: ShellApi = tauriShell(),
): Promise<string | null> {
  await shell.invoke("voice_set_binds", { binds: { wheel: v.ptt_wheel, gamepad: v.ptt_gamepad } });
  try {
    await shell.invoke("voice_set_hotkey", { key: v.ptt_key });
    return null;
  } catch (e) {
    return String(e);
  }
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

/** Mirrors the Rust `describe_bind`: "<name> · button <N+1>", falling back to the raw
 * device id when unknown, or the raw bind id when it doesn't parse. */
export function describeBind(id: string | null, devices: DeviceInfo[]): string {
  if (id === null) return "not set";
  const i = id.lastIndexOf(":");
  if (i === -1) return id;
  const device = id.slice(0, i);
  const button = Number(id.slice(i + 1));
  if (!Number.isInteger(button)) return id;
  const name = devices.find((d) => d.device === device)?.name ?? device;
  return `${name} · button ${button + 1}`;
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
