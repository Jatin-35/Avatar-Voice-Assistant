/**
 * modules/vad.js — client-side voice-activity detection (Silero, in-browser).
 *
 * Uses @ricky0123/vad-web (Silero VAD via onnxruntime-web). Speech onset is
 * detected locally and reported via onSpeechStart — so barge-in no longer
 * depends on the server (Deepgram) VAD.
 *
 * Loads the library + model from CDN at runtime (no build step). If anything
 * fails to load, startVAD() resolves false and the app keeps working without
 * barge-in (it just won't auto-interrupt).
 */

const ORT_VERSION = '1.14.0';
const VAD_VERSION = '0.0.7';
const ORT_URL = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/ort.js`;
const VAD_URL = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_VERSION}/dist/bundle.min.js`;
const VAD_ASSET_BASE = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_VERSION}/dist/`;
const ORT_WASM_BASE  = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`;

let _vad = null;

function _loadScript(src) {
    return new Promise((resolve, reject) => {
        if (document.querySelector(`script[src="${src}"]`)) return resolve();
        const s = document.createElement('script');
        s.src = src;
        s.async = true;
        s.onload = () => resolve();
        s.onerror = () => reject(new Error(`failed to load ${src}`));
        document.head.appendChild(s);
    });
}

/**
 * Start client-side VAD. Calls onSpeechStart() the instant the user begins
 * speaking. Returns true if it started, false if it could not load.
 * @param {{ onSpeechStart?: () => void, onSpeechEnd?: () => void }} cbs
 */
export async function startVAD({ onSpeechStart, onSpeechEnd } = {}) {
    try {
        await _loadScript(ORT_URL);
        await _loadScript(VAD_URL);
        if (!window.vad?.MicVAD) throw new Error('vad-web global not available');

        _vad = await window.vad.MicVAD.new({
            // Tell the lib where to fetch its worklet + silero model + wasm
            baseAssetPath:    VAD_ASSET_BASE,
            onnxWASMBasePath: ORT_WASM_BASE,
            // The VAD opens its own mic — keep echo cancellation on so it doesn't
            // hear the bot's own voice through the speakers.
            additionalAudioConstraints: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl:  true,
            },
            // Sensitivity (Silero probability gates). Stricter so background
            // noise / short blips don't trip barge-in.
            positiveSpeechThreshold: 0.82,
            negativeSpeechThreshold: 0.45,
            minSpeechFrames: 5,
            redemptionFrames: 10,
            onSpeechStart: () => { try { onSpeechStart?.(); } catch (e) { console.warn('[VAD] onSpeechStart error', e); } },
            onSpeechEnd:   () => { try { onSpeechEnd?.();   } catch (e) { console.warn('[VAD] onSpeechEnd error', e); } },
            // A misfire = a speech segment too short to be real → treat as "ended".
            onVADMisfire:  () => { try { onSpeechEnd?.();   } catch (e) { console.warn('[VAD] onVADMisfire error', e); } },
        });
        _vad.start();
        console.log('[VAD] Silero client-side VAD started');
        return true;
    } catch (err) {
        console.warn('[VAD] Client-side VAD unavailable (barge-in off):', err.message);
        _vad = null;
        return false;
    }
}

/** Stop and release the VAD (and its mic). */
export function stopVAD() {
    try { _vad?.pause?.(); } catch {}
    try { _vad?.destroy?.(); } catch {}
    _vad = null;
}

export function isVADActive() {
    return _vad !== null;
}
