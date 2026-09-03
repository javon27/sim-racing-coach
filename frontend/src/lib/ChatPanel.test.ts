import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/svelte";
import { getChats, getChatMessages } from "./api";
import type { CoachPhase, ToolActivity } from "./coach";
import ChatPanel from "./ChatPanel.svelte";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, getChats: vi.fn(), getChatMessages: vi.fn() };
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
    vi.mocked(getChats).mockReset();
    vi.mocked(getChatMessages).mockReset();
    vi.mocked(getChats).mockResolvedValue([]);
  });

  afterEach(cleanup);

  it("loads and renders the most recent chat's history", async () => {
    vi.mocked(getChats).mockResolvedValue([
      { chat_id: "c1", started: 1, last: 2, message_count: 1, tracks: "Le Mans" },
    ]);
    vi.mocked(getChatMessages).mockResolvedValue([
      {
        ts: 1,
        session_id: "s1",
        track: "Le Mans",
        car: "Corvette",
        lap_id: 9,
        question: "where do I brake?",
        answer: "Brake later into T3.",
        tool_calls: null,
      },
    ]);
    const stream = new FakeStream();

    render(ChatPanel, { props: { stream } });

    expect(await screen.findByText("where do I brake?")).toBeTruthy();
    expect(await screen.findByText(/Brake later into T3\./)).toBeTruthy();
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
    expect(stream.ask).toHaveBeenCalledWith("one", { sessionId: undefined, lapId: undefined });
  });
});
