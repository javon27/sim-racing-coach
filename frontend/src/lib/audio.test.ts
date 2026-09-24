// frontend/src/lib/audio.test.ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { KokoroSpeaker, SynthSpeaker, makeSpeaker } from "./audio";
import { UnpronounceableError, type VoiceSettings } from "./api";

function voiceSettings(overrides: Partial<VoiceSettings> = {}): VoiceSettings {
  return {
    enabled: true, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, ptt_wheel_name: null, ptt_gamepad_name: null, tts_voice: null, tts_rate: 1,
    tts_engine: "windows", kokoro_voice: "af_heart", ptt_focus_gate: true, crewchief_wait: true,
    vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30, mic_device_id: null,
    ...overrides,
  };
}

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

  it("cancel() while a decoded sentence is waiting for the previous one to end never plays it", async () => {
    const { ctx, sources } = fakeAudio();
    const resolvers: ((b: ArrayBuffer) => void)[] = [];
    const fetchWav = vi.fn(() => new Promise<ArrayBuffer>((r) => resolvers.push(r)));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    sp.enqueue("One."); sp.enqueue("Two.");
    await Promise.resolve();
    resolvers[0](wav(10));
    await vi.waitFor(() => expect(sources.length).toBe(1));   // "One." decoded and playing
    resolvers[1](wav(10));                                    // "Two." decodes while "One." plays,
    await vi.waitFor(() => expect(fetchWav).toHaveBeenCalledTimes(2));
    // let "Two." finish decoding and start waiting on drainPlaying() for "One." to end
    await new Promise((r) => setTimeout(r, 0));
    sp.cancel();
    expect(sources[0].stopped).toBe(true);
    await sp.idle();
    await new Promise((r) => setTimeout(r, 0));
    expect(sources.length).toBe(1);                           // "Two." must never have started
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

  // #125: cancel() aborts the in-flight fetch -- that's a cancel, not an engine
  // failure, and must never trigger the "not installed" fallback.
  it("cancel() during an in-flight fetch does not report onUnavailable", async () => {
    const { ctx } = fakeAudio();
    // Matches real fetch: an aborted request rejects with an object named "AbortError".
    const fetchWav = vi.fn((_t: string, _o: unknown, signal: AbortSignal) => new Promise<ArrayBuffer>((_resolve, reject) => {
      signal.addEventListener("abort", () => {
        const e = new Error("The operation was aborted.");
        (e as Error & { name: string }).name = "AbortError";
        reject(e);
      });
    }));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;
    const done = sp.speak("hello");
    await Promise.resolve();
    sp.cancel();
    await done;
    await new Promise((r) => setTimeout(r, 0));   // let the aborted fetch's rejection settle
    expect(onUnavailable).not.toHaveBeenCalled();
  });

  // Regression guard for the real fallback: a genuine fetch failure (non-OK
  // response / network error -- e.g. a 503 or a dropped connection, as opposed to
  // the per-sentence 422 UnpronounceableError above) must still demote the whole
  // session to Windows voices via onUnavailable (#118/#123 only special-cases the
  // unpronounceable 422; everything else keeps the existing wholesale behavior).
  it("a non-abort fetch rejection still reports onUnavailable exactly once", async () => {
    const { ctx } = fakeAudio();
    const fetchWav = vi.fn(() => Promise.reject(new Error("couldn't synthesise speech")));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;
    await sp.speak("a");
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(onUnavailable).toHaveBeenCalledWith("couldn't synthesise speech");
  });

  // #118/#123: an unpronounceable sentence must not demote the whole session --
  // Windows speaks just that one utterance, and Kokoro is tried again next.
  it("a 422 unpronounceable response speaks via the Windows fallback for that utterance, not onUnavailable, and the next utterance still tries Kokoro", async () => {
    const { ctx, sources } = fakeAudio();
    const windowsFallback = { voiceName: null, rate: 1, speak: vi.fn(async () => {}) } as unknown as SynthSpeaker;
    let call = 0;
    const fetchWav = vi.fn(() => {
      call++;
      return call === 1 ? Promise.reject(new UnpronounceableError(["Kemmel"])) : Promise.resolve(wav(10));
    });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx, windowsFallback });
    sp.windowsVoiceName = "David";
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;

    await sp.speak("Watch Kemmel.");
    expect(windowsFallback.speak).toHaveBeenCalledWith("Watch Kemmel.");
    expect(windowsFallback.voiceName).toBe("David");
    expect(onUnavailable).not.toHaveBeenCalled();

    const second = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(1));   // Kokoro tried again for the next utterance
    sources[0].stop();
    await second;
    expect(fetchWav).toHaveBeenCalledTimes(2);
  });

  // #148: the fallback used to speak when its FETCH resolved, but #pump() runs a
  // sentence ahead of playback -- so Windows started talking over the Kokoro sentence
  // still playing, and the two engines leapfrogged paragraphs. It must wait its turn.
  it("an unpronounceable sentence is not spoken by Windows until the sentence before it has finished playing", async () => {
    const { ctx, sources } = fakeAudio();
    const windowsFallback = { voiceName: null, rate: 1, speak: vi.fn(async () => {}), cancel: vi.fn() } as unknown as SynthSpeaker;
    let call = 0;
    const fetchWav = vi.fn(() => {
      call++;
      return call === 1 ? Promise.resolve(wav(10)) : Promise.reject(new UnpronounceableError(["Pouhon"]));
    });
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx, windowsFallback });

    sp.enqueue("First sentence, Kokoro.");
    sp.enqueue("Then Pouhon.");
    await vi.waitFor(() => expect(sources.length).toBe(1));      // first is playing
    await vi.waitFor(() => expect(fetchWav).toHaveBeenCalledTimes(2)); // second already fetched (and 422'd)

    expect(windowsFallback.speak).not.toHaveBeenCalled();        // <- the regression

    sources[0].stop();                                           // first finishes
    await vi.waitFor(() => expect(windowsFallback.speak).toHaveBeenCalledWith("Then Pouhon."));
    await sp.idle();
  });

  // #148: Stop killed the Web Audio node but left speechSynthesis talking, because
  // the fallback is a separate engine the cancel path never touched.
  it("cancel() also stops a Windows fallback sentence that is mid-utterance", async () => {
    const { ctx } = fakeAudio();
    let release: () => void = () => {};
    const windowsFallback = {
      voiceName: null, rate: 1,
      speak: vi.fn(() => new Promise<void>((r) => { release = r; })),
      cancel: vi.fn(),
    } as unknown as SynthSpeaker;
    const fetchWav = vi.fn(() => Promise.reject(new UnpronounceableError(["Raidillon"])));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx, windowsFallback });

    const done = sp.speak("Through Raidillon.");
    await vi.waitFor(() => expect(windowsFallback.speak).toHaveBeenCalled());

    sp.cancel();
    expect(windowsFallback.cancel).toHaveBeenCalled();           // <- the regression

    release();
    await done;
  });

  it("a decodeAudioData rejection skips that sentence and continues with the next, without calling onUnavailable", async () => {
    const { ctx, sources } = fakeAudio();
    (ctx.decodeAudioData as ReturnType<typeof vi.fn>).mockImplementationOnce(async () => { throw new Error("bad wav"); });
    const fetchWav = vi.fn(async () => wav(10));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });
    const onUnavailable = vi.fn(); sp.onUnavailable = onUnavailable;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    sp.enqueue("bad one"); sp.enqueue("good one");
    await vi.waitFor(() => expect(sources.length).toBe(1));   // only "good one" ever played
    sources[0].stop();
    await sp.idle();
    expect(fetchWav).toHaveBeenCalledTimes(2);
    expect(onUnavailable).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("KokoroSpeaker decoded-audio cache (#121)", () => {
  it("replaying the same text fetches once and decodes once", async () => {
    const { ctx, sources } = fakeAudio();
    const fetchWav = vi.fn(async () => wav(100));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });

    const first = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(1));
    sources[0].stop(); await first;

    const second = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop(); await second;

    expect(fetchWav).toHaveBeenCalledTimes(1);
    expect(ctx.decodeAudioData).toHaveBeenCalledTimes(1);
    expect(sources.length).toBe(2);   // two independent sources played from the one buffer
  });

  it("a different voice/rate/trackSlug is a different cache key -- fetches again", async () => {
    const { ctx, sources } = fakeAudio();
    const fetchWav = vi.fn(async () => wav(100));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });

    const first = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(1));
    sources[0].stop(); await first;

    sp.voice = "am_michael";
    const second = sp.speak("Turn ten.");
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop(); await second;

    expect(fetchWav).toHaveBeenCalledTimes(2);
  });

  it("caches at most 64 entries; the oldest is evicted and refetched on replay", async () => {
    const { ctx, sources } = fakeAudio();
    const fetchWav = vi.fn(async () => wav(10));
    const sp = new KokoroSpeaker({ fetchWav, audio: ctx });

    for (let i = 0; i < 65; i++) {
      const p = sp.speak(`Sentence ${i}.`);
      await vi.waitFor(() => expect(sources.length).toBe(i + 1));
      sources[i].stop(); await p;
    }
    expect(fetchWav).toHaveBeenCalledTimes(65);

    // "Sentence 0." was the 65th-oldest of a 64-cap cache -- evicted, refetches.
    const evicted = sp.speak("Sentence 0.");
    await vi.waitFor(() => expect(sources.length).toBe(66));
    sources[65].stop(); await evicted;
    expect(fetchWav).toHaveBeenCalledTimes(66);

    // "Sentence 64." is the most recently cached entry -- still a hit.
    const stillCached = sp.speak("Sentence 64.");
    await vi.waitFor(() => expect(sources.length).toBe(67));
    sources[66].stop(); await stillCached;
    expect(fetchWav).toHaveBeenCalledTimes(66);
  });
});

