"use strict";

function bindProfileSwipes(card, canReact, reactToCard) {
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  let gesture;
  let exiting = false;

  function reset() {
    gesture = undefined;
    delete card.dataset.swipe;
    card.classList.remove("dragging");
    card.style.removeProperty("--drag-x");
    card.style.removeProperty("--drag-rotation");
  }

  card.addEventListener("pointerdown", event => {
    if (exiting || !canReact() || gesture || !event.isPrimary || event.button !== 0 ||
        event.target.closest("button, a, input, textarea, select")) return;
    gesture = { id: event.pointerId, x: event.clientX, y: event.clientY, horizontal: false };
  });

  card.addEventListener("pointermove", event => {
    if (gesture?.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (!gesture.horizontal) {
      if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) { reset(); return; }
      if (Math.abs(dx) < 10) return;
      gesture.horizontal = true;
      card.setPointerCapture(event.pointerId);
      card.classList.add("dragging");
    }
    card.dataset.swipe = dx > 0 ? "like" : "skip";
    card.style.setProperty("--drag-x", dx + "px");
    card.style.setProperty("--drag-rotation", Math.max(-13, Math.min(13, dx / 16)) + "deg");
  });

  card.addEventListener("pointerup", async event => {
    if (gesture?.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x;
    const threshold = Math.min(92, card.clientWidth * 0.23);
    const commit = gesture.horizontal && Math.abs(dx) >= threshold && canReact();
    const direction = Math.sign(dx);
    const transform = getComputedStyle(card).transform;
    reset();
    if (!commit) return;

    exiting = true;
    card.dataset.swipe = direction > 0 ? "like" : "skip";
    const animation = !reducedMotion.matches && card.animate?.([
      { transform, opacity: 1 },
      { transform: `translateX(${direction * 110}vw) rotate(${direction * 16}deg)`, opacity: 0 }
    ], { duration: 180, easing: "ease-out", fill: "forwards" });
    try {
      await reactToCard(card.dataset.swipe, animation?.finished);
    } finally {
      if (animation) animation.cancel();
      reset();
      exiting = false;
    }
  });

  for (const type of ["pointercancel", "lostpointercapture", "pointerleave"]) {
    card.addEventListener(type, event => {
      if (gesture?.id !== event.pointerId) return;
      // A touch starts with implicit capture on the child under the finger.
      if (type === "lostpointercapture" && event.target !== card) return;
      if (type !== "pointerleave" || !gesture.horizontal) reset();
    });
  }
}
