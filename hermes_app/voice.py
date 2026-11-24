"""Hermes voice-input layer for the Echo Dot endpoint.

Four pieces, all tuned and measured against real captured audio from the
Echo Dot 2nd-gen stream (16 kHz, mono, s16le little-endian):

  detect_turns(pcm_iter)  streaming energy + zero-crossing VAD -> Turn objects
  transcribe(wav_path)    faster_whisper wrapper (tiny by default)
  is_wake(text)           fuzzy wake-word test tolerant of "hear me"/"hermie"
  fingerprint(wav_path)   MFCC-statistics speaker fingerprint (+ distance)

Nothing here touches the serial port, the controller, or the network beyond
faster_whisper's normal model cache.

Constants are justified by measurements in MEASUREMENTS.md:
  * room floor measured at ~-41 dBFS RMS in quiet stretches;
  * household background media sits at -24..-34 dBFS, only ~8-12 dB under
    near-field speech, so a fixed threshold cannot separate them;
  * near-field commands peaked at -13..-17 dBFS.
Hence an adaptive floor with a dB margin, plus a min-turn filter that throws
away sub-0.6 s transients (clicks, laughter, door bumps) - loud, but not speech.
"""

from __future__ import annotations

import math
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import numpy as np

SR = 16000
S16_MAX = 32768.0

# ---- VAD geometry -------------------------------------------------------
FRAME = 320          # 20 ms analysis frame (samples) for frame_features()
HOP = 160            # 10 ms hop between frames (samples)
BLOCK = 320          # 10 ms VAD block == 320 *bytes* of s16le == 160 samples
BLOCKS_PER_S = 100   # VAD blocks per second

# ---- VAD defaults (see MEASUREMENTS.md for the numbers behind them) -----
NOISE_WIN_S = 2.5        # window for noise-floor statistics
NOISE_PCT = 10.0         # percentile of that window treated as the floor
FLOOR_MAX_DB = -50.0     # never assume a quieter floor than this
FLOOR_MIN_DB = -32.0     # never track a floor louder than this
MARGIN_DB = 9.0          # speech must beat the floor by this much
ABS_MIN_DB = -45.0       # hard floor so digital silence never triggers
ONSET_S = 0.25           # sustained energy needed to open a turn
END_SILENCE_S = 0.9      # close a turn after this much silence
MIN_TURN_S = 0.60        # shorter than this is a click, not a turn
MAX_TURN_S = 20.0        # force-close runaway turns
PREROLL_S = 0.35         # audio kept before the detected onset (>= onset window)
RELEASE_DB = 3.0         # hysteresis: this far under threshold counts as silence
FLATNESS_MAX = 0.86      # median spectral flatness ceiling for a real turn
# Near-field gate. Measured on the real capture: background media peaks at
# floor+18..+22 dB but only covers 0.06-0.51 of the best 1 s window; near-field
# commands cover 0.71-0.88. Requiring 0.6 of a 1 s window above floor+15 dB
# keeps both commands and drops the media turns plus the loud non-speech
# transients (door bang at 122.6 s: 0.21).
GATE_DB = 15.0
SUSTAIN_WIN_F = 100      # 1.0 s windows
SUSTAIN_MIN = 0.60


# =========================================================================
# PCM helpers
# =========================================================================
def pcm_to_float(buf: bytes) -> np.ndarray:
    """s16le bytes -> float32 in [-1, 1)."""
    if len(buf) % 2:
        buf = buf[:-1]
    return np.frombuffer(buf, dtype="<i2").astype(np.float32) / S16_MAX