describe("makeSpeaker", () => {
  it("returns a KokoroSpeaker with voice/rate set when the engine is kokoro and installed", () => {
    const v = voiceSettings({ tts_engine: "kokoro", kokoro_voice: "am_michael", tts_rate: 1.3 });
    const s = makeSpeaker(v, true, () => {});
    expect(s).toBeInstanceOf(KokoroSpeaker);
    expect((s as KokoroSpeaker).voice).toBe("am_michael");
    expect((s as KokoroSpeaker).rate).toBe(1.3);
  });

  it("wires onFallback as the KokoroSpeaker's onUnavailable", () => {
    const onFallback = vi.fn();
    const v = voiceSettings({ tts_engine: "kokoro" });
    const s = makeSpeaker(v, true, onFallback);
    (s as KokoroSpeaker).onUnavailable?.("network error");
    expect(onFallback).toHaveBeenCalledWith("network error");
  });

  it("falls back to SynthSpeaker when kokoro is selected but not installed", () => {
    const v = voiceSettings({ tts_engine: "kokoro", tts_voice: "Zira", tts_rate: 0.8 });
    const s = makeSpeaker(v, false, () => {});
    expect(s).toBeInstanceOf(SynthSpeaker);
    expect((s as SynthSpeaker).voiceName).toBe("Zira");
    expect((s as SynthSpeaker).rate).toBe(0.8);
  });

  it("returns a SynthSpeaker when the engine is windows, regardless of kokoroInstalled", () => {
    const v = voiceSettings({ tts_engine: "windows", tts_voice: "David", tts_rate: 1.1 });
    const s = makeSpeaker(v, true, () => {});
    expect(s).toBeInstanceOf(SynthSpeaker);
    expect((s as SynthSpeaker).voiceName).toBe("David");
    expect((s as SynthSpeaker).rate).toBe(1.1);
  });
});

