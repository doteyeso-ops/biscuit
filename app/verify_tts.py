"""Independent verification of tts.py output. Asserts the contract, prints measurements.

Run:  python verify_tts.py            (system python; no third-party deps)
      python verify_tts.py --all      (also checks every voice + rate extremes)
"""
from __future__ import annotations

import array
import math
import os
import sys
import tempfile
import wave

import tts

SR = 48000
SENTENCES = [
    "Good morning. The weather today is clear and cold, with a high of forty-one degrees.",
    "I found three messages in your inbox: two from Sarah, and one marked urgent.",
    "Done. Your reminder is set for seven thirty tomorrow morning.",
]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print("  FAIL:", msg)


def measure(pcm: bytes, text: str) -> dict:
    a = array.array("h")
    a.frombytes(pcm)
    n = len(a)
    dur = n / SR
    rms = math.sqrt(sum(v * v for v in a) / n)
    peak = max(abs(v) for v in a)
    # 10 ms frames for voiced-time and pause accounting
    win = SR // 100
    frames = n // win
    thr = 32768 * 10 ** (-50 / 20)
    voiced = 0
    for i in range(frames):
        seg = a[i * win:(i + 1) * win]
        if math.sqrt(sum(v * v for v in seg) / win) > thr:
            voiced += 1
    vdur = voiced / 100.0
    return dict(samples=n, dur=dur, rms=rms, peak=peak,
                rms_dbfs=20 * math.log10(max(rms, 1) / 32768),
                peak_dbfs=20 * math.log10(max(peak, 1) / 32768),
                clipped=sum(1 for v in a if abs(v) >= 32767),
                articulation=len(text) / vdur if vdur else 0.0,
                silence=1 - (vdur / dur if dur else 0))


def check_wav(path: str) -> dict:
    with wave.open(path, "rb") as w:
        return dict(sr=w.getframerate(), ch=w.getnchannels(),
                    sw=w.getsampwidth(), nf=w.getnframes())


def main() -> int:
    deep = "--all" in sys.argv
    tmp = tempfile.mkdtemp(prefix="tts_verify_")

    combos = [("lessac-high", 0)]
    if deep:
        combos = [("lessac-high", r) for r in (-10, -4, 0, 4, 10)]
        combos += [(v, 0) for v in ("lessac-medium", "amy", "ryan")]
        combos += [("lessac-high", None)]

    print(f"{'voice':14} {'rate':>5} {'chars/s':>7} {'dur':>6} {'rms':>7} {'peak':>7} {'clip':>4}")
    for voice, rate in combos:
        for idx, text in enumerate(SENTENCES):
            pcm = tts.synthesise(text, voice=voice, rate=rate)
            path = os.path.join(tmp, f"{voice}_{rate}_{idx}.wav")
            tts.write_wav(path, pcm)
            m = measure(pcm, text)
            w = check_wav(path)

            check(w["sr"] == SR, f"{voice} r{rate} wav sr {w['sr']} != 48000")
            check(w["ch"] == 1, f"{voice} r{rate} wav channels {w['ch']} != 1")
            check(w["sw"] == 2, f"{voice} r{rate} wav sampwidth {w['sw']} != 2")
            check(len(pcm) % 2 == 0, f"{voice} r{rate} odd byte count")
            check(len(pcm) == w["nf"] * 2, f"{voice} r{rate} payload != frames*2")
            check(m["clipped"] == 0, f"{voice} r{rate} has {m['clipped']} clipped samples")
            check(m["peak_dbfs"] <= -0.5, f"{voice} r{rate} peak {m['peak_dbfs']} dBFS too hot")
            check(m["rms_dbfs"] > -24, f"{voice} r{rate} rms {m['rms_dbfs']} too quiet")
            check(m["dur"] > 0.4, f"{voice} r{rate} suspiciously short audio")
            if idx == 0:
                print(f"{voice:14} {str(rate):>5} {m['articulation']:>7.1f} {m['dur']:>6.2f} "
                      f"{m['rms_dbfs']:>6.1f}dB {m['peak_dbfs']:>6.1f}dB {m['clipped']:>4}")

    # empty / whitespace handling must not explode
    check(tts.synthesise("") == b"", "empty string should return b''")
    check(tts.synthesise("   \n\t ") == b"", "whitespace should return b''")

    # SAPI fallback must also satisfy the contract
    if "--sapi" in sys.argv or deep:
        pcm = tts.synthesise("Fallback check.", backend="sapi")
        m = measure(pcm, "Fallback check.")
        check(len(pcm) % 2 == 0 and m["peak"] < 32767, "sapi fallback contract")
        print(f"{'sapi fallback':14} {'-':>5} {m['articulation']:>7.1f} {m['dur']:>6.2f} "
              f"{m['rms_dbfs']:>6.1f}dB {m['peak_dbfs']:>6.1f}dB {m['clipped']:>4}")

    tts.close()
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
