/**
 * modules/ws-client.js
 * Thin WebSocket wrapper. Handles connection, keep-alive ping, and JSON sending.
 * The caller supplies handler callbacks via connect(handlers).
 */

// Auto-detect backend URL: same host in production, localhost in dev.
const _proto = location.protocol === 'https:' ? 'wss' : 'ws';
const _host  = location.hostname === 'localhost' || location.hostname === '127.0.0.1'
    ? 'localhost:8000'
    : location.host.replace(/^[^.]+/, (name) => name.replace(/-frontend$/, '-backend'));
export const WS_URL = `${_proto}://${_host}/ws/voice`;
const PING_INTERVAL  = 25_000; // ms

let socket    = null;
let pingTimer = null;

/**
 * Open a WebSocket connection to the voice backend.
 * @param {{ onOpen, onMessage, onClose, onError }} handlers
 */
export function connect(handlers = {}) {
    socket           = new WebSocket(WS_URL);
    socket.binaryType = 'arraybuffer';

    socket.onopen = () => {
        _startPing();
        handlers.onOpen?.();
    };

    socket.onclose = () => {
        _stopPing();
        handlers.onClose?.();
    };

    socket.onerror = (e) => handlers.onError?.(e);

    socket.onmessage = ({ data }) => {
        if (typeof data !== 'string') return;
        let msg;
        try { msg = JSON.parse(data); } catch { return; }
        handlers.onMessage?.(msg);
    };
}

/** Send raw binary (PCM16 ArrayBuffer from the AudioWorklet). */
export function sendBinary(buffer) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(buffer);
}

/** Send a JSON control message. */
export function sendJSON(obj) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(obj));
}

/** Close the socket and stop the ping timer. */
export function disconnect() {
    _stopPing();
    socket?.close();
    socket = null;
}

function _startPing() {
    pingTimer = setInterval(() => sendJSON({ type: 'ping' }), PING_INTERVAL);
}

function _stopPing() {
    clearInterval(pingTimer);
    pingTimer = null;
}
