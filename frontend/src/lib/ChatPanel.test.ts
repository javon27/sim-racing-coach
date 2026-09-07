import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/svelte";
import { getChatMessages } from "./api";
import type { CoachPhase, ToolActivity } from "./coach";
import ChatPanel from "./ChatPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getChatMessages: vi.fn() };
});

// Drives the panel the same way CoachStream would, without a real WebSocket.
class FakeStream {
  phase: CoachPhase = "idle";
  toolActivity: ToolActivity | null = null;
  answer = "";
  chatId: string | null = null;
  error: string | null = null;
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
});
