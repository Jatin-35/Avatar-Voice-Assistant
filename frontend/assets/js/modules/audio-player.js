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

/** Register a callback fired when the queue drains naturally (bot finished). */
export function setOnIdle(fn) { onIdleCb = fn; }

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
}

// ── Internals ─────────────────────────────────────────────────────────────────

function _playNext() {
    if (!queue.length) {
        playing = false;
        onIdleCb?.();          // natural end of playback (not interruption)
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

// Advance queue when current sentence finishes
player.addEventListener('ended', () => {
    _revokePlayerSrc();
    _playNext();
});
