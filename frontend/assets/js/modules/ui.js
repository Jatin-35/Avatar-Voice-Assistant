/**
 * modules/ui.js
 * Single source of truth for all DOM reads and writes.
 * Nothing outside this module touches the DOM directly.
 */

const LABELS = {
    idle: 'Ready',
    connecting: 'Connecting',
    listening: 'Listening',
    searching_kb: 'Searching',
    processing: 'Thinking',
    speaking: 'Speaking',
    error: 'Error',
};

const $ = (id) => document.getElementById(id);

// ── DOM refs ──────────────────────────────────────────────────────────────────
const statePill     = $('statePill');
const stateLabel    = $('stateLabel');
const avatarStill   = $('avatarStill');    // still pose — hidden while bot speaks
const avatarTalking = $('avatarTalking'); // animated GIF — opacity toggled, NO filter
const avatarGlow    = $('avatarGlow');
const screenIdle = $('screenIdle');
const screenText = $('screenText');
const userTranscript = $('userTranscript');
const transcriptText = $('transcriptText');
const micHint = $('micHint');

export const micBtn = $('micBtn');
const micIcon = micBtn.querySelector('.mic-icon');

function setHint(text) { if (micHint) micHint.textContent = text; }

// Exactly one of active/speaking/armed (or none) on the mic button.
function micClass(name) {
    micBtn.classList.remove('active', 'speaking', 'armed');
    if (name) micBtn.classList.add(name);
}

// ── State pill ────────────────────────────────────────────────────────────────
export function setState(state) {
    statePill.dataset.state = state;
    stateLabel.textContent = LABELS[state] ?? state;
}

// ── Avatar GIF ──────────────────────────────────────────────────────────────────
// Two stacked images: still (always visible when idle) + talking (opacity toggled).
// The talking GIF has ZERO CSS filter to prevent Chrome GPU compositor white-frame
// flicker (the intermediate buffer re-initialises to white on every 40ms GIF frame
// when a drop-shadow filter is present). Glow is provided by #avatarGlow div.
// When speaking, the still image is hidden so it can't show through transparent areas.
let _avatarSpeaking = null;
export function setAvatarSpeaking(on) {
    if (on === _avatarSpeaking) return;
    _avatarSpeaking = on;
    avatarStill.style.opacity   = on ? '0' : '1';  // hide still while talking
    avatarTalking.classList.toggle('active', on);
    avatarGlow.classList.toggle('active', on);
}

// ── Mic button modes + hint ────────────────────────────────────────────────────
export function setMicActive(on) {
    if (on) {
        micClass('active');
        micIcon.className = 'mic-icon bi bi-stop-fill';
        setHint('Listening — just speak, or tap to stop');
    } else {
        micClass(null);
        micIcon.className = 'mic-icon bi bi-mic-fill';
        setHint('Press Start');
    }
}

export function setMicSpeaking(on) {
    if (on) {
        micClass('speaking');
        micIcon.className = 'mic-icon bi bi-soundwave';
        setHint('Tap to talk over Sakshi');
    } else {
        micClass('active');
        micIcon.className = 'mic-icon bi bi-stop-fill';
        setHint('Listening — just speak, or tap to stop');
    }
}

export function setMicArmed(on) {
    if (on) {
        micClass('armed');
        micIcon.className = 'mic-icon bi bi-mic-fill';
        setHint('Listening — go ahead');
    }
    // off: the caller sets the next mode (setMicSpeaking)
}

// ── Answer card (bot response) ──────────────────────────────────────────────────
export function appendScreenText(token) {
    screenIdle.style.display = 'none';
    screenText.classList.add('visible', 'streaming');
    screenText.textContent += token;
    // keep the latest text in view
    const scroll = screenText.parentElement;
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

export function finaliseScreenText() {
    screenText.classList.remove('streaming');
}

export function clearScreenText() {
    screenText.textContent = '';
    screenText.classList.remove('visible', 'streaming');
    screenIdle.style.display = 'block';
}

// ── Caption (live user transcript) ──────────────────────────────────────────────
export function setTranscript(text) {
    transcriptText.textContent = text;
    userTranscript.classList.toggle('visible', text.length > 0);
}

export function clearTranscript() {
    transcriptText.textContent = '';
    userTranscript.classList.remove('visible');
}

// ── Session reset ───────────────────────────────────────────────────────────────
export function resetUI() {
    clearScreenText();
    clearTranscript();
}
