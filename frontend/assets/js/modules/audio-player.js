/**
 * modules/audio-player.js
 * Sequential MP3 playback queue.
 *
 * The backend sends audio as base64-encoded MP3 sentences. We decode each one
 * into a Blob URL, queue it, and play them in order through a hidden <audio>
 * element. Blob URLs are revoked after playback to avoid memory leaks.
 *
 * Avatar speaking state is controlled at the response level in app.js (debounced),
 * NOT per sentence here — otherwise the avatar flickers in the gaps between sentences.
 */

const player  = document.getElementById('audioPlayer');
const queue   = [];
let   playing = false;
let   onIdleCb = null;
let   onFirstPlayingCb = null;
let   announced = false;   // has the avatar already been told "audio is audible" this utterance?

/** Register a callback fired when the queue drains naturally (bot finished). */
export function setOnIdle(fn) { onIdleCb = fn; }

/** Register a callback fired the moment audio is ACTUALLY audible (not when
 *  enqueued) — ground truth for starting the avatar's talking animation. */
export function setOnFirstPlaying(fn) { onFirstPlayingCb = fn; }

// ── Public API ────────────────────────────────────────────────────────────────

/** Decode a base64 MP3 string, add it to the queue, and start playback. */
export function enqueue(base64Mp3) {
    const url = _blobUrl(base64Mp3);
    queue.push(url);
    if (!playing) _playNext();
}

/** Stop playback immediately and discard any queued audio. */
export function clearQueue() {
    queue.splice(0).forEach(u => { try { URL.revokeObjectURL(u); } catch {} });
    player.pause();
    _revokePlayerSrc();
    playing = false;
    announced = false;
}

// ── Internals ─────────────────────────────────────────────────────────────────

function _playNext() {
    if (!queue.length) {
        playing = false;
        announced = false;     // next utterance should re-announce its own start
        onIdleCb?.();          // natural end of playback (not interruption) — real ground truth
        return;
    }
    playing     = true;
    player.src  = queue.shift();
    player.play().catch(err => {
        console.warn('[Player] Play failed:', err);
        _revokePlayerSrc();
        _playNext();
    });
}

function _revokePlayerSrc() {
    if (player.src) {
        try { URL.revokeObjectURL(player.src); } catch {}
        player.src = '';
    }
}

function _blobUrl(base64) {
    const raw   = atob(base64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
}

// Fires once playback is actually audible (after buffering) — the reliable
// moment to start the avatar's talking animation, as opposed to when the
// audio_chunk message merely arrived over the WebSocket.
//
// Guard on blob: URLs only — app.js's unlockAudio() plays a silent data: URI
// on this SAME shared #audioPlayer element (once per session, to satisfy
// Chrome's autoplay gesture requirement) before the greeting ever arrives.
// That also fires 'playing', which would otherwise consume `announced` and
// leave the real greeting's own 'playing' event with nothing to trigger —
// the avatar would flip on too early (during the silent unlock blip) and
// then never get told the REAL audio started, looking frozen for the
// whole greeting. Only our own queued TTS clips count.
player.addEventListener('playing', () => {
    if (!announced && player.currentSrc.startsWith('blob:')) {
        announced = true;
        onFirstPlayingCb?.();
    }
});

// Advance queue when current sentence finishes
player.addEventListener('ended', () => {
    _revokePlayerSrc();
    _playNext();
});
