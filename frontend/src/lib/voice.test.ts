import { describe, it, expect } from "vitest";
import { encodeWav, rms, stripForSpeech, vadInitial, vadStep, type VadConfig } from "./voice";

const cfg: VadConfig = { threshold: 0.02, silenceMs: 1000, maxS: 20 };

describe("vadStep", () => {
  it("does not end on silence before speech has started", () => {
    let s = vadInitial;
    for (let i = 0; i < 100; i++) {
      const r = vadStep(s, 0.0, 50, cfg);
      expect(r.action).toBe("continue");
      s = r.state;
    }
    expect(s.speaking).toBe(false);
  });
  it("ends after silenceMs of quiet once speech was heard", () => {
    let s = vadStep(vadInitial, 0.1, 50, cfg).state;
    expect(s.speaking).toBe(true);
    let action = "continue";
    let frames = 0;
    while (action === "continue") {
      const r = vadStep(s, 0.001, 50, cfg);
      s = r.state; action = r.action; frames++;
    }
    expect(frames).toBe(20); // 1000 ms / 50 ms
  });
  it("a burst of speech resets the silence counter", () => {
    let s = vadStep(vadInitial, 0.1, 50, cfg).state;
    for (let i = 0; i < 10; i++) s = vadStep(s, 0, 50, cfg).state;
    s = vadStep(s, 0.1, 50, cfg).state;
    expect(s.silentMs).toBe(0);
  });
  it("hard-caps at maxS even while speaking", () => {
    let s = vadInitial; let action = "continue"; let ms = 0;
    while (action === "continue") { const r = vadStep(s, 0.5, 100, cfg); s = r.state; action = r.action; ms += 100; }
    expect(ms).toBe(20000);
  });
});

describe("encodeWav", () => {
  it("writes a 44-byte header + 16-bit samples", () => {
    const wav = encodeWav(new Float32Array([0, 0.5, -0.5, 1, -1]), 16000);
    expect(wav.length).toBe(44 + 10);
    const dv = new DataView(wav.buffer);
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...wav.slice(8, 12))).toBe("WAVE");
    expect(dv.getUint32(24, true)).toBe(16000);      // sample rate
    expect(dv.getUint16(22, true)).toBe(1);          // mono
    expect(dv.getUint16(34, true)).toBe(16);         // bits
    expect(dv.getInt16(46, true)).toBe(16383);       // 0.5 → 16383/16384 range
    expect(dv.getInt16(50, true)).toBe(32767);       // 1 clamps
    expect(dv.getInt16(52, true)).toBe(-32768);      // -1 clamps
  });
});

describe("stripForSpeech", () => {
  it("removes markdown that would be read literally", () => {
    expect(stripForSpeech("Brake **later** into `T4`.\n\n- one\n- two\n\n## Heading\nDone."))
      .toBe("Brake later into T4. one. two. Heading. Done.");
  });
});

describe("rms", () => {
  it("is 0 for silence and ~0.707 for a full-scale square", () => {
    expect(rms(new Float32Array(100))).toBe(0);
    expect(rms(new Float32Array([1, -1, 1, -1]))).toBeCloseTo(1, 5);
    expect(rms(new Float32Array([0.5, -0.5]))).toBeCloseTo(0.5, 5);
  });
});

import { VoiceLoop, type Recorder, type Speaker, type Transcriber, type VoiceMode } from "./voice";

