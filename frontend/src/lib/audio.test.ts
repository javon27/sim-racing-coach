// frontend/src/lib/audio.test.ts
import { describe, it, expect, vi } from "vitest";
import { KokoroSpeaker } from "./audio";

class FakeSource {
  onended: (() => void) | null = null;
  started = false; stopped = false;
  connect() {} start() { this.started = true; } stop() { this.stopped = true; this.onended?.(); }
}
function fakeAudio() {
  const sources: FakeSource[] = [];
  const ctx = {
    state: "running", destination: {}, resume: vi.fn(),
    decodeAudioData: vi.fn(async (buf: ArrayBuffer) => ({ duration: buf.byteLength / 1000 })),
    createBufferSource: () => { const s = new FakeSource(); sources.push(s); return s; },
    createOscillator: () => ({ frequency: { value: 0 }, connect() {}, start() {}, stop() {} }),
    createGain: () => ({ gain: { setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {} }),
    currentTime: 0,
  } as unknown as AudioContext;
  return { ctx, sources };
}
const wav = (n: number) => new ArrayBuffer(n);

describe("KokoroSpeaker", () => {
  it("speak() fetches, decodes, plays and resolves on ended", async () => {
    const { ctx, sources } = fakeAudio();
    const fetchWav = vi.fn(async () => wav(100));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.voice = "am_michael"; sp.rate = 1.2; sp.trackSlug = "spa";
    const done = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(1));
    expect(fetchWav).toHaveBeenCalledWith("Turn ten.", { voice: "am_michael", speed: 1.2, trackSlug: "spa" }, expect.any(AbortSignal));
    let resolved = false; void done.then(() => (resolved = true));
    await Promise.resolve(); expect(resolved).toBe(false);
    sources[0].stop();                       // fires onended
    await done; expect(resolved).toBe(true);
  });

  it("enqueue() keeps one synthesis in flight ahead of playback and idle() drains", async () => {
    const { ctx, sources } = fakeAudio();
    let resolvers: ((b: ArrayBuffer) => void)[] = [];
    const fetchWav = vi.fn(() => new Promise<ArrayBuffer>((r) => resolvers.push(r)));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.enqueue("One."); sp.enqueue("Two."); sp.enqueue("Three.");
    await Promise.resolve();
    expect(fetchWav).toHaveBeenCalledTimes(1);         // only the head is synthesising
    resolvers[0](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(1));
    expect(fetchWav).toHaveBeenCalledTimes(2);         // next one synthesises while the first plays
    sources[0].stop(); resolvers[1](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop(); resolvers[2](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(3));
    sources[2].stop();
    await sp.idle();                                   // resolves once everything played
  });

  it("cancel() aborts the in-flight fetch, stops playback, clears the queue and resolves idle()", async () => {
    const { ctx, sources } = fakeAudio();
    const seen: AbortSignal[] = [];
    const fetchWav = vi.fn((_t: string, _o: unknown, signal: AbortSignal) => { seen.push(signal); return new Promise<ArrayBuffer>(() => {}); });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.enqueue("One."); sp.enqueue("Two.");
    await Promise.resolve();
    sp.cancel();
    expect(seen[0].aborted).toBe(true);
    await sp.idle();
    expect(fetchWav).toHaveBeenCalledTimes(1);
    expect(sources.every((s) => s.stopped || !s.started)).toBe(true);
  });

  it("reports the first failure through onUnavailable once and resolves the caller", async () => {
    const { ctx } = fakeAudio();
    const fetchWav = vi.fn(async () => { throw new Error("Kokoro voice isn't installed"); });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;
    await sp.speak("a"); await sp.speak("b");
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(onUnavailable).toHaveBeenCalledWith("Kokoro voice isn't installed");
  });
});
