"""Piper worker: keeps voices loaded, answers length-prefixed requests on stdin/stdout.

Run under the .venv-tts interpreter (needs numpy + piper-tts). Never imported by
the controller; driven as a subprocess by tts.py.

Protocol (binary-safe):
  request  : one JSON line  {"text": str, "voice": str, "rate": int, "gain": float}
             or {"cmd": "quit"}
  response : one JSON line  {"ok": true, "sr": 48000, "n": <byte count>}
             followed by exactly <n> bytes of mono s16le 48 kHz PCM.
             On failure {"ok": false, "err": "..."} and no payload.

Design notes
------------
Piper emits 22050 Hz mono s16le. The Echo Dot speaker runs at 48000 Hz, so the
rate must be converted with a real anti-aliasing filter (22050 -> 48000 is a
320/147 rational ratio). Naive linear interpolation folds images back into the
passband and is a large part of why cheap pipelines sound gritty; the polyphase
filter here is a 24-tap-per-phase Kaiser-windowed sinc.
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import traceback
import wave

import numpy as np

OUT_SR = 48000
L, M = 320, 147          # 48000/22050 == 320/147 (exact)
TAPS_PER_PHASE = 24
KAISER_BETA = 8.0

# name -> (model file, base length_scale)
# base length_scale is the tuned default measured on this host: at rate 0 the
# articulation rate lands ~17.5 chars/s for lessac-high (SAPI David measures
# 19.7 chars/s with 35% dead silence, which is what reads as "rushed").
VOICES = {
    "lessac-high":   ("en_US-lessac-high.onnx",   1.28),
    "lessac-medium": ("en_US-lessac-medium.onnx", 1.30),
    "lessac":        ("en_US-lessac-high.onnx",   1.28),
    "amy":           ("en_US-amy-medium.onnx",    1.02),
    "ryan":          ("en_US-ryan-high.onnx",     1.22),
}
DEFAULT_VOICE = "lessac-high"

VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices")


# --------------------------------------------------------------------------- #
# resampling
# --------------------------------------------------------------------------- #
_h_cache: dict[int, np.ndarray] = {}


def _design_polyphase():
    n = L * TAPS_PER_PHASE
    c = (n - 1) / 2.0
    k = np.arange(n, dtype=np.float64)
    h = np.sinc((k - c) / L) * np.kaiser(n, KAISER_BETA)
    h *= L / h.sum()                      # unity DC gain per output sample
    return h.reshape(TAPS_PER_PHASE, L)   # G[t, r] == h[t*L + r]


G = _design_polyphase()
# group delay of the prototype, expressed in output samples
DELAY_OUT = int(round(((L * TAPS_PER_PHASE - 1) / 2.0) / M))


def resample_to_48k(x: np.ndarray) -> np.ndarray:
    """Rational resample int16 mono -> int16 mono at 48000 Hz, delay compensated."""
    xf = x.astype(np.float64)
    pad = TAPS_PER_PHASE + 4
    xp = np.concatenate((np.full(pad, xf[0]), xf, np.full(pad, xf[-1])))

    nout = int(round(len(xf) * OUT_SR / 22050.0))
    n = np.arange(nout + DELAY_OUT, dtype=np.int64)
    prod = n * M
    phase = prod % L
    q = prod // L + pad            # xpq[t] == x[q - t]

    y = np.zeros(len(n), dtype=np.float64)
    # phases repeat every L outputs (M and L are coprime), so handle them in blocks
    order = np.argsort(phase, kind="stable")
    phase_s = phase[order]
    bounds = np.searchsorted(phase_s, np.arange(L + 1))
    for r in range(L):
        a, b = bounds[r], bounds[r + 1]
        if a == b:
            continue
        idx = order[a:b]
        qq = q[idx]
        acc = np.zeros(len(idx), dtype=np.float64)
        g = G[:, r]
        for t in range(TAPS_PER_PHASE):
            acc += g[t] * xp[qq - t]
        y[idx] = acc

    y = y[DELAY_OUT:DELAY_OUT + nout]
    if len(y) < nout:                     # tail safety
        y = np.concatenate((y, np.zeros(nout - len(y))))
    return y


def finish_audio(y: np.ndarray, peak_dbfs: float = -1.0) -> bytes:
    """Peak-limit and trim edge silence, then return s16le bytes."""
    if y.size == 0:
        return b""
    # trim: keep at most 60 ms of lead silence, 180 ms of tail silence
    win = int(0.01 * OUT_SR)
    nw = y.size // win
    if nw > 0:
        rms = np.sqrt(np.mean(y[: nw * win].reshape(nw, win) ** 2, axis=1))
        voiced = np.flatnonzero(rms > 32768 * 10 ** (-55 / 20))
        if voiced.size:
            head = max(0, int(voiced[0] * win) - int(0.06 * OUT_SR))
            tail = min(y.size, int((voiced[-1] + 1) * win) + int(0.18 * OUT_SR))
            y = y[head:tail]
    peak = float(np.max(np.abs(y))) or 1.0
    target = 32767.0 * 10 ** (peak_dbfs / 20.0)
    if peak > target:
        y = y * (target / peak)
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
_voices: dict[str, object] = {}


def _length_scale(voice: str, rate: int) -> float:
    base = VOICES.get(voice, (None, 1.10))[1]
    # rate -10..+10 (SAPI convention): + is faster
    r = max(-10, min(10, int(rate)))
    return max(0.60, min(1.90, base * (1.0 - 0.04 * r)))


def _load(voice: str):
    key = VOICES.get(voice, (voice, 1.1))[0]
    path = key if os.path.isabs(key) else os.path.join(VOICE_DIR, key)
    if not os.path.exists(path):
        raise FileNotFoundError(f"voice model not found: {path}")
    if path not in _voices:
        from piper import PiperVoice
        _voices[path] = PiperVoice.load(path)
    return _voices[path]


def synth(text: str, voice: str = DEFAULT_VOICE, rate: int = 0,
          gain_dbfs: float = -1.0) -> bytes:
    from piper.config import SynthesisConfig

    text = " ".join(str(text).split())
    if not text:
        return b""
    v = _load(voice)
    cfg = SynthesisConfig(
        length_scale=_length_scale(voice, rate),
        noise_scale=0.60,
        noise_w_scale=0.80,      # steadier rhythm, less "rushed" variance
        normalize_audio=True,
    )
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(v.config.sample_rate)
    v.synthesize_wav(text, w, cfg)
    w.close()
    buf.seek(0)
    r = wave.open(buf, "rb")
    pcm = np.frombuffer(r.readframes(r.getnframes()), dtype="<i2")
    r.close()
    if pcm.size == 0:
        return b""
    return finish_audio(resample_to_48k(pcm), gain_dbfs)


# --------------------------------------------------------------------------- #
# server loop
# --------------------------------------------------------------------------- #
def main() -> int:
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    while True:
        line = inp.readline()
        if not line:
            return 0
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as e:
            resp = {"ok": False, "err": f"bad request json: {e}"}
            out.write((json.dumps(resp) + "\n").encode()); out.flush()
            continue
        if req.get("cmd") == "quit":
            return 0
        try:
            pcm = synth(req.get("text", ""),
                        req.get("voice", DEFAULT_VOICE),
                        int(req.get("rate", 0) or 0),
                        float(req.get("gain", -1.0)))
            out.write((json.dumps({"ok": True, "sr": OUT_SR, "n": len(pcm)}) + "\n").encode())
            out.write(pcm)
            out.flush()
        except Exception:
            resp = {"ok": False, "err": traceback.format_exc(limit=3)}
            out.write((json.dumps(resp) + "\n").encode()); out.flush()


if __name__ == "__main__":
    sys.exit(main())
