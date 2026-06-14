"""
shared/ — transport-agnostic voice primitives reused across the backend.

Modules:
  deepgram_stt     — Deepgram live-streaming STT WebSocket client
  utterance_buffer — debounce/dedupe buffer for STT results

Imported normally as `from shared.deepgram_stt import DeepgramSTTHandler`
(no sys.path manipulation needed — this is a package under backend/).
"""
