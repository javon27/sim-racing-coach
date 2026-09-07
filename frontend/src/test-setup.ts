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