def float_to_pcm(x: np.ndarray) -> bytes:
    """float32 in [-1, 1] -> s16le bytes."""
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def save_wav(path: str, pcm: bytes, sr: int = SR) -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """Read a PCM wav, converting multi-channel / other rates to 16 kHz mono."""
    with wave.open(path, "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / S16_MAX
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        x = _resample(x, rate, SR)
    return x.astype(np.float32), SR


def _resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Linear-interpolation resampler (adequate for speech at these rates)."""
    if sr_from == sr_to or len(x) == 0:
        return x
    n_out = int(round(len(x) / float(sr_from) * sr_to))
    t_out = np.arange(n_out) / float(sr_to)
    t_in = np.arange(len(x)) / float(sr_from)
    return np.interp(t_out, t_in, x).astype(np.float32)


def dbfs(rms: float) -> float:
    return 20.0 * math.log10(max(rms, 1e-9))


# =========================================================================
# Frame features
# =========================================================================
def _zcr(frame: np.ndarray) -> float:
    if len(frame) < 2:
        return 0.0
    return float(np.mean(np.signbit(frame[1:]) != np.signbit(frame[:-1])))


def _flatness(frame: np.ndarray) -> float:
    """Spectral flatness: ~1.0 for white noise, well under 0.5 for voiced speech."""
    if len(frame) < 32:
        return 1.0
    s = np.abs(np.fft.rfft(frame * np.hanning(len(frame))))[1:] + 1e-10
    return float(np.exp(np.mean(np.log(s))) / np.mean(s))


def frame_features(x: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (rms_db, zcr, flatness) arrays per 20 ms frame with 10 ms hop."""
    if len(x) < FRAME:
        z = np.zeros(0, np.float32)
        return z, z.copy(), z.copy()
    n = 1 + (len(x) - FRAME) // HOP
    fr = x[np.arange(n)[:, None] * HOP + np.arange(FRAME)[None, :]]
    rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
    db = (20.0 * np.log10(rms)).astype(np.float32)
    sgn = np.signbit(fr)
    zcr = np.mean(sgn[:, 1:] != sgn[:, :-1], axis=1).astype(np.float32)
    mag = np.abs(np.fft.rfft(fr * np.hanning(FRAME), axis=1))[:, 1:] + 1e-10
    flat = (np.exp(np.mean(np.log(mag), axis=1)) / np.mean(mag, axis=1)).astype(np.float32)
    return db, zcr, flat


# =========================================================================
# Turn detection
# =========================================================================
@dataclass
class Turn:
    """One detected utterance, with the audio needed to transcribe it."""
    start_s: float
    end_s: float
    pcm: bytes
    peak_db: float = 0.0
    mean_db: float = 0.0
    floor_db: float = 0.0
    threshold_db: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def __repr__(self) -> str:
        return (f"Turn({self.start_s:.2f}-{self.end_s:.2f}s, {self.duration_s:.2f}s, "
                f"peak={self.peak_db:.1f}dB, {len(self.pcm)//2} samples)")


class VAD:
    """Streaming energy + zero-crossing turn detector with an adaptive floor.

    push(bytes) returns turns that closed on that chunk; flush() closes the
    last one. The object also keeps .floor_db / .threshold_db / .quiet_dbs so
    the caller can log what the detector believed about the room.
    """

    def __init__(self, margin_db: float = MARGIN_DB, end_silence_s: float = END_SILENCE_S,
                 min_turn_s: float = MIN_TURN_S, onset_s: float = ONSET_S,
                 preroll_s: float = PREROLL_S, max_turn_s: float = MAX_TURN_S,
                 reject_noise: bool = True, adapt: bool = True,
                 gate_db: float = GATE_DB, sustain_min: float = SUSTAIN_MIN):
        self.margin_db = margin_db
        self.end_silence_s = end_silence_s
        self.min_turn_s = min_turn_s
        self.onset_s = onset_s
        self.preroll_s = preroll_s
        self.max_turn_s = max_turn_s
        self.reject_noise = reject_noise
        self.adapt = adapt
        self.gate_db = gate_db
        self.sustain_min = sustain_min

        self._buf = bytearray()
        self._hist: deque = deque(maxlen=int(NOISE_WIN_S * BLOCKS_PER_S))
        self.floor_db = -45.0
        self.threshold_db = max(self.floor_db + margin_db, ABS_MIN_DB)
        self._n = 0

        self._in_turn = False
        self._run = 0
        self._preroll: deque = deque(maxlen=max(1, int(round(preroll_s * BLOCKS_PER_S))))
        self._turn_bytes = bytearray()
        self._turn_start_f = 0
        self._turn_frame0 = 0
        self._last_speech_f = 0
        self._turn_db: List[float] = []
        self._turn_flat: List[float] = []
        self.rejected = 0

        self.turns: List[Turn] = []
        self.quiet_dbs: List[float] = []

    # -- noise floor ------------------------------------------------------
    def _update_floor(self, db: float) -> None:
        self._hist.append(db)
        if not self.adapt:
            self.floor_db = -45.0
        elif self._n % 5 == 0 and len(self._hist) >= 20:
            p = float(np.percentile(np.fromiter(self._hist, np.float32), NOISE_PCT))
            p = min(max(p, FLOOR_MAX_DB), FLOOR_MIN_DB)
            if p > self.floor_db:
                self.floor_db += min(p - self.floor_db, 0.02)   # rise <= 2 dB/s
            else:
                self.floor_db += 0.25 * (p - self.floor_db)     # fall quickly
        self.threshold_db = max(self.floor_db + self.margin_db, ABS_MIN_DB)

    # -- main -------------------------------------------------------------
    def push(self, chunk: bytes) -> List[Turn]:
        self._buf.extend(chunk)
        out: List[Turn] = []
        while len(self._buf) >= BLOCK:
            frame = bytes(self._buf[:BLOCK])
            del self._buf[:BLOCK]
            self._n += 1
            x = pcm_to_float(frame)
            db = dbfs(float(np.sqrt(np.mean(x * x) + 1e-12)))
            self._update_floor(db)
            thr = self.threshold_db
            loud = db > thr
            if db < thr - 12:
                self.quiet_dbs.append(db)

            if not self._in_turn:
                self._preroll.append(frame)
                if loud:
                    self._run += 1
                    if self._run >= max(1, int(round(self.onset_s * BLOCKS_PER_S))):
                        self._open_turn()
                else:
                    self._run = 0
            else:
                self._turn_bytes.extend(frame)
                self._turn_db.append(db)
                self._turn_flat.append(_flatness(x))
                if loud:
                    self._last_speech_f = self._n
                silent_frames = self._n - self._last_speech_f
                too_long = (self._n - self._turn_start_f) / BLOCKS_PER_S > self.max_turn_s
                if silent_frames / BLOCKS_PER_S >= self.end_silence_s or too_long:
                    t = self._close_turn()
                    if t is not None:
                        out.append(t)
        return out

    def flush(self) -> List[Turn]:
        out = []
        if self._in_turn:
            t = self._close_turn()
            if t is not None:
                out.append(t)
        self._buf.clear()
        return out

    def _open_turn(self) -> None:
        self._in_turn = True
        self._turn_bytes = bytearray(b"".join(self._preroll))
        self._turn_start_f = self._n - len(self._preroll) + 1
        self._turn_frame0 = self._n + 1      # abs frame of _turn_db[0]
        self._last_speech_f = self._n
        self._turn_db, self._turn_flat = [], []
        self._run = 0
        self._preroll.clear()

    def _close_turn(self) -> Optional[Turn]:
        start_f, end_f = self._turn_start_f, self._last_speech_f + 1
        end_f = max(end_f, start_f + 1)
        keep = (end_f - start_f) * BLOCK          # drop trailing silence
        pcm = bytes(self._turn_bytes[:keep])
        start_s = (start_f - 1) / BLOCKS_PER_S
        end_s = (end_f - 1) / BLOCKS_PER_S
        dbs = np.asarray(self._turn_db, np.float32)
        flats = np.asarray(self._turn_flat, np.float32)

        # Tighten to the near-field core so background media is not sent to
        # Whisper: keep the part of the turn within 12 dB of its own peak, then
        # restore 0.25 s of lead-in so the first word survives.
        gate = self.floor_db + self.gate_db
        if dbs.size >= 10:
            off = self._turn_frame0 - start_f
            # runs shorter than 30 ms are discarded, so one stray frame cannot
            # stretch the turn
            tau = max(gate, float(dbs.max()) - 12.0)
            idx = np.where(dbs > tau)[0]
            if idx.size:
                edges = np.where(np.diff(idx) > 1)[0] + 1
                runs = np.split(idx, edges)
                runs = [r for r in runs if len(r) >= 3]
                if runs:
                    a = int(runs[0][0]) - 25
                    b = min(len(dbs), int(runs[-1][-1]) + 21)
                    lead = min(off, 25)      # keep 0.25 s of pre-roll context
                    if a <= 0:
                        a = -lead
                    if b > a:
                        p0 = a + off
                        start_f = start_f + p0
                        end_f = start_f + (b - a)
                        pcm = pcm[p0 * BLOCK:p0 * BLOCK + (b - a) * BLOCK]
                        dbs = dbs[max(a, 0):b]
                        flats = flats[max(a, 0):b]
        start_s = (start_f - 1) / BLOCKS_PER_S
        end_s = (end_f - 1) / BLOCKS_PER_S

        self._in_turn = False
        self._run = 0
        self._last_speech_f = 0
        self._turn_bytes = bytearray()
        self._turn_db, self._turn_flat = [], []

        dur = end_s - start_s
        if dur < self.min_turn_s or len(pcm) < int(self.min_turn_s * SR) * 2:
            self.rejected += 1
            return None
        flat_med = float(np.median(flats)) if flats.size else 1.0
        peak = float(dbs.max()) if dbs.size else 0.0
        sustain = self._sustain(dbs) if dbs.size else 0.0
        if self.reject_noise and flat_med > FLATNESS_MAX and peak < self.threshold_db + 6:
            self.rejected += 1
            return None
        if sustain < self.sustain_min:
            # loud, but not sustained near-field speech (media, bangs, laughter)
            self.rejected += 1
            return None

        t = Turn(start_s=round(start_s, 2), end_s=round(end_s, 2), pcm=pcm,
                 peak_db=round(peak, 1),
                 mean_db=round(float(dbs.mean()) if dbs.size else 0.0, 1),
                 floor_db=round(self.floor_db, 1),
                 threshold_db=round(self.threshold_db, 1),
                 meta={"frames": int(dbs.size), "flat_med": round(flat_med, 3),
                       "sustain": round(sustain, 3)})
        self.turns.append(t)
        return t

    def _sustain(self, dbs: np.ndarray) -> float:
        """Best coverage of a 1 s window by frames above floor + gate_db."""
        gate = self.floor_db + self.gate_db
        over = (dbs > gate).astype(np.float32)
        if over.size < SUSTAIN_WIN_F:
            return float(over.mean())
        c = np.convolve(over, np.ones(SUSTAIN_WIN_F, np.float32), "valid") / SUSTAIN_WIN_F
        return float(c.max())


def detect_turns(pcm_iter: Iterable[bytes], **kw) -> Iterator[Turn]:
    """Turn raw 16 kHz mono s16le byte chunks into completed Turn objects.

        for t in detect_turns(chunks):
            text = transcribe(save_wav(f"t{t.start_s}.wav", t.pcm)).text

    Chunks may be any size. Turns are yielded as soon as they close (0.9 s of
    trailing silence), so a caller can transcribe while the mic keeps streaming.
    Extra keyword arguments go to VAD (margin_db, end_silence_s, ...).
    """
    vad = VAD(**kw)
    for chunk in pcm_iter:
        if not chunk:
            continue
        yield from vad.push(chunk)
    yield from vad.flush()


# =========================================================================
# Wake-word detection
# =========================================================================
WAKE_PHRASES = ("hermes", "hey hermes", "hear me")

# Single-token fuzzy threshold. Measured on labelled data (see MEASUREMENTS.md):
#   "hear me" vs "hermes" = 0.769  (must pass)
#   "hermie"  vs "hermes" = 0.833, "hermi" = 0.727   (base-model mishearings)
#   "her miss" vs "hermes" = 0.714, "her music" = 0.667, "harm us" = 0.615
# 0.72 is the lowest cut that keeps "hear me"/"hermi"; 0.75 drops "her miss".
WAKE_RATIO = 0.72
_PUNCT = str.maketrans({c: " " for c in ".,!?;:\"'`()[]{}<>-–—…"})


def normalize_text(text: str) -> str:
    return " ".join(text.lower().translate(_PUNCT).split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(a=a, b=b).ratio()


def wake_match(text: str, threshold: float = WAKE_RATIO,
               max_offset: int = 2) -> Tuple[bool, float, str, int]:
    """Fuzzy wake test. Returns (hit, best_score, phrase, word_offset).

    Only the first `max_offset` words are considered: a wake word is how people
    start an utterance ("okay Hermes...", "hey Hermes..."). That placement rule
    stops "can you hear me?" said to another person from waking the agent, while
    still catching Whisper writing "Hear me set a timer" for "Hermes set a
    timer". Windows are compared with phrases of the same word count, so the
    two-word "her mess" cannot match the one-word "hermes".
    """
    words = normalize_text(text).split()
    if not words:
        return False, 0.0, "", -1
    phrases = [(p.split(), p) for p in WAKE_PHRASES]
    best = (False, 0.0, "", -1)
    for off in range(min(max_offset, len(words))):
        for pw, phrase in phrases:
            n = len(pw)
            if off + n > len(words):
                continue
            window = words[off:off + n]
            if window[0][:1] != pw[0][:1]:
                continue          # "there" must not pass as "hermes"
            # per-word ratios; a multi-word phrase is only as good as its
            # weakest word, so "hey her" does not pass as "hey hermes"
            score = min(_ratio(w, p) for w, p in zip(window, pw))
            if score > best[1]:
                best = (score >= threshold, score, phrase, off)
    return best


def is_wake(text: str, threshold: float = WAKE_RATIO) -> bool:
    """True when the turn is addressed to the agent.

    Tolerates Whisper's habitual mishearings of "Hermes" ("hear me", "hermie",
    "hermi"); "her miss" is rejected. Case and punctuation insensitive.

        is_wake("Hear me set a timer for 10 minutes.")   -> True
        is_wake("Can you hear me now?")                  -> False
    """
    return wake_match(text, threshold)[0]


# =========================================================================
# Transcription
# =========================================================================
@dataclass
class Transcript:
    text: str
    segments: List[dict] = field(default_factory=list)
    language: str = "en"
    language_probability: float = 0.0
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    model: str = "tiny"

    def __str__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return bool(self.text.strip())


_MODELS: Dict[Tuple[str, str], object] = {}


def _get_model(model_size: str = "tiny", compute_type: str = "int8"):
    key = (model_size, compute_type)
    if key not in _MODELS:
        from faster_whisper import WhisperModel
        _MODELS[key] = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    return _MODELS[key]


def transcribe(wav_path: str, model_size: str = "tiny", language: str = "en",
               vad_filter: bool = True, beam_size: int = 5) -> Transcript:
    """Transcribe a wav file with faster_whisper (CPU int8).

    Defaults to `tiny`: 0.70 s for a 5 s clip on this CPU (RTF 0.14), and it
    read every real command correctly; `base` costs 1.32 s (RTF 0.27) for the
    same text. vad_filter stays on because on non-speech bursts Whisper
    without it looped for 22 s on a 6 s clip.
    """
    t0 = time.perf_counter()
    model = _get_model(model_size)
    segs, info = model.transcribe(wav_path, language=language, beam_size=beam_size,
                                  vad_filter=vad_filter, condition_on_previous_text=False)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
           for s in segs]
    text = " ".join(s["text"] for s in out).strip()
    return Transcript(text=text, segments=out, language=info.language,
                      language_probability=round(info.language_probability, 3),
                      duration_s=round(info.duration, 2),
                      elapsed_s=round(time.perf_counter() - t0, 3), model=model_size)


# =========================================================================
# Speaker fingerprint
# =========================================================================
N_MFCC = 13
N_MELS = 26
FMIN, FMAX = 60.0, 7800.0


def _mel_filterbank(n_mels: int = N_MELS, n_fft: int = 512,
                    sr: int = SR, fmin: float = FMIN, fmax: float = FMAX) -> np.ndarray:
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    hz = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), np.float32)
    for i in range(n_mels):
        lo, mid, hi = bins[i], bins[i + 1], bins[i + 2]
        mid = max(mid, lo + 1)
        hi = max(hi, mid + 1)
        fb[i, lo:mid] = np.linspace(0, 1, mid - lo, endpoint=False)
        fb[i, mid:hi] = np.linspace(1, 0, hi - mid, endpoint=False)
    return fb


