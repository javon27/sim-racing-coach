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
