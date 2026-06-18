/**
 * modules/showroom.js — Showroom shell interactions.
 *
 * Purely cosmetic / discovery layer. Deliberately decoupled from the voice
 * pipeline (app.js): it never imports from app.js and only reads/writes its own
 * DOM (intro overlay, tractor pedestals, the idle prompt). The mic button stays
 * the single source of truth for starting a session.
 */

// ── Intro overlay ───────────────────────────────────────────────────────────────
const intro    = document.getElementById('intro');
const enterBtn = document.getElementById('enterBtn');

function enterShowroom() {
    if (!intro) return;
    intro.classList.add('hidden');
    // Nudge the visitor toward the start control once they're inside.
    const hint = document.getElementById('micHint');
    if (hint) hint.textContent = 'Tap the mic to start';
}

if (enterBtn) enterBtn.addEventListener('click', enterShowroom);

// ── Tractor pedestals (product discovery) ────────────────────────────────────────
// Tapping a model highlights it and previews its specs in the idle prompt area,
// suggesting a question the visitor can then ask Laxmi out loud.
const peds       = document.querySelectorAll('.ped');
const screenIdle = document.getElementById('screenIdle');
const screenText = document.getElementById('screenText');

function selectPed(ped) {
    // Just highlight the tapped model — the card itself is the affordance, so we
    // don't write any text over the scene (keeps the showroom clean).
    peds.forEach((p) => p.classList.toggle('selected', p === ped));
}

peds.forEach((ped) => {
    ped.addEventListener('click', () => selectPed(ped));
});
