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
