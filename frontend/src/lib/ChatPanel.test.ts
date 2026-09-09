import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/svelte";
import { getChatMessages, getChatUsage, type ChatUsage } from "./api";
import type { CoachPhase, ToolActivity } from "./coach";
import ChatPanel from "./ChatPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getChatMessages: vi.fn(), getChatUsage: vi.fn() };
});

function usage(overrides: Partial<ChatUsage> = {}): ChatUsage {
  return { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0, messages: 0, ...overrides };
}

// Drives the panel the same way CoachStream would, without a real WebSocket.
class FakeStream {
  phase: CoachPhase = "idle";
  toolActivity: ToolActivity | null = null;
  answer = "";
  chatId: string | null = null;
  error: string | null = null;
  usage: ChatUsage | null = null;
  cost: number | null = null;
  costComplete = true;
  ask = vi.fn();
  #listeners = new Set<(value: this) => void>();

  subscribe(run: (value: this) => void): () => void {
    this.#listeners.add(run);
    run(this);
    return () => this.#listeners.delete(run);
  }

  notify(): void {
    for (const listener of this.#listeners) listener(this);
  }
}

describe("ChatPanel", () => {
  beforeEach(() => {
    vi.mocked(getChatMessages).mockReset();
    vi.mocked(getChatUsage).mockReset();
    vi.mocked(getChatUsage).mockResolvedValue({ usage: usage(), cost: 0, cost_complete: true });
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
    usage: ChatUsage; cost: number | null; costComplete?: boolean;
  }) {
    render(ChatPanel, { props: { stream } });
    const textarea = await screen.findByPlaceholderText(/ask your engineer/i);
    await fireEvent.input(textarea, { target: { value: "where am I losing time?" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    stream.phase = "done";
    stream.answer = "Brake later into T3.";
    stream.chatId = "chat-1";
    stream.usage = meter.usage;
    stream.cost = meter.cost;
    stream.costComplete = meter.costComplete ?? true;
    stream.notify();
  }

  it("shows the token + cost meter after an answer", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200, messages: 1 }), cost: 0.011 });

    const meter = await screen.findByTestId("usage-meter");
    expect(meter.textContent).toMatch(/1\.2k tokens/);
    expect(meter.textContent).toMatch(/\$0\.011/);
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

  it("zeroes the meter on + New session", async () => {
    const stream = new FakeStream();
    await answerWith(stream, { usage: usage({ total: 1200 }), cost: 0.011 });
    expect(await screen.findByTestId("usage-meter")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /new session/i }));

    await waitFor(() => expect(screen.queryByTestId("usage-meter")).toBeNull());
  });
});