def _dct_matrix(n_out: int = N_MFCC, n_in: int = N_MELS) -> np.ndarray:
    k = np.arange(n_out)[:, None]
    n = np.arange(n_in)[None, :]
    m = np.cos(np.pi * k * (2 * n + 1) / (2.0 * n_in)) * np.sqrt(2.0 / n_in)
    m[0] /= np.sqrt(2.0)
    return m.astype(np.float32)


_FB = _mel_filterbank()
_DCT = _dct_matrix()


def mfcc(x: np.ndarray, sr: int = SR, win: int = 400, hop: int = 160,
         n_mfcc: int = N_MFCC) -> np.ndarray:
    """MFCC matrix (frames x n_mfcc) for 16 kHz float audio."""
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    n = 1 + (len(x) - win) // hop
    fr = x[np.arange(n)[:, None] * hop + np.arange(win)[None, :]]
    fr = np.append(fr[:, 0:1], fr[:, 1:] - 0.97 * fr[:, :-1], axis=1)  # pre-emphasis
    mag = np.abs(np.fft.rfft(fr * np.hamming(win), n=512, axis=1)) ** 2
    return np.log(mag @ _FB.T + 1e-10) @ _DCT[:n_mfcc].T


def f0_track(x: np.ndarray, sr: int = SR, win: int = 640, hop: int = 160,
             fmin: float = 70.0, fmax: float = 400.0,
             min_rms: float = 0.01, min_peak: float = 0.30) -> np.ndarray:
    """Autocorrelation pitch track (Hz) for voiced frames only."""
    lo, hi = int(sr / fmax), int(sr / fmin)
    out = []
    for i in range(0, max(0, len(x) - win), hop):
        fr = x[i:i + win]
        if fr.size < win or np.sqrt(np.mean(fr * fr)) < min_rms:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[win - 1:]
        ac = ac / (ac[0] + 1e-9)
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] > min_peak:
            out.append(sr / k)
    return np.asarray(out, np.float32)


