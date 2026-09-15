import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/svelte";
import { getChatMessages, getChatUsage, getSettings, type ChatUsage, type SettingsResponse } from "./api";
import type { CoachPhase, ToolActivity } from "./coach";
import { VoiceLoop, type VoiceMode } from "./voice";
import ChatPanel from "./ChatPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getChatMessages: vi.fn(), getChatUsage: vi.fn(), getSettings: vi.fn() };
});

// Only used by the mic-device-change test below (#104): every other voice test
// injects a `voice` prop, which short-circuits ChatPanel before it ever builds a
// real MicRecorder, so exercising the dispose-on-device-change path needs the
// real ./audio module replaced instead.
const { micInstances } = vi.hoisted(() => ({
  micInstances: [] as { deviceId: string | null; dispose: () => Promise<void> }[],
}));
vi.mock("./audio", async () => {
  const actual = await vi.importActual<typeof import("./audio")>("./audio");
  class FakeMicRecorder {
    deviceId: string | null;
    dispose = vi.fn(async () => {});
    constructor(deviceId: string | null = null) {
      this.deviceId = deviceId;
      micInstances.push(this);
    }
    async start() {}
    async stop() {
      return { pcm: new Float32Array(0), sampleRate: 16000 };
    }
  }
  class FakeSynthSpeaker {
    static available() { return true; }
    voiceName: string | null = null;
    rate = 1;
    async speak() {}
    cancel() {}
    earcon() {}
  }
  return { ...actual, MicRecorder: FakeMicRecorder, SynthSpeaker: FakeSynthSpeaker };
});

function usage(overrides: Partial<ChatUsage> = {}): ChatUsage {
  return { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0, messages: 0, ...overrides };
}

// Drives the panel the same way CoachStream would, without a real WebSocket.
class FakeStream {
  phase: CoachPhase = "idle";
  toolActivity: ToolActivity | null = null;
  answer = "";
  spoken: string | null = null;
  chatId: string | null = null;
  error: string | null = null;
  usage: ChatUsage | null = null;
  cost: number | null = null;
  costComplete = true;
  longChat = false;
  lastAsk: { question: string; opts: { sessionId?: string; lapId?: number; chatId?: string; voice?: VoiceMode } } | null = null;
  ask = vi.fn((question: string, opts: { sessionId?: string; lapId?: number; chatId?: string; voice?: VoiceMode } = {}) => {
    this.lastAsk = { question, opts };
  });
  #listeners = new Set<(value: this) => void>();

  subscribe(run: (value: this) => void): () => void {
    this.#listeners.add(run);
    run(this);
    return () => this.#listeners.delete(run);
  }

