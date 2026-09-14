import { describe, it, expect, vi, beforeEach } from "vitest";
import { CoachStream } from "./coach";

// Fake WebSocket the test drives synchronously — no real network, no timers.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    this.onclose?.();
  }
}

function emit(ws: FakeWebSocket, payload: unknown) {
  ws.onmessage?.({ data: JSON.stringify(payload) });
}

describe("CoachStream", () => {
  beforeEach(() => {
    FakeWebSocket.instances.length = 0;
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("opens ws://127.0.0.1:8765/coach and sends the question payload on open", () => {
    const stream = new CoachStream();
    stream.ask("where do I brake?", { sessionId: "s1", lapId: 9 });

    expect(stream.phase).toBe("connecting");
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toBe("ws://127.0.0.1:8765/coach");

    ws.onopen?.();
    expect(JSON.parse(ws.sent[0])).toEqual({
      question: "where do I brake?",
      session_id: "s1",
      lap_id: 9,
    });
  });

  it("accumulates text deltas and ends done, closing the socket", () => {
    const stream = new CoachStream();
    stream.ask("how's my braking?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "tool", name: "get_lap_analysis", input: { lap_id: 9 } });
    expect(stream.phase).toBe("tool");
    expect(stream.toolActivity).toEqual({ name: "get_lap_analysis", input: { lap_id: 9 } });

    emit(ws, { type: "text", text: "Brake later " });
    emit(ws, { type: "text", text: "into T3." });
    expect(stream.phase).toBe("streaming");
    expect(stream.answer).toBe("Brake later into T3.");

    emit(ws, { type: "done", chat_id: "chat-1" });
    expect(stream.phase).toBe("done");
    expect(stream.chatId).toBe("chat-1");
    expect(ws.closed).toBe(true);
  });

  it("sets phase=error and closes the socket on an error event", () => {
    const stream = new CoachStream();
    stream.ask("what happened?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "error", message: "coach error" });
    expect(stream.phase).toBe("error");
    expect(stream.error).toBe("coach error");
    expect(ws.closed).toBe(true);
  });

  it("sets phase=error when the socket errors before done", () => {
    const stream = new CoachStream();
    stream.ask("where do I brake?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    ws.onerror?.();

    expect(stream.phase).toBe("error");
    expect(stream.error).toBe("lost connection to the coach server");
  });

  it("sets phase=error on a malformed (non-JSON) message instead of hanging", () => {
    const stream = new CoachStream();
    stream.ask("where do I brake?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    ws.onmessage?.({ data: "not json" });

    expect(stream.phase).toBe("error");
    expect(stream.error).toBe("received a malformed message from the coach server");
    expect(ws.closed).toBe(true);
  });

  it("sets phase=error when the socket closes before done/error", () => {
    const stream = new CoachStream();
    stream.ask("where do I brake?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    ws.onclose?.();

    expect(stream.phase).toBe("error");
    expect(stream.error).toBe("lost connection to the coach server");
  });

  it("does not override phase/error when the socket closes after done", () => {
    const stream = new CoachStream();
    stream.ask("how's my braking?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "done", chat_id: "chat-1" });
    ws.onclose?.();

    expect(stream.phase).toBe("done");
    expect(stream.error).toBeNull();
  });

  it("notifies subscribers when the connection is lost", () => {
    const stream = new CoachStream();
    const seen: string[] = [];
    const unsubscribe = stream.subscribe((s) => seen.push(s.phase));

    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    ws.onerror?.();

    expect(seen).toEqual(["idle", "connecting", "error"]);
    unsubscribe();
  });

  it("closes the previous socket when a new ask starts before the first finishes", () => {
    const stream = new CoachStream();
    stream.ask("first question");
    const first = FakeWebSocket.instances[0];
    first.onopen?.();

    stream.ask("second question");
    expect(first.closed).toBe(true);
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("captures usage and cost from the done frame", () => {
    const stream = new CoachStream();
    stream.ask("how's my braking?");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, {
      type: "done",
      chat_id: "chat-1",
      usage: { input: 3500, output: 400, cache_read: 0, cache_write: 0, total: 3900, messages: 2 },
      cost: 0.011,
      cost_complete: true,
    });

    expect(stream.usage?.total).toBe(3900);
    expect(stream.cost).toBe(0.011);
    expect(stream.costComplete).toBe(true);
  });

  it("treats a done frame with no usage as no meter data, not a crash", () => {
    const stream = new CoachStream();
    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "done", chat_id: "chat-1" });

    expect(stream.phase).toBe("done");
    expect(stream.usage).toBeNull();
    expect(stream.cost).toBeNull();
  });

  it("captures the long_chat nudge flag from the done frame", () => {
    const stream = new CoachStream();
    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "done", chat_id: "c1", usage: { total: 200000 }, cost: 1, cost_complete: true, long_chat: true });

    expect(stream.longChat).toBe(true);
  });

  it("defaults long_chat to false when the frame omits it", () => {
    const stream = new CoachStream();
    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    emit(ws, { type: "done", chat_id: "c1" });

    expect(stream.longChat).toBe(false);
  });

  it("clears usage when a new ask starts", () => {
    const stream = new CoachStream();
    stream.ask("q1");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    emit(ws, { type: "done", chat_id: "c1", usage: { total: 10 }, cost: 1, cost_complete: true });
    expect(stream.usage).not.toBeNull();

    stream.ask("q2");

    expect(stream.usage).toBeNull();
    expect(stream.cost).toBeNull();
    expect(stream.costComplete).toBe(true);
  });

  it("ignores an unknown event type without breaking the stream", () => {
    const stream = new CoachStream();
    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();

    // the server filters these out, but an older/newer sidecar may send them
    emit(ws, { type: "usage", usage: { input: 1 }, model: "claude-sonnet-5" });
    emit(ws, { type: "text", text: "still fine" });

    expect(stream.phase).toBe("streaming");
    expect(stream.answer).toBe("still fine");
  });

  it("notifies subscribers on state changes (store contract)", () => {
    const stream = new CoachStream();
    const seen: string[] = [];
    const unsubscribe = stream.subscribe((s) => seen.push(s.phase));

    stream.ask("q");
    const ws = FakeWebSocket.instances[0];
    ws.onopen?.();
    emit(ws, { type: "done", chat_id: "c1" });

    expect(seen).toEqual(["idle", "connecting", "done"]);
    unsubscribe();
  });
});