PITCH_WEIGHT = 2.0      # weight of the pitch block vs the MFCC block
PITCH_REF_HZ = 150.0    # pitch features are centred here (log2) so the
                        # vector encodes relative, not absolute, pitch


def fingerprint_pcm(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """26-dim fingerprint: 24 MFCC statistics + 2 centred pitch statistics."""
    if sr != SR:
        x = _resample(x, sr, SR)
    db, zcr, _ = frame_features(x)
    if db.size == 0 or len(x) < 1600:
        return np.zeros(26, np.float32)
    keep = (db >= np.percentile(db, 30)) & (zcr < 0.30)
    if keep.sum() < 5:
        keep = db >= np.percentile(db, 70)
    c = mfcc(x)[:, 1:]                  # drop c0: it is loudness, not voice
    n = min(len(c), len(keep))
    c, keep = c[:n], keep[:n]
    c = c[keep]
    if c.shape[0] < 3:
        return np.zeros(26, np.float32)
    # cepstral mean normalisation removes channel/gain; the *shape* statistics
    # (spread and frame-to-frame change) are what identify a vocal tract
    cc = c - c.mean(axis=0, keepdims=True)
    v = np.concatenate([cc.std(axis=0), np.abs(np.diff(cc, axis=0)).mean(axis=0)])
    v = v.astype(np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)

    f = f0_track(x)
    if f.size >= 10:
        lf = np.log2(f)
        p = np.array([(np.median(lf) - np.log2(PITCH_REF_HZ)) / 0.5,
                      (np.percentile(lf, 75) - np.percentile(lf, 25)) / 0.5],
                     np.float32) * PITCH_WEIGHT
    else:
        p = np.zeros(2, np.float32)
    v = np.concatenate([v, p])
    return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)


