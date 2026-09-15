import { describe, it, expect } from "vitest";
import { attachShell, captureNext, describeBind, pushBinds, type ShellApi } from "./voice-shell";

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
    expect(invoked).toContainEqual(["voice_set_binds", { binds: { wheel: "winmm:0:7", gamepad: null } }]);
    expect(invoked).toContainEqual(["voice_set_hotkey", { key: "F9" }]);
    expect(await pushBinds({ ptt_key: "BAD", ptt_wheel: null, ptt_gamepad: null }, shell)).toMatch(/no such key/);
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
});