// #163: a hold (CrewChief talking) gates each sentence BEFORE it starts; cancel never waits on it.
function manualHold() {
  const releases: (() => void)[] = [];
  const hold = Object.assign(
    vi.fn((_cancelled: () => boolean) => new Promise<void>((r) => releases.push(r))),
    { reset: vi.fn() },
  );
  return { hold, releases };
}

describe("KokoroSpeaker hold (#163)", () => {
  it("holds each sentence until the hold releases, between sentences only", async () => {
    const { ctx, sources } = fakeAudio();
    const sp = new KokoroSpeaker({ fetchWav: vi.fn(async () => wav(10)), audio: ctx });
    const { hold, releases } = manualHold();
    sp.hold = hold;
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    expect(sources.length).toBe(0);                    // held: CrewChief is talking
    releases[0]();
    await vi.waitFor(() => expect(sources.length).toBe(1));
    expect(hold).toHaveBeenCalledTimes(1);             // "Two." waits for "One." to end first
    sources[0].stop();
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(2));
    expect(sources.length).toBe(1);
    releases[1]();
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop();
    await sp.idle();
  });

  it("idle() stays pending while a sentence is held", async () => {
    const { ctx } = fakeAudio();
    const sp = new KokoroSpeaker({ fetchWav: vi.fn(async () => wav(10)), audio: ctx });
    const { hold } = manualHold();
    sp.hold = hold;
    sp.enqueue("One.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalled());
    let idle = false; void sp.idle().then(() => (idle = true));
    await new Promise((r) => setTimeout(r, 10));
    expect(idle).toBe(false);
  });

  it("cancel() while held drops every held/queued sentence at once, even if the hold never returns", async () => {
    const { ctx, sources } = fakeAudio();
    const sp = new KokoroSpeaker({ fetchWav: vi.fn(async () => wav(10)), audio: ctx });
    const { hold, releases } = manualHold();
    sp.hold = hold;
    const spoken = sp.speak("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    expect(hold.mock.calls[0][0]()).toBe(false);
    sp.cancel();
    expect(hold.mock.calls[0][0]()).toBe(true);        // the hold is told to stop waiting
    await spoken; await sp.idle();                     // resolved without the hold returning
    releases[0]();
    await new Promise((r) => setTimeout(r, 10));
    expect(sources.length).toBe(0);                    // nothing held ever plays
  });
  it("a rejecting hold still speaks the sentence, and the next one", async () => {
    const { ctx, sources } = fakeAudio();
    const sp = new KokoroSpeaker({ fetchWav: vi.fn(async () => wav(10)), audio: ctx });
    sp.hold = vi.fn(async () => { throw new Error("boom"); });
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(sources.length).toBe(1));
    sources[0].stop();
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop();
    await sp.idle();
  });

  it("resets the hold's answer budget on cancel and when it goes idle, not between queued sentences", async () => {
    const { ctx, sources } = fakeAudio();
    const sp = new KokoroSpeaker({ fetchWav: vi.fn(async () => wav(10)), audio: ctx });
    const { hold, releases } = manualHold();
    sp.hold = hold;
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    releases[0]();
    await vi.waitFor(() => expect(sources.length).toBe(1));
    sources[0].stop();
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(2));
    expect(hold.reset).not.toHaveBeenCalled();         // same answer: budget carries over
    releases[1]();
    await vi.waitFor(() => expect(sources.length).toBe(2));
    sources[1].stop();
    await sp.idle();
    expect(hold.reset).toHaveBeenCalledTimes(1);       // drained: next answer starts fresh
    sp.cancel();
    expect(hold.reset).toHaveBeenCalledTimes(2);
  });
});