class FakeRecorder implements Recorder {
  started = 0; stopped = 0; onFrame: ((f: Float32Array, sampleRate?: number) => void) | null = null; failStart = false;
  startGate: Promise<void> | null = null;
  async start(onFrame: (f: Float32Array, sampleRate?: number) => void) {
    if (this.startGate) await this.startGate;
    if (this.failStart) throw new Error("NotAllowedError");
    this.started++; this.onFrame = onFrame;
  }
  async stop() { this.stopped++; this.onFrame = null; return { pcm: new Float32Array(160), sampleRate: 16000 }; }
  get open() { return this.started > this.stopped; }
  frame(level: number) { this.onFrame?.(new Float32Array(800).fill(level)); }
}
class FakeTranscriber implements Transcriber {
  text = "where am I losing time"; fail: Error | null = null; calls = 0;
  async transcribe() { this.calls++; if (this.fail) throw this.fail; return { text: this.text, seconds: 1 }; }
}
class FakeSpeaker implements Speaker {
  spoken: string[] = []; enqueued: string[] = []; earcons: string[] = []; cancels = 0; resolve: (() => void) | null = null;
  #pending = 0; #idleWaiters: (() => void)[] = [];
  speak(text: string) { this.spoken.push(text); return new Promise<void>((r) => (this.resolve = r)); }
  enqueue(text: string) { this.enqueued.push(text); this.#pending++; }
  idle(): Promise<void> {
    if (this.#pending === 0) return Promise.resolve();
    return new Promise((r) => this.#idleWaiters.push(r));
  }
  // Resolve every enqueue()d utterance, like the native queue finishing playback.
  drain() {
    this.#pending = 0;
    const waiters = this.#idleWaiters; this.#idleWaiters = [];
    waiters.forEach((w) => w());
  }
  cancel() {
    this.cancels++; this.resolve?.(); this.resolve = null;
    this.#pending = 0;
    const waiters = this.#idleWaiters; this.#idleWaiters = [];
    waiters.forEach((w) => w());
  }
  earcon(k: "listen-start" | "listen-end" | "error") { this.earcons.push(k); }
  end() { this.resolve?.(); this.resolve = null; }
}
const flush = () => new Promise((r) => setTimeout(r, 0));

function make(over: Partial<{ idle: number }> = {}) {
  const recorder = new FakeRecorder(), transcriber = new FakeTranscriber(), speaker = new FakeSpeaker();
  const asked: [string, VoiceMode][] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const loop = new VoiceLoop({
    recorder, transcriber, speaker, ask: (t, m) => asked.push([t, m]),
    vad: { threshold: 0.02, silenceMs: 1000, maxS: 20 }, conversationIdleS: over.idle ?? 30, frameMs: 50,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
  });
  return { loop, recorder, transcriber, speaker, asked, timers };
}

describe("VoiceLoop PTT", () => {
  it("press → listening, release → transcribing → asking, done → speaks spoken → idle", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.press(); await flush();
    expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true); expect(speaker.earcons).toEqual(["listen-start"]);
    loop.release(); await flush(); await flush();
    expect(recorder.open).toBe(false);
    expect(asked).toEqual([["where am I losing time", "ptt"]]); expect(loop.state).toBe("asking");
    loop.onAnswer("Full **answer**.\n\nMore.", "Brake later."); await flush();
    expect(loop.state).toBe("speaking"); expect(speaker.spoken).toEqual(["Brake later."]);
    speaker.end(); await flush();
    expect(loop.state).toBe("idle");
  });
  it("onSpoken speaks before onAnswer; onAnswer does not re-speak", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onSpoken("Brake later into four."); await flush();
    expect(loop.state).toBe("speaking"); expect(speaker.spoken).toEqual(["Brake later into four."]);
    // done arrives while the cue is still being spoken: no second utterance.
    loop.onAnswer("Full answer.", "Brake later into four."); await flush();
    expect(speaker.spoken).toEqual(["Brake later into four."]);
    speaker.end(); await flush();
    expect(loop.state).toBe("idle");
  });
  it("onSpoken outside asking/ptt is ignored", async () => {
    const { loop, speaker } = make();
    loop.onSpoken("Brake later."); await flush();
    expect(loop.state).toBe("idle"); expect(speaker.spoken).toEqual([]);
  });
  it("falls back to the first paragraph when spoken is missing", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onAnswer("First **para**.\n\nSecond.", null); await flush();
    expect(speaker.spoken).toEqual(["First para."]);
  });
  it("short transcript: says didn't catch that, sends nothing, idle", async () => {
    const { loop, transcriber, speaker, asked } = make();
    transcriber.text = "uh";
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    expect(asked).toEqual([]); expect(loop.state).toBe("idle");
    expect(speaker.spoken[0]).toMatch(/didn't catch/i); expect(speaker.earcons).toContain("error");
  });
  it("press during speaking cancels speech and listens again (barge-in)", async () => {
    const { loop, speaker, recorder } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onAnswer("A.", "A."); await flush();
    loop.press(); await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true);
  });
  it("release before start resolves waits for start then transcribes", async () => {
    const { loop, recorder, transcriber } = make();
    let resolveGate: () => void = () => {};
    recorder.startGate = new Promise((r) => { resolveGate = r; });
    loop.press();
    loop.release();
    await flush();
    expect(transcriber.calls).toBe(0);
    resolveGate();
    await flush(); await flush(); await flush();
    expect(transcriber.calls).toBe(1);
    expect(recorder.stopped).toBe(1);
  });
  it("release without press, or press while asking, is ignored", async () => {
    const { loop, recorder, asked } = make();
    loop.release(); expect(loop.state).toBe("idle");
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.press(); await flush();
    expect(loop.state).toBe("asking"); expect(recorder.started).toBe(1); expect(asked.length).toBe(1);
  });
});

