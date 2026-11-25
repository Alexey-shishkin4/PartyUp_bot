"use strict";

let startupPlayback;

function waitForStartup() {
  if (!startupPlayback) startupPlayback = playStartup();
  return startupPlayback;
}

async function playStartup() {
  const artwork = document.getElementById("startup-animation");
  const duration = Number(artwork.dataset.durationMs);
  const started = performance.now();
  let remaining = duration;

  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    let deadline;
    try {
      await Promise.race([
        artwork.querySelector("img").decode(),
        new Promise((_, reject) => { deadline = setTimeout(reject, 6000); })
      ]);
    } catch {
      artwork.hidden = true;
      remaining = Math.max(0, duration - (performance.now() - started));
    } finally {
      clearTimeout(deadline);
    }
  }

  // Show at least one full loop. The animation keeps playing while data is pending.
  await new Promise(resolve => setTimeout(resolve, remaining));
}
