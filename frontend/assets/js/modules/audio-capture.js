/**
 * modules/audio-capture.js
 * Opens the microphone with the browser's echo-cancellation + noise-suppression
 * engaged, creates an AudioContext at 16 kHz, loads the AudioWorklet processor,
 * and forwards PCM16 chunks to a callback.
 *
 * Echo cancellation matters here: the bot's TTS plays through the speakers, and
 * without AEC the mic re-captures it and the VAD barges in on the bot's own
 * voice. We therefore:
 *   • let getUserMedia run its full audio-processing module (AEC/NS/AGC) at the
 *     device's NATIVE rate — pinning sampleRate here can disable the canceller —
 *   • resample to 16 kHz via the AudioContext for Deepgram (linear16, 16 kHz).
 * (Headphones remain the only way to eliminate acoustic echo completely.)
 */

const WORKLET_MODULE = 'assets/js/audio-processor.js';

let audioCtx    = null;
let mediaStream = null;
let workletNode = null;

/**
 * Start capturing microphone audio and call `onChunk` with each PCM16 buffer.
 * @param {(buffer: ArrayBuffer) => void} onChunk
 * @throws {Error} if microphone access is denied or AudioWorklet fails
 */
export async function startCapture(onChunk) {
    mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
            channelCount:     1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl:  true,
            // NOTE: intentionally NO sampleRate here — forcing a capture rate can
            // bypass Chrome's echo-canceller. AudioContext below resamples to 16k.
            // Best-effort extra hints; browsers silently ignore unsupported ones.
            advanced: [
                { echoCancellation: true },
                { noiseSuppression: true },
                { autoGainControl:  true },
                { voiceIsolation:   true },   // Safari 17+/newer Chromium
                { googEchoCancellation:             true },
                { googExperimentalEchoCancellation: true },
                { googAutoGainControl:              true },
                { googNoiseSuppression:             true },
                { googExperimentalNoiseSuppression: true },
                { googHighpassFilter:               true },
                { googTypingNoiseDetection:         true },
            ],
        },
    });

    // Log what the browser actually applied, so AEC can be verified.
    const settings = mediaStream.getAudioTracks()[0]?.getSettings?.() ?? {};
    console.log('[Capture] Mic settings:', {
        sampleRate:       settings.sampleRate,
        echoCancellation: settings.echoCancellation,
        noiseSuppression: settings.noiseSuppression,
        autoGainControl:  settings.autoGainControl,
    });
    if (settings.echoCancellation === false) {
        console.warn('[Capture] Echo cancellation NOT active — use headphones to avoid self-interruption.');
    }

    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 16000,
    });

    await audioCtx.audioWorklet.addModule(WORKLET_MODULE);

    const source = audioCtx.createMediaStreamSource(mediaStream);
    workletNode  = new AudioWorkletNode(audioCtx, 'pcm16-processor');

    workletNode.port.onmessage = ({ data }) => onChunk(data);

    source.connect(workletNode);
    // Keep the graph pulling without routing mic audio to the speakers:
    // the worklet emits no output, so a zero-gain sink is purely a keep-alive.
    const sink = audioCtx.createGain();
    sink.gain.value = 0;
    workletNode.connect(sink).connect(audioCtx.destination);

    console.log('[Capture] Mic open @', audioCtx.sampleRate, 'Hz (AEC/NS/AGC requested)');
}

/** Release the microphone and tear down the audio graph. */
export function stopCapture() {
    workletNode?.disconnect();   workletNode = null;
    audioCtx?.close();           audioCtx    = null;
    mediaStream?.getTracks().forEach(t => t.stop());
    mediaStream = null;
    console.log('[Capture] Mic released');
}

/** The live mic MediaStream (for the visualizer). Null when not capturing. */
export function getMicStream() {
    return mediaStream;
}