describe("VoiceLoop errors always end idle with the mic closed", () => {
  it("mic denied", async () => {
    const { loop, recorder, speaker } = make(); recorder.failStart = true;
    loop.press(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    expect(speaker.spoken[0]).toMatch(/can't hear the mic/i); expect(loop.error).toBeTruthy();
  });
  it("mic denied does not play the error earcon", async () => {
    const { loop, recorder, speaker } = make(); recorder.failStart = true;
    loop.press(); await flush();
    expect(speaker.earcons).not.toContain("error");
  });
  it("409 from transcribe", async () => {
    const { loop, recorder, transcriber, speaker } = make();
    transcriber.fail = new Error("voice isn't set up yet — enable it in Settings");
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    expect(speaker.spoken[0]).toMatch(/set up/); expect(speaker.earcons).toContain("error");
  });
  it("coach error", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onError("coach error"); await flush();
    expect(loop.state).toBe("idle"); expect(speaker.spoken).toContain("coach error");
  });
});

describe("VoiceLoop conversation", () => {
  it("tap → listening; VAD end → ask; sentences spoken as they stream; then listening again", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.toggleConversation(); await flush();
    expect(loop.state).toBe("listening"); expect(loop.mode).toBe("conversation");
    recorder.frame(0.2);                       // speech
    for (let i = 0; i < 20; i++) recorder.frame(0);   // 1000 ms silence
    await flush(); await flush();
    expect(asked).toEqual([["where am I losing time", "conversation"]]);
    loop.onText("Full **answer**.\n\n- one"); await flush();
    expect(loop.state).toBe("speaking");
    expect(speaker.enqueued).toEqual(["Full answer."]); // "- one" has no terminator yet, stays buffered
    loop.onAnswer("Full **answer**.\n\n- one", null); await flush(); // done: flush the "- one" remainder
    expect(speaker.enqueued).toEqual(["Full answer.", "one."]);
    speaker.drain(); await flush();
    expect(loop.state).toBe("listening"); expect(recorder.open).toBe(true);
  });
  it("speech during speaking barges in", async () => {
    const { loop, recorder, speaker } = make();
    loop.toggleConversation(); await flush();
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0); await flush(); await flush();
    loop.onText("Answer."); await flush();
    expect(loop.state).toBe("speaking");
    // Sustained loud speech (>= 2x threshold for BARGE_IN_MS) interrupts.
    for (let i = 0; i < 8; i++) recorder.frame(0.3);
    await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening");
  });
  it("noise during speaking does not barge in (rig: playback kept cutting itself off)", async () => {
    const { loop, recorder, speaker } = make();
    loop.toggleConversation(); await flush();
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0); await flush(); await flush();
    loop.onText("Answer."); await flush();
    expect(loop.state).toBe("speaking");
    // A single loud frame, a quiet gap, then another: no sustained burst.
    recorder.frame(0.3); recorder.frame(0); recorder.frame(0.3); await flush();
    expect(speaker.cancels).toBe(0); expect(loop.state).toBe("speaking");
    // Sustained but only at the LISTENING threshold (playback bleed): still no.
    for (let i = 0; i < 20; i++) recorder.frame(0.03);
    await flush();
    expect(speaker.cancels).toBe(0); expect(loop.state).toBe("speaking");
    // 7 loud frames = 350 ms < 400 ms: not yet.
    for (let i = 0; i < 7; i++) recorder.frame(0.3);
    await flush();
    expect(loop.state).toBe("speaking");
    recorder.frame(0.3); await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening");
  });
  it("tap again → idle with the mic closed; idle timeout → idle", async () => {
    const { loop, recorder, timers } = make({ idle: 30 });
    loop.toggleConversation(); await flush();
    loop.toggleConversation(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
    loop.toggleConversation(); await flush();
    const t = timers.find((x) => x.ms === 30000); expect(t).toBeTruthy();
    t!.fn(); await flush();
    expect(loop.state).toBe("idle"); expect(recorder.open).toBe(false);
  });
  it("sentences are enqueued as text streams, listening resumes after the queue drains", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.toggleConversation(); await flush();
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0);
    await flush(); await flush();
    expect(asked).toEqual([["where am I losing time", "conversation"]]);

    loop.onText("Brake later into four. ");
    await flush();
    expect(loop.state).toBe("speaking");
    expect(speaker.enqueued).toEqual(["Brake later into four."]);
    expect(recorder.open).toBe(true); // mic reopened for barge-in

    loop.onText("You're carrying good speed through five.\n");
    await flush();
    expect(speaker.enqueued).toEqual(["Brake later into four.", "You're carrying good speed through five."]);

    loop.onText("More to come"); // no terminator yet -- stays buffered
    loop.onAnswer("Full answer text here.", null); // done: flush the remainder
    await flush();
    expect(speaker.enqueued).toEqual([
      "Brake later into four.", "You're carrying good speed through five.", "More to come",
    ]);
    expect(loop.state).toBe("speaking"); // still waiting on the queue to drain

    speaker.drain(); await flush();
    expect(loop.state).toBe("listening"); expect(loop.mode).toBe("conversation"); expect(recorder.open).toBe(true);
  });
  it("barge-in mid-stream clears the queue and drops the buffer", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.toggleConversation(); await flush();
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0); await flush(); await flush();
    loop.onText("First sentence. Partial leftover without a terminator");
    await flush();
    expect(loop.state).toBe("speaking");
    expect(speaker.enqueued).toEqual(["First sentence."]);

    for (let i = 0; i < 8; i++) recorder.frame(0.3); // sustained loud speech -> barge-in
    await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening");

    // A new utterance starts; the dropped buffer must not leak into it.
    recorder.frame(0.2); for (let i = 0; i < 20; i++) recorder.frame(0); await flush(); await flush();
    expect(asked).toEqual([
      ["where am I losing time", "conversation"], ["where am I losing time", "conversation"],
    ]);
    loop.onText("New sentence."); await flush();
    // "First sentence." (already enqueued before the barge-in) stays queued;
    // only the buffered, not-yet-spoken "Partial leftover..." was dropped.
    expect(speaker.enqueued).toEqual(["First sentence.", "New sentence."]);
  });
  it("PTT ignores onText", async () => {
    const { loop, speaker } = make();
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    expect(loop.state).toBe("asking"); expect(loop.mode).toBe("ptt");
    loop.onText("Some streamed text.");
    await flush();
    expect(speaker.enqueued).toEqual([]);
    expect(loop.state).toBe("asking");
  });
  it("tap converts a PTT listen into conversation without stopping the recorder", async () => {
    const { loop, recorder } = make();
    loop.press(); await flush();
    expect(loop.state).toBe("listening"); expect(loop.mode).toBe("ptt");
    loop.toggleConversation();
    expect(loop.state).toBe("listening"); expect(loop.mode).toBe("conversation");
    expect(recorder.started).toBe(1); expect(recorder.stopped).toBe(0);
  });
});

