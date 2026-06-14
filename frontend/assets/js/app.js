/**
 * app.js — Avatar Bot · main entry point
 *
 * Data flow:
 *   Mic → AudioWorklet (PCM16) → WebSocket → Deepgram STT
 *   Deepgram transcript → Azure OpenAI LLM (streaming)
 *   LLM tokens → Azure TTS → base64 MP3 → audio queue → <audio>
 *
 * Turn-taking (half-duplex + tap-to-talk interrupt):
 *   • While the bot speaks the mic is MUTED (we stream silence) → no echo.
 *   • The mic auto-reopens when the bot finishes.
 *   • To interrupt: TAP the mic button → mic opens while the bot keeps talking →
 *     when YOU actually speak, the bot is cut off. (Echo cancellation keeps the
 *     bot's own voice out of the detector, so it triggers on you, not the bot.)
 *   • Tap again while "Listening" to cancel (re-mute, bot continues).
 */

import { startCapture, stopCapture, getMicStream } from './modules/audio-capture.js';
import { enqueue, clearQueue, setOnIdle } from './modules/audio-player.js';
import { connect, sendBinary, sendJSON, disconnect } from './modules/ws-client.js';
import { initBotVisualizer, initMicVisualizer, startViz, setVizMode, resumeViz } from './modules/visualizer.js';
import {
    micBtn, setState, setMicActive, setMicSpeaking, setMicArmed, resetUI,
    appendScreenText, finaliseScreenText, clearScreenText,
    setTranscript, clearTranscript, setAvatarSpeaking,
} from './modules/ui.js';

// ── Session state ─────────────────────────────────────────────────────────────
let active       = false;
let botSpeaking  = false;   // bot is currently playing audio
let micMuted     = false;   // while true, stream silence instead of mic audio
let armed        = false;   // user tapped "Speak": mic open over the bot, awaiting voice
let armedHits    = 0;
let safetyTimer  = null;    // fallback if audio_complete never arrives
let greetingDone = false;   // mic stays muted until the opening greeting is delivered
let greetingTimer = null;

const SAFETY_TIMEOUT_MS = 8000;   // fallback: force-idle if audio_complete never arrives
const SPEECH_RMS        = 0.025;  // energy gate to detect the user's voice
const SPEECH_HITS       = 2;      // consecutive loud chunks (~0.5s) before interrupting

// RMS (0..1) of a PCM16 chunk.
function rmsOf(buf) {
    const s = new Int16Array(buf);
    if (!s.length) return 0;
    let sum = 0;
    for (let i = 0; i < s.length; i++) { const v = s[i] / 32768; sum += v * v; }
    return Math.sqrt(sum / s.length);
}

// ── Turn control ───────────────────────────────────────────────────────────────
function enterSpeaking() {
    botSpeaking = true;
    armed = false; armedHits = 0;
    micMuted = true;
    setMicArmed(false);
    setMicSpeaking(true);     // button: "Speak"
    setVizMode('bot');        // visualize Sakshi's voice
    clearTimeout(safetyTimer);
}

function exitSpeaking() {
    botSpeaking = false;
    armed = false; armedHits = 0;
    micMuted = false;
    greetingDone = true;     // first bot-finish = greeting delivered → mic may open
    setMicArmed(false);
    setMicSpeaking(false);    // button: "Stop"
    setVizMode('mic');        // visualize the user again
}

// Tap while the bot talks → open the mic (bot keeps playing), wait for your voice.
function armInterrupt() {
    armed = true; armedHits = 0;
    micMuted = false;        // mic now streams real audio
    setMicArmed(true);       // button: "Listening"
    setVizMode('mic');
    console.log('[App] Mic open — speak to interrupt');
}

// Tap again while armed → cancel; re-mute, bot keeps playing.
function disarm() {
    armed = false; armedHits = 0;
    micMuted = true;
    setMicArmed(false);
    setMicSpeaking(true);     // back to "Speak"
    console.log('[App] Interrupt cancelled');
}

// Your voice was detected while armed → actually interrupt the bot.
function commitInterrupt() {
    console.log('[App] Speech detected — interrupting bot');
    armed = false; armedHits = 0;
    sendJSON({ type: 'interrupt' });   // server cancels pipeline + opens STT gate
    clearQueue();                       // stop bot audio
    clearTimeout(safetyTimer);
    exitSpeaking();                     // mic stays open for your turn
    setState('listening');
    setAvatarSpeaking(false);
}

// Audio player queue drained — server will send audio_complete when truly done.
// This is only a safety fallback in case that message never arrives.
function onPlaybackIdle() {
    clearTimeout(safetyTimer);
    safetyTimer = setTimeout(() => {
        if (!botSpeaking) return;
        console.warn('[App] audio_complete fallback — forcing idle');
        exitSpeaking();
        setAvatarSpeaking(false);
        setState('listening');
    }, SAFETY_TIMEOUT_MS);
}