describe("SynthSpeaker hold (#163)", () => {
  afterEach(() => { vi.unstubAllGlobals(); });
  function fakeSynth() {
    const spoken: { text: string; onend: (() => void) | null; onerror: (() => void) | null }[] = [];
    vi.stubGlobal("SpeechSynthesisUtterance", class { onend: (() => void) | null = null; onerror: (() => void) | null = null; rate = 1; voice = null; constructor(public text: string) {} });
    vi.stubGlobal("speechSynthesis", { speak: (u: (typeof spoken)[number]) => spoken.push(u), cancel: vi.fn(), getVoices: () => [] });
    return spoken;
  }

  it("with no hold, enqueue() hands every sentence to the native queue at once (as before)", () => {
    const spoken = fakeSynth();
    const sp = new SynthSpeaker();
    sp.enqueue("One."); sp.enqueue("Two.");
    expect(spoken.map((u) => u.text)).toEqual(["One.", "Two."]);
  });

  it("holds each sentence, speaking the next only after the previous ended and its hold released", async () => {
    const spoken = fakeSynth();
    const sp = new SynthSpeaker();
    const { hold, releases } = manualHold();
    sp.hold = hold;
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    expect(spoken.length).toBe(0);
    releases[0]();
    await vi.waitFor(() => expect(spoken.length).toBe(1));
    expect(hold).toHaveBeenCalledTimes(1);
    spoken[0].onend!();
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(2));
    expect(spoken.length).toBe(1);
    releases[1]();
    await vi.waitFor(() => expect(spoken.length).toBe(2));
    spoken[1].onend!();
    await sp.idle();
  });

  it("tells the held sentence to stop waiting on cancel()", async () => {
    fakeSynth();
    const sp = new SynthSpeaker();
    const { hold } = manualHold();
    sp.hold = hold;
    sp.enqueue("One.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    const cancelled = hold.mock.calls[0][0];
    expect(cancelled()).toBe(false);
    sp.cancel();
    expect(cancelled()).toBe(true);
    expect(hold.reset).toHaveBeenCalled();
  });

  it("a cancelled utterance's late onerror does not settle the next answer", async () => {
    const spoken = fakeSynth();
    const sp = new SynthSpeaker();
    const { hold, releases } = manualHold();
    sp.hold = hold;
    sp.enqueue("One.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    releases[0]();
    await vi.waitFor(() => expect(spoken.length).toBe(1));   // "One." is speaking
    sp.cancel();
    sp.enqueue("Two.");                                       // next answer pending
    let idle = false; void sp.idle().then(() => (idle = true));
    spoken[0].onerror!();                                     // the engine's late "interrupted"
    await new Promise((r) => setTimeout(r, 10));
    expect(idle).toBe(false);
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(2));
    releases[1]();
    await vi.waitFor(() => expect(spoken.length).toBe(2));
    spoken[1].onend!();
    await vi.waitFor(() => expect(idle).toBe(true));
  });

  it("a rejecting hold does not break the chain: every sentence is still spoken", async () => {
    const spoken = fakeSynth();
    const sp = new SynthSpeaker();
    sp.hold = vi.fn(async () => { throw new Error("boom"); });
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(spoken.length).toBe(1));
    spoken[0].onend!();
    await vi.waitFor(() => expect(spoken.map((u) => u.text)).toEqual(["One.", "Two."]));
    spoken[1].onend!();
    await sp.idle();
  });

  it("cancel() while held drops everything and a new answer does not wait on the stale hold", async () => {
    const spoken = fakeSynth();
    const sp = new SynthSpeaker();
    const { hold, releases } = manualHold();
    sp.hold = hold;
    sp.enqueue("One."); sp.enqueue("Two.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(1));
    sp.cancel();
    await sp.idle();
    sp.enqueue("Three.");
    await vi.waitFor(() => expect(hold).toHaveBeenCalledTimes(2));
    releases[1]();
    await vi.waitFor(() => expect(spoken.map((u) => u.text)).toEqual(["Three."]));
    releases[0]();
    await new Promise((r) => setTimeout(r, 10));
    expect(spoken.map((u) => u.text)).toEqual(["Three."]);   // "One."/"Two." were dropped
  });
});

describe("makeSpeaker CrewChief hold (#163)", () => {
  it("has no hold outside the Tauri shell or with the setting off -- speech is ungated", () => {
    for (const engine of ["windows", "kokoro"] as const) {
      for (const crewchief_wait of [true, false]) {
        const s = makeSpeaker(voiceSettings({ tts_engine: engine, crewchief_wait }), true, () => {});
        expect((s as SynthSpeaker | KokoroSpeaker).hold).toBeNull();
      }
    }
  });

  it("gets a hold inside the Tauri shell when the setting is on", () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    try {
      for (const engine of ["windows", "kokoro"] as const) {
        const on = makeSpeaker(voiceSettings({ tts_engine: engine, crewchief_wait: true }), true, () => {});
        const off = makeSpeaker(voiceSettings({ tts_engine: engine, crewchief_wait: false }), true, () => {});
        expect((on as SynthSpeaker | KokoroSpeaker).hold).toBeTypeOf("function");
        expect((off as SynthSpeaker | KokoroSpeaker).hold).toBeNull();
      }
    } finally {
      delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    }
  });
});