describe("frame clock", () => {
  it("times VAD and barge-in from the chunk's real duration when the recorder reports a rate (rig: 256 ms chunks counted as 50 ms)", async () => {
    const { loop, recorder, speaker, asked } = make();
    loop.toggleConversation(); await flush();
    // 4096-sample chunks at 16 kHz = 256 ms each: speech, then 4 silent chunks = 1024 ms >= silenceMs.
    const chunk = (level: number) => recorder.onFrame?.(new Float32Array(4096).fill(level), 16000);
    chunk(0.2); for (let i = 0; i < 4; i++) chunk(0);
    await flush(); await flush();
    expect(asked.length).toBe(1);
    loop.onText("Answer."); await flush();
    expect(loop.state).toBe("speaking");
    // Two loud 256 ms chunks = 512 ms >= BARGE_IN_MS: interrupts (was 8 chunks = 2 s).
    chunk(0.3); chunk(0.3); await flush();
    expect(speaker.cancels).toBe(1); expect(loop.state).toBe("listening");
  });
});

describe("mic release after idle", () => {
  it("disposes the recorder after MIC_RELEASE_S idle, not while active, and re-acquires on the next press", async () => {
    const recorder = new FakeRecorder(), transcriber = new FakeTranscriber(), speaker = new FakeSpeaker();
    let disposed = 0;
    (recorder as unknown as { dispose: () => Promise<void> }).dispose = async () => { disposed++; };
    const timers: { fn: () => void; ms: number }[] = [];
    const loop = new VoiceLoop({
      recorder, transcriber, speaker, ask: () => {},
      vad: { threshold: 0.02, silenceMs: 1000, maxS: 20 }, conversationIdleS: 30, frameMs: 50, micReleaseS: 60,
      setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
    });
    loop.press(); await flush(); loop.release(); await flush(); await flush();
    loop.onAnswer("A.", "A."); await flush(); speaker.end(); await flush();
    expect(loop.state).toBe("idle");
    const release = timers.filter((t) => t.ms === 60000).at(-1);
    expect(release).toBeTruthy();
    release!.fn(); await flush();
    expect(disposed).toBe(1);
    // A press after release re-acquires (start called again) and a release timer
    // armed while listening must not fire a dispose.
    loop.press(); await flush();
    expect(recorder.started).toBe(2);
    release!.fn(); await flush();
    expect(disposed).toBe(1);
  });
});

describe("speakMessage (▶)", () => {
  it("toggles, is exclusive, and a PTT press stops it", async () => {
    const { loop, speaker } = make();
    loop.speakMessage(0, "**One**"); expect(loop.speakingMessage).toBe(0); expect(speaker.spoken).toEqual(["One"]);
    loop.speakMessage(1, "Two"); expect(speaker.cancels).toBe(1); expect(loop.speakingMessage).toBe(1);
    loop.speakMessage(1, "Two"); expect(loop.speakingMessage).toBe(null); expect(loop.state).toBe("idle");
    loop.speakMessage(2, "Three"); loop.press(); await flush();
    expect(loop.speakingMessage).toBe(null); expect(loop.state).toBe("listening");
  });
});