// Per-mic-chunk: detect the user's voice while armed, then forward (or mute).
function onMicChunk(chunk) {
    if (armed) {
        if (rmsOf(chunk) >= SPEECH_RMS) {
            if (++armedHits >= SPEECH_HITS) { commitInterrupt(); }
        } else {
            armedHits = 0;
        }
    }
    sendBinary(micMuted ? new ArrayBuffer(chunk.byteLength) : chunk);
}

// ── Audio unlock (Chrome blocks autoplay without prior user gesture) ──────────
async function unlockAudio() {
    const player = document.getElementById('audioPlayer');
    const silentWav =
        'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
    player.muted = true;
    player.src   = silentWav;
    try { await player.play(); } catch { /* ignore */ }
    player.pause();
    player.muted = false;
    player.src   = '';
}

// ── WebSocket message router ──────────────────────────────────────────────────
const HANDLERS = {

    session_ready(msg) {
        console.log('[App] Session:', msg.session_id);
    },

    partial_transcript(msg) {
        setTranscript(msg.text);
    },

    final_transcript(msg) {
        setTranscript(msg.text);
        clearScreenText();
    },

    llm_chunk(msg) {
        appendScreenText(msg.text);
    },

    audio_chunk(msg) {
        finaliseScreenText();
        clearTranscript();
        enqueue(msg.data);
        setState('speaking');
        setAvatarSpeaking(true);
        enterSpeaking();             // ← mute mic while bot talks
    },

    audio_complete() {
        // Server says all sentences finished playing — stop avatar and open mic.
        clearTimeout(safetyTimer);
        exitSpeaking();
        setAvatarSpeaking(false);
        setState('listening');
        console.log('[App] audio_complete — bot finished, mic reopened');
    },

    audio_clear() {
        clearQueue();
        clearTimeout(safetyTimer);
        exitSpeaking();
        setState('listening');
        setAvatarSpeaking(false);
    },

    interrupted() {
        clearQueue();
        clearTimeout(safetyTimer);
        exitSpeaking();
        setState('listening');
        setAvatarSpeaking(false);
    },

    agent_sources(msg) {
        console.log('[App] KB sources:', msg.sources);
    },

    // Don't override "speaking" — mute is client-driven, server "listening" can be early
    state(msg) {
        switch (msg.value) {
            case 'listening':   if (!botSpeaking) setState('listening'); break;
            case 'searching_kb':if (!botSpeaking) setState('searching_kb'); break;
            case 'processing':  if (!botSpeaking) setState('processing'); break;
            case 'speaking':    setState('speaking'); break;
            case 'disconnecting':
                setState('idle');
                setTimeout(stop, 1500);
                break;
        }
    },

    error(msg) {
        console.error('[WS] Server error:', msg.message);
        setState('error');
    },

    pong() { /* keep-alive reply */ },
};

// ── Session management ────────────────────────────────────────────────────────
async function start() {
    if (active) return;
    active = true;
    greetingDone = false;
    micMuted = true;             // no mic input until the greeting is delivered

    // Safety net: if the greeting never plays, open the mic anyway after a while.
    clearTimeout(greetingTimer);
    greetingTimer = setTimeout(() => {
        if (!greetingDone) {
            greetingDone = true;
            micMuted = false;
            console.warn('[App] Greeting fallback — mic opened');
        }
    }, 12000);

    resetUI();
    setMicActive(true);
    setState('connecting');

    await unlockAudio();

    // Wire the bot visualizer (audio element exists now) and resume audio context
    initBotVisualizer(document.getElementById('audioPlayer'));
    resumeViz();
    setVizMode('idle');

    connect({
        onOpen: async () => {
            setState('listening');
            try {
                await startCapture(onMicChunk);
                initMicVisualizer(getMicStream());
            } catch (err) {
                console.error('[App] Mic error:', err.name, err.message);
                setState('error');
                stop();
            }
        },

        onMessage: (msg) => {
            const handler = HANDLERS[msg.type];
            if (handler) handler(msg);
            else console.warn('[App] Unknown message type:', msg.type);
        },

        onClose: () => { if (active) stop(); },

        onError: () => setState('error'),
    });
}

function stop() {
    if (!active) return;
    active = false;

    clearTimeout(safetyTimer);
    clearTimeout(greetingTimer);
    greetingDone = false;
    stopCapture();
    clearQueue();
    disconnect();
    exitSpeaking();          // setMicSpeaking(false)
    setAvatarSpeaking(false);
    setMicActive(false);     // → "Start"
    setState('idle');
    setVizMode('idle');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
micBtn.disabled = false;
setState('idle');
setOnIdle(onPlaybackIdle);
startViz(document.getElementById('viz'));   // idle bars until a session starts

micBtn.addEventListener('click', () => {
    if (!active)                       start();        // start session
    else if (botSpeaking && !greetingDone) return;    // can't interrupt the greeting
    else if (armed)                    disarm();       // cancel listening
    else if (botSpeaking)              armInterrupt(); // open mic to talk over the bot
    else                               stop();         // stop session
});
