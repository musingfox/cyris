// The blind-label deck: one sampled article at a time, showing nothing of what the
// pipeline decided, answered up, down or skip. The server holds the order and the
// progress, so every answer comes back with the next item.
const $ = (id) => document.getElementById(id);
const card = $("l-card");
const buttons = [...document.querySelectorAll("#l-actions button")];
const notice = $("l-error");
let current = null;
// One card, one answer: set before the first await, so a second press or a drag
// while the answer is in flight finds the deck taken.
let busy = false;
// The button that held focus when its answer began, so a run of Enter presses keeps
// answering card after card.
let answerFocus = null;

const FAILED = {
  signedOut: "Your session has ended, so the answer was not taken. Reload the page and sign in again.",
  refused: (status, reason) => `The answer was not taken (HTTP ${status})${reason ? `: ${reason}` : "."}`,
  unanswered: "No answer from the server, so the answer may not have been taken. Check your connection, then try again.",
};

function show(deck) {
  current = deck.item;
  const remaining = deck.total - deck.answered;
  $("l-progress").textContent = deck.total
    ? `${deck.answered} of ${deck.total} answered · ${remaining} remaining`
    : "No sample";
  for (const id of ["l-card", "l-actions", "l-hint"]) $(id).hidden = !current;
  $("l-empty").hidden = Boolean(current);
  $("l-empty").textContent = deck.total
    ? "Every item is answered. Run cyris labels report for the result."
    : "No sample is drawn yet. Run cyris labels draw first.";
  if (!current) return;
  $("l-source").textContent = current.source;
  $("l-title").textContent = current.title;
  $("l-excerpt").textContent = current.excerpt;
  $("l-excerpt").hidden = !current.excerpt;
}

async function failureOf(resp) {
  if (resp.status === 401) return FAILED.signedOut;
  const body = await resp.json().catch(() => ({}));
  return FAILED.refused(resp.status, body.error);
}

const enable = (on) => buttons.forEach((b) => { b.disabled = !on; });
const lean = (dir) => {
  card.classList.toggle("lean-up", dir === "up");
  card.classList.toggle("lean-down", dir === "down");
};

async function load() {
  try {
    const resp = await fetch("/api/labels");
    if (!resp.ok) throw new Error(await failureOf(resp));
    show(await resp.json());
  } catch (error) {
    $("l-progress").textContent = "Not loaded";
    // fetch rejects with a TypeError only when no answer came back at all.
    notice.textContent = error instanceof TypeError ? FAILED.unanswered : error.message;
    notice.hidden = false;
  }
}

// Answer first, then fly: the card leaves only once its answer has landed.
async function answer(label) {
  if (busy || !current) return;
  busy = true;
  answerFocus = buttons.find((b) => b === document.activeElement) || null;
  enable(false);
  notice.hidden = true;
  let failure = "";
  let next = null;
  try {
    const resp = await fetch("/api/labels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: current.url, label }),
    });
    if (resp.ok) next = await resp.json();
    else failure = await failureOf(resp);
  } catch {
    failure = FAILED.unanswered;
  }
  if (!failure) return fly(label, next);
  // The answer did not land, so the card stays where it is and says why.
  card.style.transform = "";
  lean(null);
  busy = false;
  enable(true);
  notice.textContent = failure;
  notice.hidden = false;
  refocus();
}

// With no card left the buttons are hidden, and there is nothing left to focus.
function refocus() {
  if (answerFocus && !card.hidden) answerFocus.focus();
  answerFocus = null;
}

let pending = null;
function fly(label, next) {
  pending = next;
  // A skip has no direction, and reduced motion stops every transition, so no
  // transitionend would ever come.
  if (label === "skip" || matchMedia("(prefers-reduced-motion: reduce)").matches) return settle();
  card.dataset.flying = label;
  card.classList.remove("dragging");
  // A card dealt in this same frame has no style yet to move from; without one no
  // transition runs, and no transitionend would ever deal the next card.
  void card.offsetWidth;
  lean(label);
  card.style.transform = `translateX(${label === "up" ? 120 : -120}vw) rotate(${label === "up" ? 12 : -12}deg)`;
}
// Back to rest without animating the way back, then deal the next card.
function settle() {
  delete card.dataset.flying;
  card.classList.add("dragging");
  card.style.transform = "";
  lean(null);
  void card.offsetWidth;
  card.classList.remove("dragging");
  busy = false;
  enable(true);
  show(pending);
  refocus();
}
card.addEventListener("transitionend", (e) => {
  if (e.propertyName === "transform" && card.dataset.flying) settle();
});

// The card follows the pointer, leans once it points somewhere, and released far
// enough to one side answers that way; released where it was pressed, it opens the
// article. The distances are raw's triage card's.
const SWIPE_PX = 120, LEAN_PX = 40, TAP_PX = 5;
const dirOf = (dx, min) => dx > min ? "up" : dx < -min ? "down" : null;
let drag = null;
card.addEventListener("pointerdown", (e) => {
  if (busy || e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, dx: 0, dy: 0 };
  card.setPointerCapture(e.pointerId);
  card.classList.add("dragging");
});
card.addEventListener("pointermove", (e) => {
  if (!drag) return;
  drag.dx = e.clientX - drag.x;
  drag.dy = e.clientY - drag.y;
  lean(dirOf(drag.dx, LEAN_PX));
  card.style.transform = `translateX(${drag.dx}px) rotate(${drag.dx / 20}deg)`;
});
function endDrag(e) {
  if (!drag) return;
  const { dx, dy } = drag;
  drag = null;
  card.classList.remove("dragging");
  const swipe = e.type === "pointerup" && dirOf(dx, SWIPE_PX);
  if (swipe) return answer(swipe);
  card.style.transform = "";
  lean(null);
  if (e.type === "pointerup" && Math.abs(dx) < TAP_PX && Math.abs(dy) < TAP_PX) openCard();
}
function openCard() {
  if (!busy && current) window.open(current.url, "_blank", "noopener");
}
card.addEventListener("pointerup", endDrag);
card.addEventListener("pointercancel", endDrag);
card.addEventListener("keydown", (e) => {
  if (e.key === "Enter") openCard();
});
for (const b of buttons) {
  b.addEventListener("mouseenter", () => lean(b.dataset.label));
  b.addEventListener("mouseleave", () => card.dataset.flying || lean(null));
  b.addEventListener("click", () => answer(b.dataset.label));
}

load();
