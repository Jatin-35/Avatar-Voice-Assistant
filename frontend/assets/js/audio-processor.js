/**
 * audio-processor.js — AudioWorklet processor
 * Converts Float32 mic samples to Int16 PCM and forwards in 4096-sample
 * chunks (~256ms at 16kHz) to the main thread via postMessage.
 */
class PCM16Processor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._buf = [];
        this._chunkSize = 4096;
    }

    process(inputs) {
        const channel = inputs[0]?.[0];
        if (!channel) return true;

        for (let i = 0; i < channel.length; i++) {
            this._buf.push(channel[i]);
        }

        while (this._buf.length >= this._chunkSize) {
            const chunk = this._buf.splice(0, this._chunkSize);
            const int16 = new Int16Array(this._chunkSize);
            for (let i = 0; i < this._chunkSize; i++) {
                const s = Math.max(-1, Math.min(1, chunk[i]));
                int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }
            this.port.postMessage(int16.buffer, [int16.buffer]);
        }

        return true;
    }
}

registerProcessor('pcm16-processor', PCM16Processor);