def fingerprint(wav_path) -> np.ndarray:
    """26-dim voice fingerprint for a wav path (or a float array).

    = 24 MFCC statistics (cepstral-mean-normalised spread + frame deltas,
    over the louder 70 % of frames) + 2 pitch statistics (median F0 and F0
    IQR, centred on 150 Hz, weighted 2x). L2-normalised; compare with
    fingerprint_distance().

    Measured separation on this machine (see MEASUREMENTS.md): same speaker
    0.004-0.07, different speaker 0.6-1.4. Works from ~1 s of speech; longer
    clips tighten the spread. Routing several people to one agent is only as
    good as the pitch separation between them - two household members with
    near-identical pitch are not reliably separated by this.
    """
    x, sr = ((read_wav(wav_path) if isinstance(wav_path, str)
              else (np.asarray(wav_path, np.float32), SR)))
    return fingerprint_pcm(x, sr)


def fingerprint_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2]; measured same-speaker < 0.07, different > 0.6."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 2.0
    return float(1.0 - np.dot(a, b) / (na * nb))


def identify(fp: np.ndarray, enrolled: Dict[str, np.ndarray],
             max_distance: float = 0.25) -> Tuple[Optional[str], float]:
    """Nearest enrolled speaker, or (None, best_distance) when nobody is close.

    Enrol with fingerprint() of a few seconds of speech per person and average
    (or keep the first); 0.25 sits between the measured same- and
    different-speaker distances.
    """
    best, bd = None, 2.0
    for name, ref in enrolled.items():
        d = fingerprint_distance(fp, ref)
        if d < bd:
            best, bd = name, d
    if bd > max_distance:
        return None, bd
    return best, bd