  notify(): void {
    for (const listener of this.#listeners) listener(this);
  }

  // Sets fields from `patch` then notifies -- a terser `notify()` for tests that
  // don't need to inspect intermediate states.
  emit(patch: Partial<FakeStream>): void {
    Object.assign(this, patch);
    this.notify();
  }
}

describe("ChatPanel", () => {
  beforeEach(() => {
    vi.mocked(getChatMessages).mockReset();
    vi.mocked(getChatUsage).mockReset();
    vi.mocked(getChatUsage).mockResolvedValue({ usage: usage(), cost: 0, cost_complete: true });
    vi.mocked(getSettings).mockReset();
    vi.mocked(getSettings).mockResolvedValue({ show_cost: true } as SettingsResponse);
    micInstances.length = 0;
  });

  afterEach(cleanup);

  it("cold load starts an empty new chat (does NOT auto-open an old chat)", async () => {
    // A fresh app load must NOT surprise the driver by opening a past chat —
    // it shows the empty new-chat state. (Past chats are resumed via Sessions.)
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: 9,
        question: "where do I brake?", answer: "Brake later into T3.", tool_calls: null },
    ]);
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream } });

    expect(await screen.findByText(/new chat — ask a question/i)).toBeTruthy();
    expect(screen.queryByText("where do I brake?")).toBeNull();
  });

  it("sends a question, then renders the live coach bubble as the stream fills in", async () => {
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream, sessionId: "s1", lapId: 9 } });

    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "where am I losing time?" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("where am I losing time?")).toBeTruthy();
    expect(stream.ask).toHaveBeenCalledWith("where am I losing time?", {
      sessionId: "s1",
      lapId: 9,
      chatId: undefined,
    });

    stream.phase = "tool";
    stream.toolActivity = { name: "get_lap_analysis", input: { lap_id: 9 } };
    stream.notify();
    expect(await screen.findByText(/reading your lap/i)).toBeTruthy();

    stream.phase = "streaming";
    stream.answer = "Brake later into T3.";
    stream.notify();
    expect(await screen.findByText(/Brake later into T3\./)).toBeTruthy();

    stream.phase = "done";
    stream.chatId = "chat-1";
    stream.notify();
    await waitFor(() => expect(screen.queryByText(/reading your lap/i)).toBeNull());
    expect(screen.getByText(/Brake later into T3\./)).toBeTruthy();
  });

  it("shows the error text when the stream errors", async () => {
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream } });

    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "what happened?" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    stream.phase = "error";
    stream.error = "lost connection to the coach server";
    stream.notify();

    expect(await screen.findByText(/lost connection to the coach server/i)).toBeTruthy();
  });

  it("sends on Enter and inserts a newline on Shift+Enter", async () => {
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream } });

    const textarea = await screen.findByPlaceholderText<HTMLTextAreaElement>(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "one" } });
    await fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(stream.ask).not.toHaveBeenCalled();

    await fireEvent.keyDown(textarea, { key: "Enter" });
    expect(stream.ask).toHaveBeenCalledWith("one", { sessionId: undefined, lapId: undefined, chatId: undefined });
  });

  it("loads the thread named by the chatId prop", async () => {
    vi.mocked(getChatMessages).mockImplementation(async (id: string) =>
      id === "c9"
        ? [{ ts: 1, session_id: "s2", track: "Spa", car: "Ferrari", lap_id: null, question: "spa question", answer: "spa answer", tool_calls: null }]
        : [{ ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "lemans question", answer: "lemans answer", tool_calls: null }],
    );
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream, chatId: "c9" } });

    expect(await screen.findByText("spa question")).toBeTruthy();
    expect(screen.queryByText("lemans question")).toBeNull();
  });

  it("chatId=null (session with no chat) shows an empty new chat, not the latest", async () => {
    // A chatless session was opened (chatId=null), so the panel must NOT load
    // any latest chat — it shows the empty new-chat state.
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "old question", answer: "old answer", tool_calls: null },
    ]);
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream, chatId: null } });

    expect(await screen.findByText(/new chat — ask a question/i)).toBeTruthy();
    expect(screen.queryByText("old question")).toBeNull();
  });

  it("+ New session empties the thread, fires onnewsession, and the next ask starts fresh", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "old question", answer: "old answer", tool_calls: null },
    ]);
    const stream = new FakeStream();
    const onnewsession = vi.fn();

    render(ChatPanel, { props: { stream, chatId: "c1", onnewsession } });
    expect(await screen.findByText("old question")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    await waitFor(() => expect(screen.queryByText("old question")).toBeNull());
    expect(screen.getByText(/new chat — ask a question/i)).toBeTruthy();
    expect(onnewsession).toHaveBeenCalled();

    const textarea = screen.getByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "brand new" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(stream.ask).toHaveBeenCalledWith("brand new", { sessionId: undefined, lapId: undefined, chatId: undefined });
  });

  // --- the usage/cost meter (issue #39) ---

  async function answerWith(stream: FakeStream, meter: {
    usage: ChatUsage; cost: number | null; costComplete?: boolean; longChat?: boolean;
  }, props: Record<string, unknown> = {}) {
    render(ChatPanel, { props: { stream, ...props } });
    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "where am I losing time?" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await answerAgain(stream, meter);
  }

  async function answerAgain(stream: FakeStream, meter: {
    usage: ChatUsage; cost: number | null; costComplete?: boolean; longChat?: boolean;
  }) {
    stream.phase = "done";
    stream.answer = "Brake later into T3.";
    stream.chatId = "chat-1";
    stream.usage = meter.usage;
    stream.cost = meter.cost;
    stream.costComplete = meter.costComplete ?? true;
    stream.longChat = meter.longChat ?? false;
    stream.notify();
  }

  it("shows the token + cost meter after an answer", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200, messages: 1 }), cost: 0.011 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toMatch(/1\.2k tokens/);
    expect(meter.textContent).toMatch(/\$0\.011/);
  });

  it("scopes the meter's tooltip to this chat (#56: excludes setup/notes/memory)", async () => {
    // Model.complete() (setup capture, track-notes drafting, memory
    // distillation) bypasses the instrumented streaming path and is billed to
    // the driver's own key but counted nowhere -- the tooltip must say so
    // rather than let the figure read as total spend.
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200, messages: 1 }), cost: 0.011 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.title).toBe(
      "Tokens billed in this chat, and the estimated API cost of them. Excludes setup capture, track-notes drafting, and memory operations, which are billed separately to your key.",
    );
  });

  it("hides the meter on an empty new chat", async () => {
    const stream = new FakeStream();
    render(ChatPanel, { props: { stream } });
    expect(await screen.findByText(/new chat — ask a question/i)).toBeTruthy();
    expect(screen.queryByTestId("usage-meter")).toBeNull();
  });

  it("shows a dash instead of a cost when the model is unpriced", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: null });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toMatch(/1\.2k tokens/);
    expect(meter.textContent).toContain("—");
    expect(meter.textContent).not.toContain("$");
  });

  it("renders a free model as a dash, never $0.00", async () => {
    // A local/fake model costs no API dollars, but "$0.00" would read as "this
    // paid thing was free". Tokens still show.
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 900 }), cost: 0 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toContain("900 tokens");
    expect(meter.textContent).toContain("—");
    expect(meter.textContent).not.toContain("$0.00");
  });

  it("never rounds a real cost down to $0.00", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 50 }), cost: 0.000002 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toContain("<$0.01");
    expect(meter.textContent).not.toContain("$0.00 ");
  });

  it("shows a >= prefix when the chat total is incomplete", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 5000 }), cost: 0.04, costComplete: false });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toMatch(/≥\s*\$0\.04/);
  });

  it("seeds the meter from the server when an existing thread is opened", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Spa", car: "Ferrari", lap_id: null,
        question: "q", answer: "a", tool_calls: null },
    ]);
    vi.mocked(getChatUsage).mockResolvedValue({
      usage: usage({ total: 12400, messages: 3 }), cost: 0.031, cost_complete: true,
    });
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream, chatId: "c9" } });

    const meter = await screen.findByTestId("usage-meter");
    expect(vi.mocked(getChatUsage)).toHaveBeenCalledWith("c9");
    expect(meter.textContent).toMatch(/12\.4k tokens/);
    expect(meter.textContent).toMatch(/\$0\.031/);
  });

  it("a failed usage fetch leaves the thread usable and the meter hidden", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Spa", car: "Ferrari", lap_id: null,
        question: "spa question", answer: "spa answer", tool_calls: null },
    ]);
    vi.mocked(getChatUsage).mockRejectedValue(new Error("sidecar not up"));
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream, chatId: "c9" } });

    expect(await screen.findByText("spa question")).toBeTruthy();
    expect(screen.queryByTestId("usage-meter")).toBeNull();
  });

  it("with the Settings cost estimate off, shows tokens but no dollar figure", async () => {
    vi.mocked(getSettings).mockResolvedValue({ show_cost: false } as SettingsResponse);
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: 0.011 });

    const meter = await screen.findByTestId("usage-meter");
    await waitFor(() => expect(meter.textContent).not.toContain("$"));
    expect(meter.textContent).toMatch(/1\.2k tokens/);
    expect(meter.textContent).not.toContain("0.011");
  });

  it("a failed settings fetch still shows the cost (the default is on)", async () => {
    vi.mocked(getSettings).mockRejectedValue(new Error("sidecar not up"));
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: 0.011 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toMatch(/\$0\.011/);
  });

  it("zeroes the meter on + New session", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: 0.011 });
    expect(await screen.findByTestId("usage-meter")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /new session/i }));

    await waitFor(() => expect(screen.queryByTestId("usage-meter")).toBeNull());
  });

  // --- the soft long-chat nudge (issue #39, task 7) ---

  it("shows no nudge on a normal-length chat", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: 0.011 });

    expect(await screen.findByTestId("usage-meter")).toBeTruthy();
    expect(screen.queryByTestId("long-chat-nudge")).toBeNull();
  });

  it("nudges toward a New chat once the chat gets long", async () => {
    const stream = new FakeStream();
    await answerWith(stream,
      { usage: usage({ total: 200000 }), cost: 0.9, longChat: true });

    const nudge = await screen.findByTestId("long-chat-nudge");
    expect(nudge.textContent).toMatch(/long chat/i);
    expect(nudge.textContent).toMatch(/new chat/i);
  });

  it("the nudge's action starts a fresh chat, so the next ask sends no chat_id", async () => {
    const stream = new FakeStream();
    await answerWith(stream,
      { usage: usage({ total: 200000 }), cost: 0.9, longChat: true });
    await screen.findByTestId("long-chat-nudge");

    await fireEvent.click(screen.getByRole("button", { name: /start a new chat/i }));

    await waitFor(() => expect(screen.queryByTestId("long-chat-nudge")).toBeNull());
    expect(screen.getByText(/new chat — ask a question/i)).toBeTruthy();
    expect(screen.queryByTestId("usage-meter")).toBeNull();

    const textarea = screen.getByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "fresh start" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(stream.ask).toHaveBeenLastCalledWith("fresh start",
      { sessionId: undefined, lapId: undefined, chatId: undefined });
  });

  it("does not reappear after dismissal within the same thread", async () => {
    const stream = new FakeStream();
    await answerWith(stream,
      { usage: usage({ total: 200000 }), cost: 0.9, longChat: true });
    await screen.findByTestId("long-chat-nudge");

    await fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await waitFor(() => expect(screen.queryByTestId("long-chat-nudge")).toBeNull());

    // a later answer in the SAME thread still reports long_chat -- stay dismissed
    await answerAgain(stream,
      { usage: usage({ total: 260000 }), cost: 1.2, longChat: true });

    await waitFor(() => expect(screen.queryByTestId("usage-meter")).toBeTruthy());
    expect(screen.queryByTestId("long-chat-nudge")).toBeNull();
  });

  // --- voice (#104) ---

  function fakeVoice() {
    const speaker = {
      spoken: [] as string[], cancels: 0,
      speak(text: string) { this.spoken.push(text); return new Promise<void>(() => {}); },
      cancel() { this.cancels++; },
      earcon() { /* no-op */ },
    };
    const recorder = {
      async start() { /* no-op */ },
      async stop() { return { pcm: new Float32Array(16000), sampleRate: 16000 }; },
    };
    const transcriber = { async transcribe() { return { text: "where am I losing time", seconds: 1 }; } };
    const asked: [string, VoiceMode][] = [];
    const voice = new VoiceLoop({
      recorder, transcriber, speaker, ask: (t, m) => asked.push([t, m]),
      vad: { threshold: 0.02, silenceMs: 1000, maxS: 20 }, conversationIdleS: 30,
    });
    return { voice, speaker, asked };
  }

  const rowDefaults = { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, tool_calls: null };

  it("shows a play button on every coach message and toggles it", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { question: "q1", answer: "**A1**", ...rowDefaults },
      { question: "q2", answer: "A2", ...rowDefaults },
    ]);
    const { voice, speaker } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), chatId: "c1", voice } });

    const plays = await screen.findAllByRole("button", { name: "Read aloud" });
    expect(plays).toHaveLength(2);

    await fireEvent.click(plays[0]);
    expect(speaker.spoken).toEqual(["A1"]);
    expect(screen.getAllByRole("button", { name: "Stop" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(speaker.cancels).toBe(1);
  });

  it("hold on the mic = PTT: adds a 🎤 message and asks with voice=ptt", async () => {
    const stream = new FakeStream();
    const { voice, asked } = fakeVoice();
    render(ChatPanel, { props: { stream, voice } });

    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic);
    await new Promise((r) => setTimeout(r, 300));
    await fireEvent.pointerUp(mic);

    await waitFor(() => expect(asked).toEqual([["where am I losing time", "ptt"]]));
    expect(screen.getByText("🎤 where am I losing time")).toBeTruthy();
    expect(stream.lastAsk?.opts.voice).toBe("ptt");
  });

  it("asking the same question twice in a row (PTT) sends both times", async () => {
    const stream = new FakeStream();
    const { voice, asked } = fakeVoice();
    render(ChatPanel, { props: { stream, voice } });
    const mic = screen.getByRole("button", { name: /hold to talk/i });

    await fireEvent.pointerDown(mic);
    await new Promise((r) => setTimeout(r, 300));
    await fireEvent.pointerUp(mic);
    await waitFor(() => expect(asked).toEqual([["where am I losing time", "ptt"]]));
    stream.emit({ phase: "done", answer: "Answer one.", chatId: "c1" });
    await waitFor(() => expect(voice.state).toBe("speaking"));
    voice.stop(); // finish speaking, back to idle

    await fireEvent.pointerDown(mic);
    await new Promise((r) => setTimeout(r, 300));
    await fireEvent.pointerUp(mic);

    await waitFor(() => expect(asked).toEqual([
      ["where am I losing time", "ptt"],
      ["where am I losing time", "ptt"],
    ]));
    expect(stream.ask).toHaveBeenCalledTimes(2);
  });

  it("press shows a readable Listening… status", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });

    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic);

    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Listening…"));
  });

  it("tap on the mic = conversation mode; the button reflects state", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });

    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic);
    await fireEvent.pointerUp(mic);

    await waitFor(() => expect(mic.getAttribute("data-voice-state")).toBe("listening"));
    expect(voice.mode).toBe("conversation");
  });

  it("done with spoken hands the answer to the loop", async () => {
    const stream = new FakeStream();
    const { voice, speaker } = fakeVoice();
    render(ChatPanel, { props: { stream, voice } });

    const mic = screen.getByRole("button", { name: /hold to talk/i });
    await fireEvent.pointerDown(mic);
    await new Promise((r) => setTimeout(r, 300));
    await fireEvent.pointerUp(mic);

    await waitFor(() => expect(voice.state).toBe("asking"));
    stream.emit({ phase: "done", answer: "Full answer.", spoken: "Short cue.", chatId: "c1" });

    await waitFor(() => expect(speaker.spoken).toContain("Short cue."));
    expect(screen.getByText("Full answer.")).toBeTruthy(); // written chat is the full answer
  });

  it("composer: voice enabled + empty box shows the mic, no Send button", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });

    expect(await screen.findByRole("button", { name: /hold to talk/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });

  it("composer: typing swaps the mic for Send; clearing brings the mic back", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });
    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);

    await fireEvent.input(textarea, { target: { value: "hello" } });
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /hold to talk/i })).toBeNull();

    await fireEvent.input(textarea, { target: { value: "" } });
    expect(await screen.findByRole("button", { name: /hold to talk/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });

  it("composer: voice disabled always shows Send, never the mic", async () => {
    render(ChatPanel, { props: { stream: new FakeStream(), voice: null } });
    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);

    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /hold to talk/i })).toBeNull();

    await fireEvent.input(textarea, { target: { value: "hello" } });
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /hold to talk/i })).toBeNull();
  });

  it("composer: mid-utterance the state button stays even once text is typed", async () => {
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });
    const mic = screen.getByRole("button", { name: /hold to talk/i });
    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);

    await fireEvent.pointerDown(mic);
    await waitFor(() => expect(mic.getAttribute("data-voice-state")).toBe("listening"));

    await fireEvent.input(textarea, { target: { value: "typed mid utterance" } });

    expect(mic.getAttribute("data-voice-state")).toBe("listening");
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });

  it("keyboard PTT key presses and releases the loop, but not while typing", async () => {
    vi.mocked(getSettings).mockResolvedValue({
      show_cost: true,
      voice: { enabled: true, ptt_key: "F9" },
    } as unknown as SettingsResponse);
    const { voice } = fakeVoice();
    render(ChatPanel, { props: { stream: new FakeStream(), voice } });
    await waitFor(() => expect(getSettings).toHaveBeenCalled());

    await fireEvent.keyDown(window, { code: "F9" });
    await waitFor(() => expect(voice.state).toBe("listening"));
    await fireEvent.keyUp(window, { code: "F9" });
    await waitFor(() => expect(voice.state).not.toBe("listening"));

    const ta = screen.getByLabelText("Ask your engineer");
    await fireEvent.keyDown(ta, { code: "F9" });
    expect(voice.state).not.toBe("listening");
  });

  it("switching voice.mic_device_id disposes the previous MicRecorder", async () => {
    Object.defineProperty(navigator, "mediaDevices", { value: {}, configurable: true });
    const voiceSettings = (mic_device_id: string) => ({
      enabled: true, ptt_key: "F9", ptt_wheel: null, ptt_gamepad: null, tts_voice: null, tts_rate: 1,
      vad_threshold: 0.02, vad_silence_ms: 1000, vad_max_s: 20, conversation_idle_s: 30, mic_device_id,
    });
    vi.mocked(getSettings).mockResolvedValue({
      show_cost: true, voice: voiceSettings("device-1"),
    } as unknown as SettingsResponse);
    render(ChatPanel, { props: { stream: new FakeStream() } });
    await waitFor(() => expect(micInstances.length).toBe(1));
    expect(micInstances[0].deviceId).toBe("device-1");

    vi.mocked(getSettings).mockResolvedValue({
      show_cost: true, voice: voiceSettings("device-2"),
    } as unknown as SettingsResponse);
    window.dispatchEvent(new Event("settings-changed"));

    await waitFor(() => expect(micInstances.length).toBe(2));
    expect(micInstances[0].dispose).toHaveBeenCalled();
    expect(micInstances[1].deviceId).toBe("device-2");

    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  });
});

