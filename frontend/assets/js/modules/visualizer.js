/**
 * modules/visualizer.js
 * Live audio visualizer (canvas bars) driven by Web Audio AnalyserNodes.
 *
 *   • Mic  → MediaStreamSource → analyserMic   (green; reacts to the user)
 *   • Bot  → MediaElementSource(<audio>) → analyserBot → destination (blue)
 *
 * setVizMode('mic' | 'bot' | 'idle') picks which analyser drives the bars.
 * Everything is defensive: if Web Audio fails, the app keeps working (the bars
 * just stay idle, and bot audio still plays because we only reroute on success).
 */

let ctx         = null;
let analyserBot = null;
let analyserMic = null;
let botWired    = false;
let canvas, cctx, raf = null;
let mode = 'idle';
let bars = null;          // smoothed bar heights

function ensureCtx() {
    if (!ctx) {
        try { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
        catch (e) { console.warn('[Viz] no AudioContext', e); }
    }
    return ctx;
}

/** Route the bot <audio> element through an analyser (once). */
export function initBotVisualizer(audioEl) {
    if (botWired || !ensureCtx()) return;
    try {
        const src = ctx.createMediaElementSource(audioEl);
        analyserBot = ctx.createAnalyser();
        analyserBot.fftSize = 256;
        analyserBot.smoothingTimeConstant = 0.7;
        src.connect(analyserBot);
        analyserBot.connect(ctx.destination);   // keep audio audible
        botWired = true;
        console.log('[Viz] bot analyser wired');
    } catch (e) {
        // createMediaElementSource can only run once / may be blocked — bot audio
        // still plays normally; we just won't visualize it.
        console.warn('[Viz] bot analyser unavailable:', e.message);
    }
}

/** Attach the mic stream to an analyser (call whenever a new stream opens). */
export function initMicVisualizer(stream) {
    if (!ensureCtx() || !stream) return;
    try {
        const src = ctx.createMediaStreamSource(stream);
        analyserMic = ctx.createAnalyser();
        analyserMic.fftSize = 256;
        analyserMic.smoothingTimeConstant = 0.6;
        src.connect(analyserMic);               // NOT connected to destination (no feedback)
        console.log('[Viz] mic analyser wired');
    } catch (e) {
        console.warn('[Viz] mic analyser unavailable:', e.message);
    }
}

export function resumeViz() { try { ctx?.resume?.(); } catch {} }
export function setVizMode(m) { mode = m; }

/** Start the render loop on a canvas. */
export function startViz(canvasEl) {
    canvas = canvasEl;
    cctx = canvas.getContext('2d');
    if (!raf) _loop();
}

export function stopViz() {
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    mode = 'idle';
}

function _loop() {
    raf = requestAnimationFrame(_loop);
    if (!cctx) return;

    const W = canvas.width, H = canvas.height;
    cctx.clearRect(0, 0, W, H);

    const N = 40;                       // number of bars
    if (!bars) bars = new Array(N).fill(0);

    const analyser = mode === 'bot' ? analyserBot : mode === 'mic' ? analyserMic : null;
    let freq = null;
    if (analyser) {
        freq = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(freq);
    }


    const accent = mode === 'bot'
        ? 'rgba(37,99,235,'      // blue (Laxmi talking)
        : 'rgba(46,158,79,';     // green (you)
    const gap = 3;
    const bw  = (W - gap * (N - 1)) / N;

    for (let i = 0; i < N; i++) {
        // target level for this bar
        let target = 0;
        if (freq) {
            const idx = Math.floor((i / N) * (freq.length * 0.7));
            target = (freq[idx] / 255);
        } else {
            // idle: gentle breathing ripple
            target = 0.06 + 0.04 * Math.sin(Date.now() / 380 + i * 0.5) ** 2;
        }
        // smooth
        bars[i] += (target - bars[i]) * 0.35;
        const h = Math.max(2, bars[i] * (H - 4));
        const x = i * (bw + gap);
        const y = (H - h) / 2;
        const alpha = freq ? (0.35 + bars[i] * 0.65) : 0.3;
        cctx.fillStyle = accent + alpha + ')';
        _roundRect(cctx, x, y, bw, h, Math.min(bw / 2, 3));
        cctx.fill();
    }
}

function _roundRect(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r);
    c.closePath();
}
