"""Offline text-to-speech for the Echo Dot speaker.

`synthesise(text, voice=None, rate=None) -> bytes`
    returns exactly MONO 48 kHz signed-16-bit little-endian PCM, ready to stream
    to device/cmd (const speakerRate = 48000).

Backends, in order of quality:
  1. piper  - neural TTS (piper-tts + en_US voices, all local files, no keys).
              Model runs in .venv-tts via a warm daemon so voices stay loaded.
  2. sapi   - Windows System.Speech fallback (Microsoft David/Zira). Always
              present, but it is a ~16 kHz band-limited formant synth.

CLI:
    python tts.py "text" out.wav [--voice lessac-high] [--rate 0]
    python tts.py --voices
    python tts.py --selftest

The module itself imports only the standard library, so it runs under the
controller's interpreter; the neural model runs in the venv subprocess.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave

SR = 48000
CHANNELS = 1
SAMPWIDTH = 2

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_PY = os.path.join(_HERE, ".venv-tts", "Scripts", "python.exe")
_WORKER = os.path.join(_HERE, "_tts_worker.py")

# lessac-high synthesises at ~0.95x realtime on this CPU (5.42s for
# 5.7s of audio), which is dead air before every answer. medium is
# 2.16s for the same sentence and sounds the same through a Dot.
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "lessac-medium")

# name -> rough character, used for --voices and for looking things up
VOICE_NAMES = ("lessac-high", "lessac-medium", "lessac", "amy", "ryan")

_lock = threading.RLock()
_proc = None
_cache: dict[tuple, bytes] = {}
_CACHE_MAX = 32


# --------------------------------------------------------------------------- #
# piper backend (warm daemon)
# --------------------------------------------------------------------------- #
class _Unavailable(RuntimeError):
    pass


def _piper_ready() -> bool:
    return os.path.exists(_VENV_PY) and os.path.exists(_WORKER)


def _spawn():
    global _proc
    kwargs = {}
    if os.name == "nt":
        # keep the console clean for the parent controller; no new window
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    _proc = subprocess.Popen(
        [_VENV_PY, _WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=_HERE, **kwargs,
    )
    return _proc


def _kill():
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
        except Exception:
            pass
        _proc = None


def _request(text: str, voice: str, rate: int) -> bytes:
    """Send one request to the daemon, return raw PCM bytes."""
    global _proc
    for attempt in (1, 2):
        if _proc is None or _proc.poll() is not None:
            _spawn()
        p = _proc
        payload = json.dumps({"text": text, "voice": voice, "rate": rate}).encode() + b"\n"
        try:
            p.stdin.write(payload)
            p.stdin.flush()
            line = p.stdout.readline()
            if not line:
                raise _Unavailable("worker closed stdout")
            hdr = json.loads(line.decode("utf-8", "replace"))
            if not hdr.get("ok"):
                raise _Unavailable(hdr.get("err", "worker error"))
            n = int(hdr["n"])
            buf = bytearray()
            while len(buf) < n:
                chunk = p.stdout.read(n - len(buf))
                if not chunk:
                    raise _Unavailable("short read from worker")
                buf.extend(chunk)
            return bytes(buf)
        except Exception:
            _kill()
            if attempt == 2:
                raise
    raise _Unavailable("unreachable")


# --------------------------------------------------------------------------- #
# SAPI fallback
# --------------------------------------------------------------------------- #
_SAPI_SCRIPT = r"""
param([string]$Text, [string]$OutFile, [string]$VoiceName, [int]$Rate)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($VoiceName) { $s.SelectVoice($VoiceName) }
} catch { }
$s.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
  48000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($OutFile, $fmt)