// #104: opening a past chat should land at the end, with a jump-to-latest
// button while scrolled up (jsdom has no layout, so scrollHeight/clientHeight
// are stubbed on the scroll element).
describe("ChatPanel (scroll to end + jump-to-latest, #104)", () => {
  beforeEach(() => {
    vi.mocked(getChatUsage).mockReset();
    vi.mocked(getChatUsage).mockResolvedValue({ usage: usage(), cost: 0, cost_complete: true });
    vi.mocked(getSettings).mockReset();
    vi.mocked(getSettings).mockResolvedValue({ show_cost: true } as SettingsResponse);
  });
  afterEach(cleanup);

  function stubLayout(el: HTMLElement, { scrollHeight = 1000, clientHeight = 300 } = {}) {
    Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
    if (!("scrollTop" in el) || typeof el.scrollTop !== "number") {
      Object.defineProperty(el, "scrollTop", { value: 0, writable: true, configurable: true });
    }
  }

  it("opens a stored chat scrolled to the end", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "q1", answer: "a1", tool_calls: null },
      { ts: 2, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "q2", answer: "a2", tool_calls: null },
      { ts: 3, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "q3", answer: "a3", tool_calls: null },
    ]);
    const { container } = render(ChatPanel, { props: { stream: new FakeStream(), chatId: "c1" } });
    const scrollEl = container.querySelector(".chat-scroll") as HTMLElement;
    stubLayout(scrollEl);
    await screen.findByText("q3");
    await waitFor(() => expect(scrollEl.scrollTop).toBe(scrollEl.scrollHeight));
  });

  it("shows the jump button only while scrolled up, and clicking it returns to bottom", async () => {
    vi.mocked(getChatMessages).mockResolvedValue([
      { ts: 1, session_id: "s1", track: "Le Mans", car: "Corvette", lap_id: null, question: "q1", answer: "a1", tool_calls: null },
    ]);
    const { container } = render(ChatPanel, { props: { stream: new FakeStream(), chatId: "c1" } });
    const scrollEl = container.querySelector(".chat-scroll") as HTMLElement;
    stubLayout(scrollEl);
    await screen.findByText("q1");
    await waitFor(() => expect(scrollEl.scrollTop).toBe(scrollEl.scrollHeight));

    expect(container.querySelector(".jump")).toBeNull();

    scrollEl.scrollTop = 0; // far from the bottom (scrollHeight 1000, clientHeight 300)
    await fireEvent.scroll(scrollEl);
    const jumpBtn = await screen.findByRole("button", { name: /jump to latest/i });

    await fireEvent.click(jumpBtn);
    expect(scrollEl.scrollTop).toBe(scrollEl.scrollHeight);
    expect(container.querySelector(".jump")).toBeNull();
  });
});

