import { afterEach } from "vitest";
import { eventFeed } from "./lib/events";

// The /events feed is an app-lifetime singleton (#106), so a frame pushed by one
// test would still be the "current value" replayed to the next test's subscribers.
// Clear it between cases so every test starts from "nothing has arrived yet".
afterEach(() => eventFeed.reset());

// jsdom DOES have WebSocket, so App's boot (liveField.start + eventFeed.start)
// would dial a real ws://127.0.0.1:8765 from every test that renders the shell.
// Replace it with an inert socket; tests that exercise a feed stub their own via
// vi.stubGlobal (which unstubs back to this one).
class InertWebSocket {
  onopen: unknown = null;
  onmessage: unknown = null;
  onerror: unknown = null;
  onclose: unknown = null;
  constructor(public url: string) {}
  close() {}
  send() {}
}
globalThis.WebSocket = InertWebSocket as unknown as typeof WebSocket;

// jsdom lacks the Web Animations API, which Svelte's transitions (e.g. the
// splash's out:fade) call via element.animate(). Stub it so components that
// animate on mount/unmount don't crash under test.
if (typeof Element !== "undefined" && !Element.prototype.animate) {
  Element.prototype.animate = function () {
    return {
      cancel() {},
      finish() {},
      play() {},
      pause() {},
      onfinish: null,
      oncancel: null,
    } as unknown as Animation;
  };
}

// jsdom lacks matchMedia, which svelte/motion's prefersReducedMotion reads at
// import time (before any test's beforeAll runs). Stub it here.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
