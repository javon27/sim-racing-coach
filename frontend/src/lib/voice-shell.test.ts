import { describe, it, expect, vi, afterEach } from "vitest";
import { attachShell, CC_MAX_WAIT_MS, captureNext, crewChiefHold, describeBind, deviceIdOf, isStaleBind, pushBinds, type ShellApi } from "./voice-shell";

function fakeShell() {
  const handlers = new Map<string, (p: unknown) => void>();
  const invoked: [string, unknown][] = [];
  const shell: ShellApi = {
    async listen(ev, cb) { handlers.set(ev, cb); return () => handlers.delete(ev); },
    async invoke(cmd, args) { invoked.push([cmd, args]); if (cmd === "voice_set_hotkey" && (args as any).key === "BAD") throw "no such key"; return null; },
  };
  return { shell, handlers, invoked };
}

describe("voice-shell", () => {
  it("routes press/release events to the loop and detaches", async () => {
    const { shell, handlers } = fakeShell();
    const calls: string[] = [];
    const detach = await attachShell({ press: () => calls.push("p"), release: () => calls.push("r") }, shell);
    handlers.get("voice:press")!(null); handlers.get("voice:release")!(null);
    expect(calls).toEqual(["p", "r"]);
    detach(); expect(handlers.size).toBe(0);
  });
  it("pushBinds sends binds + hotkey and surfaces a registration error", async () => {
    const { shell, invoked } = fakeShell();
    expect(await pushBinds({ ptt_key: "F9", ptt_wheel: "winmm:0:7", ptt_gamepad: null }, shell)).toBe(null);
    expect(invoked).toContainEqual([
      "voice_set_binds",
      { binds: { wheel: "winmm:0:7", gamepad: null }, focus_gate: true },
    ]);
    expect(invoked).toContainEqual(["voice_set_hotkey", { key: "F9" }]);
    expect(await pushBinds({ ptt_key: "BAD", ptt_wheel: null, ptt_gamepad: null }, shell)).toMatch(/no such key/);
  });

  it("pushBinds forwards focus_gate (true by default, false when the setting is off, #112)", async () => {
    const { shell, invoked } = fakeShell();
    await pushBinds({ ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null }, shell);
    expect(invoked).toContainEqual(["voice_set_binds", { binds: { wheel: null, gamepad: null }, focus_gate: true }]);
    await pushBinds({ ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, ptt_focus_gate: false }, shell);
    expect(invoked).toContainEqual(["voice_set_binds", { binds: { wheel: null, gamepad: null }, focus_gate: false }]);
  });
  it("captureNext arms capture and resolves with the captured id, or null on timeout", async () => {
    const { shell, handlers, invoked } = fakeShell();
    const p = captureNext(shell, 1000);
    await new Promise((r) => setTimeout(r, 0));
    expect(invoked.at(-1)![0]).toBe("voice_capture_next");
    handlers.get("voice:captured")!({ id: "xinput:0:4" });
    expect(await p).toBe("xinput:0:4");
    expect(await captureNext(shell, 10)).toBe(null);
  });
  it("describeBind names the device and button, or falls back", () => {
    const devices = [{ device: "winmm:0", name: "Fanatec DD1" }];
    expect(describeBind("winmm:0:1", devices)).toBe("Fanatec DD1 · button 2");
    expect(describeBind("winmm:3:1", devices)).toBe("winmm:3 · button 2");
    expect(describeBind("garbage", devices)).toBe("garbage");
    expect(describeBind(null, devices)).toBe("not set");
  });

  it("describeBind: live name wins over a stored name (#104)", () => {
    const devices = [{ device: "winmm:0", name: "Fanatec DD1" }];
    expect(describeBind("winmm:0:1", devices, "Old Stored Name")).toBe("Fanatec DD1 · button 2");
  });

  it("describeBind: falls back to the stored name with a not-connected hint when the device isn't listed (#104)", () => {
    expect(describeBind("winmm:3:1", [], "Xbox controller 2")).toBe("Xbox controller 2 · button 2 (not connected)");
  });

  it("describeBind: falls back to the raw id when there's no live or stored name (#104)", () => {
    expect(describeBind("winmm:3:1", [], null)).toBe("winmm:3 · button 2");
    expect(describeBind("garbage", [], "Stored")).toBe("garbage");
  });

  it("deviceIdOf extracts the device id from a bind id (#104)", () => {
    expect(deviceIdOf("winmm:0:1")).toBe("winmm:0");
    expect(deviceIdOf("xinput:0:4")).toBe("xinput:0");
    expect(deviceIdOf("garbage")).toBe(null);
    expect(deviceIdOf(null)).toBe(null);
  });

  it("isStaleBind flags a winmm: id (#144 -- can't be migrated to hid:) and nothing else", () => {
    expect(isStaleBind("winmm:0:7")).toBe(true);
    expect(isStaleBind("hid:046d:c276:7")).toBe(false);
    expect(isStaleBind("xinput:0:4")).toBe(false);
    expect(isStaleBind(null)).toBe(false);
  });
});

// #163: the shell reports CrewChief active (threshold + quiet tail live in Rust).
describe("crewChiefHold", () => {
  afterEach(() => { vi.useRealTimers(); });
  function ccShell(active: () => boolean | Error) {
    const shell: ShellApi = {
      async listen() { return () => {}; },
      async invoke(cmd) {
        expect(cmd).toBe("crewchief_active");
        const a = active(); if (a instanceof Error) throw a; return a;
      },
    };
    return shell;
  }
  async function settled(p: Promise<void>): Promise<boolean> {
    let done = false; void p.then(() => (done = true));
    await vi.advanceTimersByTimeAsync(0);
    return done;
  }

  it("holds while CrewChief is active and releases once it goes quiet", async () => {
    vi.useFakeTimers();
    let active = true;
    const p = crewChiefHold(ccShell(() => active))(() => false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await settled(p)).toBe(false);
    active = false;                                   // Rust tail elapsed
    await vi.advanceTimersByTimeAsync(60);
    expect(await settled(p)).toBe(true);
  });

  it("speaks anyway after CC_MAX_WAIT_MS", async () => {
    vi.useFakeTimers();
    const p = crewChiefHold(ccShell(() => true))(() => false);
    await vi.advanceTimersByTimeAsync(CC_MAX_WAIT_MS - 100);
    expect(await settled(p)).toBe(false);
    await vi.advanceTimersByTimeAsync(200);
    expect(await settled(p)).toBe(true);
  });

  it("returns as soon as the sentence is cancelled", async () => {
    vi.useFakeTimers();
    let cancelled = false;
    const p = crewChiefHold(ccShell(() => true))(() => cancelled);
    await vi.advanceTimersByTimeAsync(500);
    cancelled = true;
    await vi.advanceTimersByTimeAsync(60);
    expect(await settled(p)).toBe(true);
  });

  it("never holds when the shell can't answer (no command / no CrewChief)", async () => {
    vi.useFakeTimers();
    expect(await settled(crewChiefHold(ccShell(() => new Error("unknown command")))(() => false))).toBe(true);
    expect(await settled(crewChiefHold(ccShell(() => false))(() => false))).toBe(true);
  });
});
