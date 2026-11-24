"""Objective benchmark for TTS candidates. Measures only what is measurable."""
import io, time, wave, sys, json
import numpy as np

TESTS = [
    "Good morning. The weather today is clear and cold, with a high of forty-one degrees.",
    "I found three messages in your inbox: two from Sarah, and one marked urgent.",
    "Done. Your reminder is set for seven thirty tomorrow morning.",
]

def metrics(pcm: np.ndarray, sr: int, text: str, synth_s: float) -> dict:
    x = pcm.astype(np.float64)
    dur = len(x) / sr
    nchars = len(text)
    nwords = len(text.split())
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = int(np.max(np.abs(x)))
    clip = int(np.sum(np.abs(x) >= 32767))
    # silent fraction (frames below -50 dBFS in 20ms windows)
    w = max(1, int(0.02 * sr))
    n = len(x) // w
    if n:
        fr = np.sqrt(np.mean(x[:n * w].reshape(n, w) ** 2, axis=1))
        sil = float(np.mean(fr < 32768 * 10 ** (-50 / 20)))
    else:
        sil = 0.0
    return dict(dur=round(dur, 3), chars_s=round(nchars / dur, 2),
                words_s=round(nwords / dur, 2), rms=int(rms), peak=peak,
                rms_dbfs=round(20 * np.log10(max(rms, 1) / 32768), 1),
                peak_dbfs=round(20 * np.log10(max(peak, 1) / 32768), 2),
                clip_samples=clip, silence_frac=round(sil, 3),
                synth_s=round(synth_s, 2), rtf=round(synth_s / dur, 2))

def report(name, pcm, sr, synth_s):
    out = [f"\n== {name} == sr={sr} n={len(pcm)}"]
    for t in TESTS:
        pass
    return out

def bench_piper(model):
    from piper import PiperVoice
    t0 = time.time()
    v = PiperVoice.load(model)
    load_s = time.time() - t0
    rows = []
    for text in TESTS:
        buf = io.BytesIO()
        w = wave.open(buf, "wb")
        t0 = time.time()
        v.synthesize_wav(text, w)
        dt = time.time() - t0
        w.close()
        buf.seek(0)
        r = wave.open(buf, "rb")
        sr, nch, sw, nf = r.getframerate(), r.getnchannels(), r.getsampwidth(), r.getnframes()
        pcm = np.frombuffer(r.readframes(nf), dtype="<i2").astype(np.int16)
        m = metrics(pcm, sr, text, dt)
        m["sr"] = sr; m["ch"] = nch; m["sw"] = sw
        rows.append(m)
    print(f"\n### PIPER {model}  (load {load_s:.2f}s)")
    for m in rows:
        print(f"  dur={m['dur']:>6}s chars/s={m['chars_s']:>5} rms={m['rms']:>5} "
              f"({m['rms_dbfs']:>6} dBFS) peak={m['peak']:>5} ({m['peak_dbfs']:>5}) "
              f"clip={m['clip_samples']:<4} sil={m['silence_frac']:<5} rtf={m['rtf']} sr={m['sr']}")
    return rows

if __name__ == "__main__":
    for m in sys.argv[1:]:
        bench_piper(m)