$s.Speak($Text)
$s.Dispose()
"""


def _powershell() -> str | None:
    for c in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        p = shutil.which(c)
        if p:
            return p
    return None


def _sapi(text: str, rate: int, voice: str | None) -> bytes:
    ps = _powershell()
    if not ps:
        raise RuntimeError("no TTS backend available (piper missing, PowerShell missing)")
    tmp = tempfile.mkdtemp(prefix="emos_tts_")
    wav = os.path.join(tmp, "o.wav")
    scr = os.path.join(tmp, "s.ps1")
    with open(scr, "w", encoding="utf-8") as f:
        f.write(_SAPI_SCRIPT)
    try:
        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", scr,
               "-Text", text, "-OutFile", wav, "-Rate", str(int(rate))]
        if voice:
            cmd += ["-VoiceName", voice]
        r = subprocess.run(cmd, capture_output=True, timeout=90,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0 or not os.path.exists(wav):
            raise RuntimeError("SAPI failed: " + r.stderr.decode("utf-8", "replace")[:400])
        with wave.open(wav, "rb") as w:
            if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise RuntimeError("SAPI produced unexpected format")
            raw = w.readframes(w.getnframes())
        return _limit_peak(raw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _limit_peak(pcm: bytes, peak_dbfs: float = -1.0) -> bytes:
    """Scale down to `peak_dbfs` full scale if hotter. Cheap, stdlib only."""
    import array
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return b""
    peak = max(max(a), -min(a))
    target = 32767.0 * 10 ** (peak_dbfs / 20.0)
    if peak > target:
        k = target / peak
        for i, v in enumerate(a):
            a[i] = int(v * k)
    return a.tobytes()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def synthesise(text: str, voice: str | None = None, rate: int | None = None,
               backend: str | None = None) -> bytes:
    """Synthesise `text` -> MONO 48 kHz s16le PCM bytes.

    voice : piper voice name ('lessac-high', 'lessac-medium', 'amy', 'ryan') or,
            for the sapi backend, a Windows voice name ('Microsoft Zira Desktop').
    rate  : -10 (slowest) .. +10 (fastest), 0 = the tuned default.
    backend: force 'piper' or 'sapi'; default = best available.
    """
    if text is None:
        return b""
    text = " ".join(str(text).split())
    if not text:
        return b""
    r = 0 if rate is None else int(rate)
    v = voice or DEFAULT_VOICE

    key = (text, v, r, backend)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit

    want_piper = backend in (None, "piper") and _piper_ready()
    if want_piper:
        with _lock:
            try:
                pcm = _request(text, v, r)
                if pcm:
                    _remember(key, pcm)
                    return pcm
            except Exception:
                if backend == "piper":
                    raise
                _kill()
    with _lock:
        pcm = _sapi(text, r, v if backend == "sapi" else None)
        _remember(key, pcm)
        return pcm


def _remember(key, pcm: bytes) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = pcm


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPWIDTH)
        w.setframerate(SR)
        w.writeframes(pcm)


def close() -> None:
    """Shut the daemon down (optional; it also exits with the parent)."""
    with _lock:
        if _proc is not None:
            try:
                _proc.stdin.write(b'{"cmd":"quit"}\n')
                _proc.stdin.flush()
                _proc.wait(timeout=3)
            except Exception:
                pass
        _kill()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _stats(pcm: bytes) -> dict:
    import array
    import math
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(a)
    if not n:
        return {"samples": 0}
    sq = sum(v * v for v in a) / n
    rms = math.sqrt(sq)
    peak = max(abs(v) for v in a)
    return {
        "samples": n,
        "seconds": round(n / SR, 3),
        "rms": round(rms, 1),
        "rms_dbfs": round(20 * math.log10(max(rms, 1) / 32768), 1),
        "peak": peak,
        "peak_dbfs": round(20 * math.log10(max(peak, 1) / 32768), 2),
        "clipped": sum(1 for v in a if abs(v) >= 32767),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    voice = None
    rate = None
    backend = None
    if "--voices" in args:
        print("piper voices (default %s):" % DEFAULT_VOICE)
        for v in VOICE_NAMES:
            print("  ", v)
        print("sapi fallback voices: Microsoft David Desktop, Microsoft Zira Desktop")
        return 0
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--voice" and i + 1 < len(args):
            voice = args[i + 1]; i += 2
        elif args[i] == "--rate" and i + 1 < len(args):
            rate = int(args[i + 1]); i += 2
        elif args[i] == "--backend" and i + 1 < len(args):
            backend = args[i + 1]; i += 2
        else:
            rest.append(args[i]); i += 1

    if "--selftest" in args or not rest:
        text = "Good morning. The weather today is clear and cold, with a high of forty-one degrees."
        out = rest[1] if len(rest) > 1 else "tts_selftest.wav"
    else:
        text = rest[0]
        out = rest[1] if len(rest) > 1 else "out.wav"

    import time
    t0 = time.time()
    pcm = synthesise(text, voice=voice, rate=rate, backend=backend)
    dt = time.time() - t0
    write_wav(out, pcm)
    with wave.open(out, "rb") as w:
        meta = dict(sr=w.getframerate(), ch=w.getnchannels(),
                    sw=w.getsampwidth(), frames=w.getnframes())
    st = _stats(pcm)
    print(f"wrote {out}")
    print(f"  format : {meta['sr']} Hz, {meta['ch']} channel(s), "
          f"{meta['sw'] * 8}-bit, {meta['frames']} frames")
    print(f"  duration: {st['seconds']} s   rms {st['rms']} ({st['rms_dbfs']} dBFS)   "
          f"peak {st['peak']} ({st['peak_dbfs']} dBFS)   clipped {st['clipped']}")
    print(f"  synth : {dt:.2f} s wall")
    close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