// The chat bar doubles as the Inputs panel's drag handle (#95 round 2): App owns
// the height/drag state and hands ChatPanel three callbacks + a dragging flag.
describe("ChatPanel (chat bar as the inputs-pane grab handle)", () => {
  afterEach(cleanup);

  function fakeResize(dragging = false) {
    return { down: vi.fn(), move: vi.fn(), up: vi.fn(), dragging };
  }

  it("wires the resize prop's down/move/up onto the chat bar's pointer events", async () => {
    const resize = fakeResize();
    const { container } = render(ChatPanel, { props: { stream: new FakeStream(), resize } });
    const bar = container.querySelector(".chat-bar") as HTMLElement;

    await fireEvent.pointerDown(bar, { clientY: 100 });
    expect(resize.down).toHaveBeenCalledTimes(1);
    expect(resize.down.mock.calls[0][0].clientY).toBe(100);

    await fireEvent.pointerMove(bar, { clientY: 160 });
    expect(resize.move).toHaveBeenCalledTimes(1);
    expect(resize.move.mock.calls[0][0].clientY).toBe(160);

    await fireEvent.pointerUp(bar);
    expect(resize.up).toHaveBeenCalledTimes(1);
  });

  it("reflects resize.dragging as a class on the chat bar", async () => {
    const { container, rerender } = render(ChatPanel, {
      props: { stream: new FakeStream(), resize: fakeResize(false) },
    });
    const bar = container.querySelector(".chat-bar") as HTMLElement;
    expect(bar.className).not.toContain("dragging");

    await rerender({ stream: new FakeStream(), resize: fakeResize(true) });
    expect((container.querySelector(".chat-bar") as HTMLElement).className).toContain("dragging");
  });

  it("does nothing extra with no resize prop (unchanged bar, no pointer handlers wired)", () => {
    const { container } = render(ChatPanel, { props: { stream: new FakeStream() } });
    const bar = container.querySelector(".chat-bar") as HTMLElement;
    expect(bar.className).not.toContain("dragging");
    expect(bar.className).not.toContain("resizable");
  });

  it("a click on New session inside the bar still works (no pointer movement)", async () => {
    const onnewsession = vi.fn();
    const resize = fakeResize();
    const { container } = render(ChatPanel, {
      props: { stream: new FakeStream(), resize, onnewsession },
    });
    const bar = container.querySelector(".chat-bar") as HTMLElement;

    await fireEvent.pointerDown(bar, { clientY: 100 });
    await fireEvent.pointerUp(bar);
    await fireEvent.click(screen.getByRole("button", { name: /new session/i }));

    expect(onnewsession).toHaveBeenCalledTimes(1);
  });
});
