"""Hermes controller for an Echo Dot running emOS (aiohttp version).

Serves HTTP probes with 200 (the device's probeTCP expects an answer) and
upgrades real WebSocket clients on /control, /data and /shell/{id}.
Advertises _emcontroller._tcp.local over mDNS exactly as the EchoMuse
controller does: version=1, server=<name>, port 8767, no tls_port.
"""
import asyncio
import hmac
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

from aiohttp import web, WSMsgType
from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo, IPVersion

PORT = int(os.environ.get("DOT_PORT", "8767"))
MDNS_NAME = "hermes"
DEVICE_WS = None
SIM = ("--simulate" in sys.argv) or os.environ.get("DOT_SIMULATE") == "1"
DATA_WS = None
SHELL_WS = None
SHELL_READY = asyncio.Event()
AUDIO_Q = asyncio.Queue()

# A bounded turn: the device opens the mic only when asked (mic_start with
# lock_mic:true), does its own VAD, and ends the turn with 0x04 (spoke) or
# 0x05 (no speech). Capturing on the host instead is what made every press
# record silence: the mic was never opened.
TURN = {"active": False, "buf": bytearray(), "end": None, "done": None}


def turn_event():
    if TURN["done"] is None:
        TURN["done"] = asyncio.Event()
    return TURN["done"]


async def ctl(obj):
    """Send one control message to the device."""
    if DEVICE_WS is None:
        raise RuntimeError("no control plane")
    await DEVICE_WS.send_str(json.dumps(obj))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
try:
    import tts as hts
    import ring as hring
    import convo
    HAVE_APP = True
except Exception as _e:      # the modules are optional; the controller still runs
    HAVE_APP = False
    log_import = _e

try:
    import quick                 # snippets + a model, when Google has no answer
    HAVE_QUICK = True
except Exception:
    HAVE_QUICK = False

try:
    import dashboard as hdash      # settings, history and the /ui control page
    HAVE_DASH = True
except Exception:
    HAVE_DASH = False

try:
    import webanswer as hweb     # Google's own AI Overview via the user's Brave
    HAVE_WEB = True
except Exception:
    HAVE_WEB = False

# Router: the free lane answers by default (measured 3-5 s vs 18-23 s for the
# agent CLI). Anything that needs a tool, this house, this laptop or the local
# clock goes to the agent instead. Inverted from "opt in to fast": more
# questions are general knowledge than are actions.
_AGENT_WORDS = ("remind", "timer", "alarm", "calendar", "schedule", "start",
                "stop", "restart", "shut", "open", "delete", "send", "email",
                "file", "code", "build", "check", "run ", "play", "pause",
                "volume", "light", "house", "laptop", "hermes", "you ",
                "your ", "what time", "the time", "what day", "the date",
                "today", "tomorrow", "yesterday",
                "my ")   # anything personal needs the agent's memory


# "play", "put on", "i want to hear" -- but not "what songs did Michael make"
_PLAY_ANY = re.compile(
    r"\b(?:play|put on|queue up|start)\b\s+(?:me\s+|us\s+)?(?:some\s+|a\s+|an\s+)?(.+)$")
_HEAR_ANY = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"
    r"(?:i want to hear|i wanna hear|let's hear|i'd like to hear|listen to|"
    r"play me)\s+(.+)$")
_PLAY_STRIP = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+|i want to hear\s+|"
    r"i wanna hear\s+|let'?s hear\s+|i'?d like\s+)")


def music_query(text):
    """Return the song to play, or "" if this is not a play request.

    Only the imperative forms count: "play X", "put on X", "i want to hear X".
    A question about a song ("what album is X from") must not match, or the Dot
    describes the song instead of playing it.
    """
    t = _PLAY_STRIP.sub("", (text or "").lower().strip())
    if t.startswith(("what", "who", "when", "why", "how", "which", "is ", "are ",
                     "tell me about", "describe")):
        return ""
    # "I want to hear X" has no play keyword left after stripping; take X.
    hear = _HEAR_ANY.search((text or "").lower().strip())
    if hear:
        return hear.group(1).strip(" .!?")
    m = _PLAY_ANY.search(t)
    if not m:
        return ""
    q = m.group(1).strip(" .!?")
    q = re.sub(r"^(?:the song|the track|music by)\s+", "", q)
    if len(q) < 2:
        return ""
    return q


_TRIVIAL = {"", ".", "ok", "okay", "k", "kk", "mm", "hmm", "uh huh", "yeah",
            "yep", "yes", "no", "nope", "sure", "thanks", "thank you", "ty",
            "cool", "nice", "got it", "right", "alright", "bye", "goodbye",
            "see ya", "never mind", "nevermind", "no thanks", "that's all",
            "nothing", "huh"}


def is_trivial(text):
    return (text or "").strip(" .!?,").lower() in _TRIVIAL


def needs_agent(text):
    s = (text or "").lower().strip()
    if not s:
        return True
    return any(w in s for w in _AGENT_WORDS)


def ring_send(state, **opts):
    """Best-effort ring state; never blocks or raises into the audio path."""
    if not HAVE_APP or DEVICE_WS is None:
        return
    try:
        payload = hring.render(state, **opts)
    except Exception as e:
        log("ring render failed:", e)
        return
    asyncio.ensure_future(DEVICE_WS.send_str(json.dumps(payload)))


async def shell_run(cmd, settle=0.5):
    """Run a command on the device's root shell plane (opened on demand)."""
    if DEVICE_WS is None:
        return "no device"
    SHELL_READY.clear()
    try:
        await DEVICE_WS.send_str(json.dumps({"type": "shell_open", "pty": False}))
    except Exception as e:
        return f"shell_open failed: {e}"
    try:
        await asyncio.wait_for(SHELL_READY.wait(), 3.0)
    except asyncio.TimeoutError:
        return "shell never dialled"
    try:
        await SHELL_WS.send_bytes(cmd.encode() + b"\n")
        await asyncio.sleep(settle)
    except Exception as e:
        return f"shell write failed: {e}"
    finally:
        try:
            await DEVICE_WS.send_str(json.dumps({"type": "shell_close"}))
        except Exception:
            pass
    return "ok"


async def amp_on():
    """The Dot plays into a muted amp unless this runs before each utterance.

    Ext_Amp_Gain defaults to 6dB and the mixer to unity; the board supports
    12/18/24 dB, and the device's own music guard already pulls 21-32 dB out of
    everything it is fed, so the analogue stage is the only place loudness can
    actually come from.
    """
    if SIM:
        return
    return await shell_run(
        'tinymix -D 0 "Ext_Speaker_Amp_Switch" On; '
        'tinymix -D 0 "PCM Playback Volume" 127 127; '
        'tinymix -D 0 "Ext_Amp_Gain" 12dB; '
        'tinymix -D 0 "Audio Mrgrx Volume" 98304', settle=0.5)


_SENT = re.compile(r"(?<=[.!?])\s+")

MUSIC = {"task": None, "stop": None, "title": "", "query": "", "gen": 0}
TURN_TASK = {"task": None}
PLAYBACK = {"periods": 0, "bytes": 0}
UI_TOKEN = os.environ.get("DOT_UI_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# Ring visualiser
#
# The ring is 12 LEDs and the music arrives as 42.67 ms mono periods, so the
# cheapest honest visualisation is a three-band meter: bass on 0-3, mids on 4-7,
# treble on 8-11, all scaled by the period's level. Sent at ~7 Hz, not per
# period: the control plane also carries button events, and flooding it starves
# nothing but makes the ring look nervous.
# ---------------------------------------------------------------------------
VIS = {"on": False, "frame": 0, "peak": 1.0, "sent": 0,
       "pk": {"bass": 0.2, "mid": 0.2, "treble": 0.2}}

# Lighting: presets decide the shape, `hue` rotates the whole palette so the
# wheel in the dashboard and the preset chips are the same mechanism.
LIGHT = {"enabled": True, "preset": "equalizer", "hue": 200, "brightness": 1.0}

PRESETS = {
    # name: (mode, base hues as degrees for the three bands)
    "equalizer": ("zones", (28, 135, 192)),
    "mono":      ("zones", (0, 0, 0)),
    "ocean":     ("zones", (196, 176, 158)),
    "sunset":    ("zones", (8, 32, 350)),
    "forest":    ("zones", (96, 130, 160)),
    "rainbow":   ("rainbow", (0, 120, 240)),
    "pulse":     ("pulse", (0, 0, 0)),
}


SETTINGS = {"title": "Biscuit", "llm_url": "", "llm_model": "", "llm_key": "",
            "google": True, "voice": ""}


def load_settings():
    """Settings the user can change from the dashboard, not just at launch."""
    global SETTINGS
    if not HAVE_DASH:
        return
    try:
        cfg = hdash.load_config()
        st = cfg.get("settings") or {}
        SETTINGS.update({k: v for k, v in st.items() if v not in (None, "")})
        log("settings:", {k: ("***" if k == "llm_key" and v else v) for k, v in SETTINGS.items()})
    except Exception as e:
        log("settings load failed:", e)


def load_lighting():
    """Pick up the saved palette at startup so a restart keeps the look."""
    try:
        cfg = hdash.load_config().get("lighting") or {}
        for k in ("enabled", "preset", "hue", "brightness"):
            if k in cfg:
                LIGHT[k] = cfg[k]
        if LIGHT["preset"] not in PRESETS:
            LIGHT["preset"] = "equalizer"
        log(f"lighting: {LIGHT['preset']} hue {LIGHT['hue']} "
            f"brightness {LIGHT['brightness']} enabled {LIGHT['enabled']}")
    except Exception as e:
        log("lighting: could not load config:", e)


def _hsv_rgb(h, sat=1.0, val=1.0):
    """HSV -> (r, g, b) 0-255. Small enough to inline rather than import."""
    h = h % 360
    c = val * sat
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = val - c
    if h < 60:    r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:         r, g, b = c, 0, x
    return (int((r + m) * 255), int((g + m) * 255), int((b + m) * 255))


def _zone_colors():
    """Three (r,g,b) for bass/mid/treble, from the preset plus the hue wheel."""
    mode, hues = PRESETS.get(LIGHT["preset"], PRESETS["equalizer"])
    if LIGHT["preset"] == "mono":
        return [(255, 255, 255)] * 3, mode
    # The wheel rotates the palette around a fixed neutral, so preset colours stay
    # recognisable at the default hue and shift together as the wheel turns.
    shift = int(LIGHT["hue"]) - 200
    if LIGHT["preset"] in ("rainbow", "pulse"):
        base = int(LIGHT["hue"])
        hues = (base, base + 120, base + 240)
    return [_hsv_rgb(h + shift) for h in hues], mode


def _band_levels(chunk):
    """(bass, mid, treble, level) normalised 0..1 from one mono s16 period.

    A real FFT split, because the cheap version could not tell 60 Hz from 1 kHz
    (both landed in "mid"), which makes a ring that flickers rather than moves
    with the music. numpy is used when present; the arithmetic fallback keeps
    the visualiser alive on a machine without it.
    """
    usable = len(chunk) // 2 * 2
    if usable < 256:
        return 0.0, 0.0, 0.0, 0.0
    try:
        import numpy as np
        a = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float32) / 32768.0
        level = float(np.abs(a).mean())
        spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) / len(a)
        freqs = np.fft.rfftfreq(len(a), 1.0 / 48000.0)
        bands = []
        for lo, hi in ((20, 250), (250, 4000), (4000, 16000)):
            sel = spec[(freqs >= lo) & (freqs < hi)]
            bands.append(float(np.sqrt((sel ** 2).mean())) if sel.size else 0.0)
        bass, mid, treb = bands
        # scale so a loud period reaches ~1.0 in its band
        return (min(1.0, bass * 22), min(1.0, mid * 30),
                min(1.0, treb * 55), min(1.0, level * 3.6))
    except Exception:
        import array
        a = array.array("h")
        a.frombytes(chunk[:usable])
        n = len(a)
        bass = sum(abs(a[i]) for i in range(0, n, 8)) / max(1, len(range(0, n, 8)))
        mid = sum(abs(a[i]) for i in range(0, n, 2)) / max(1, len(range(0, n, 2)))
        treb = sum(abs(a[i] - a[i - 1]) for i in range(1, n, 2)) / max(1, (n - 1) // 2)
        level = sum(abs(x) for x in a) / n
        sc = 32768.0
        return (min(1.0, bass / sc * 2.2), min(1.0, mid / sc * 3.2),
                min(1.0, treb / sc * 1.6), min(1.0, level / sc * 3.0))


async def _visualise(chunk):
    """Push one ring frame per ~140 ms of audio."""
    if not VIS["on"] or not LIGHT["enabled"] or DEVICE_WS is None:
        return
    VIS["frame"] += 1
    if VIS["frame"] % 3:          # 42.67 ms * 3 ~= 128 ms
        return
    bass, mid, treb, level = _band_levels(chunk)
    VIS["peak"] = max(VIS["peak"] * 0.985, level, 0.08)   # slow decay
    norm = min(1.0, level / VIS["peak"])
    # each band against its own rolling peak, so quiet bands still move
    for name, val in (("bass", bass), ("mid", mid), ("treble", treb)):
        VIS["pk"][name] = max(VIS["pk"][name] * 0.96, val, 0.05)
    colors, mode = _zone_colors()
    bright = max(0.0, min(1.0, float(LIGHT["brightness"])))
    if mode == "pulse":
        # one colour, whole ring breathing with the loudest band
        lvl = max(bass, mid, treb)
        k = (0.25 + 0.75 * min(1.0, lvl / (VIS["pk"]["mid"] or 1))) * bright
        r, g, b = colors[0]
        leds = [{"id": i, "r": int(r * k), "g": int(g * k), "b": int(b * k)}
                for i in range(12)]
        try:
            await DEVICE_WS.send_str(json.dumps(
                {"type": "leds", "leds": leds, "listening": False}))
            VIS["sent"] += 1
            if VIS["sent"] == 1:
                log("ring: first visualiser frame sent")
        except Exception:
            VIS["on"] = False
        return
    if mode == "rainbow":
        # hue rotates around the ring, brightness follows the band under it
        leds = []
        for i in range(12):
            band = bass if i < 4 else (mid if i < 8 else treb)
            name = "bass" if i < 4 else ("mid" if i < 8 else "treble")
            amt = min(1.0, band / (VIS["pk"][name] or 1))
            r, g, b = _hsv_rgb(int(LIGHT["hue"]) + i * 30)
            k = (0.2 + 0.8 * amt) * bright
            leds.append({"id": i, "r": int(r * k), "g": int(g * k), "b": int(b * k)})
        try:
            await DEVICE_WS.send_str(json.dumps(
                {"type": "leds", "leds": leds, "listening": False}))
            VIS["sent"] += 1
            if VIS["sent"] == 1:
                log("ring: first visualiser frame sent")
        except Exception:
            VIS["on"] = False
        return
    zones = ((0, 3, bass, "bass", colors[0]),
             (4, 7, mid, "mid", colors[1]),
             (8, 11, treb, "treble", colors[2]))
    leds = []
    for lo, hi, band, name, (r, g, b) in zones:
        amt = max(0.0, min(1.0, band / VIS["pk"][name] if VIS["pk"][name] else 0))
        amt = (amt * 0.8 + norm * 0.2)
        for i in range(lo, hi + 1):
            # a little variation inside the zone so it reads as motion
            k = amt * (0.65 + 0.35 * ((i % 2) ^ 1)) * bright
            leds.append({"id": i, "r": int(r * k), "g": int(g * k), "b": int(b * k)})
    try:
        await DEVICE_WS.send_str(json.dumps(
            {"type": "leds", "leds": leds, "listening": False}))
        VIS["sent"] += 1
        if VIS["sent"] == 1:
            log("ring: first visualiser frame sent")
    except Exception:
        VIS["on"] = False

VOLUME = {"level": 60}


async def play_music(query, ws=None, gain_db=None, stale=None):
    """Stream a song through the Dot. Audio-only extraction, so there is no ad
    break in the stream to skip."""
    import music as hmu
    if ws is None:
        ws = await live_data_ws()
    if ws is None:
        raise RuntimeError("no data plane")
    try:
        title, url = await asyncio.to_thread(hmu.resolve, query)
    except Exception as e:
        log("music resolve failed:", e)
        raise
    if stale is not None and stale():
        log(f"music: dropped stale request ({query!r}) -- it was interrupted while resolving")
        return 0
    # Flush *here*, after the resolve and after any chime, so the plane is clean
    # immediately before the first frame rather than seconds earlier.
    try:
        await ctl({"type": "speaker_flush"})
        await ctl({"type": "music_flush"})
    except Exception:
        pass
    await asyncio.sleep(0.2)
    MUSIC["title"] = title
    MUSIC["query"] = query
    if HAVE_DASH:
        try:
            hdash.add_history(title, query)
        except Exception:
            pass
    log(f"music: {title}")
    try:
        proc = await hmu.open_ffmpeg_async(url, gain_db)
    except Exception as e:
        log("music ffmpeg failed:", e)
        raise
    log(f"music: ffmpeg pid={getattr(proc, 'pid', '?')}")
    stop = MUSIC["stop"] = asyncio.Event()
    VIS.update({"on": True, "frame": 0, "peak": 1.0,
                "pk": {"bass": 0.2, "mid": 0.2, "treble": 0.2}})
    log("ring: visualiser on")
    PLAYBACK["periods"] = 0
    PLAYBACK["bytes"] = 0
    clock = {"t": time.monotonic()}
    frames = 0
    prime = 3                     # ~128 ms up front; more than this is a level spike
    try:
        while not stop.is_set():
            if stale is not None and stale():
                log("music: stream abandoned (interrupted)")
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(hmu.FRAME), 12)
            except asyncio.TimeoutError:
                log("music: read stalled, restarting the stream")
                break
            if not chunk:
                break
            clock["t"] += 0.0333
            try:
                await ws.send_bytes(b"\x04" + chunk)
            except Exception as e:
                log("music send failed:", e)
                break
            frames += 1
            # The device reports playback_stats only every ~10 s, so a check
            # early in the stream is a false alarm; by ~12 s silence is real.
            if frames == 300 and PLAYBACK["periods"] == 0:
                log("music: device has accepted 12s of audio and reported no playback")
            await _visualise(chunk)
            if frames <= prime:
                clock["t"] = max(clock["t"], time.monotonic())
                continue
            slack = clock["t"] - time.monotonic()
            if slack > 0:
                await asyncio.sleep(slack)
            elif slack < -0.5:
                clock["t"] = time.monotonic()
        await ws.send_bytes(b"\x05")
    finally:
        VIS["on"] = False
        log(f"ring: visualiser sent {VIS['sent']} frames")
        ring_send("off")
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except (Exception, asyncio.CancelledError):
            pass
    log(f"music stopped after {frames} frames")


def stop_music():
    """Silence music completely: stop the producer AND cancel the task that is
    writing frames, otherwise the old song keeps streaming into the speaker
    while the mic is open."""
    MUSIC["gen"] += 1          # invalidate any request still resolving
    stopped = False
    if MUSIC["stop"] is not None:
        MUSIC["stop"].set()
        MUSIC["stop"] = None
        stopped = True
    t = MUSIC.get("task")
    if t is not None and not t.done():
        t.cancel()
        stopped = True
    MUSIC["task"] = None
    return stopped


async def quiesce_music():
    """Cancel the current song and WAIT for its writer to be gone.

    `task.cancel()` only schedules the cancellation; the task may still push
    another period or two into the music plane. Opening the next stream inside
    that window leaves the device holding a half-initialised queue -- it takes a
    couple of periods, reports minDepth -1, and then goes silent. That is the
    "says it is playing but never starts" case."""
    t = MUSIC.get("task")
    MUSIC["gen"] += 1
    if MUSIC["stop"] is not None:
        MUSIC["stop"].set()
        MUSIC["stop"] = None
    MUSIC["task"] = None
    if t is not None and not t.done():
        t.cancel()
        try:
            await asyncio.wait_for(t, 2.5)     # a resolve stuck in a thread is not worth waiting on
        except asyncio.TimeoutError:
            log("music: old task still finishing; moving on")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log("music task ended with", repr(e))
    try:
        await ctl({"type": "music_flush"})
        await ctl({"type": "speaker_flush"})
    except Exception:
        pass
    return t is not None


async def start_music(query, gain_db=None):
    """Only ever one song: a new request replaces what is playing, with a flush
    and a soft cue so the switch is a transition rather than a hard cut into
    silence while yt-dlp resolves."""
    if await quiesce_music():
        await asyncio.sleep(0.35)      # let the device's own queue drain
        await cue("done")
        await asyncio.sleep(0.45)      # the chime is still buffered on the device
    # Even with nothing playing, the plane can be left primed from a killed
    # stream (a restart, a crash). Flushing first is what makes the difference
    # between "starts playing" and "says it is playing and never does".
    try:
        await ctl({"type": "music_flush"})
        await ctl({"type": "speaker_flush"})
    except Exception:
        pass
    await asyncio.sleep(0.2)
    PLAYBACK["periods"] = 0
    gen = MUSIC["gen"]

    async def guarded():
        try:
            await play_music(query, gain_db=gain_db,
                             stale=lambda: MUSIC["gen"] != gen)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log("play_music crashed:", repr(e))

    MUSIC["task"] = asyncio.create_task(guarded())
    return MUSIC["task"]


def _synth(text):
    try:
        return hts.synthesise(text) if HAVE_APP else tts_pcm(text)
    except Exception as e:
        log("tts module failed, SAPI fallback:", e)
        return tts_pcm(text)


async def _send_pcm(ws, pcm, clock):
    """Stream one PCM buffer with deadline pacing; clock carries the timeline
    across sentences so consecutive buffers join without a gap."""
    sent = 0
    for i in range(0, len(pcm), 3200):          # 33 ms at 48 kHz mono s16
        clock["t"] += 0.0333                    # absolute schedule: no drift
        for attempt in range(3):
            try:
                if ws is None or getattr(ws, "closed", False):
                    raise RuntimeError("data plane went away")
                await ws.send_bytes(b"" + pcm[i:i + 3200])
                break
            except Exception as e:
                log(f"send retry {attempt + 1}: {e}")
                await asyncio.sleep(0.25)
        else:
            raise RuntimeError("data plane closed mid-stream")
        sent += 1
        slack = clock["t"] - time.monotonic()
        if slack > 0:
            await asyncio.sleep(slack)
        elif slack < -0.5:
            clock["t"] = time.monotonic()       # fell far behind; resync
    return sent


async def speak(text, stream=True):
    """Speak `text`. Sentence-by-sentence: the first sentence starts playing
    while the rest is still being synthesised (Piper runs at ~0.8x realtime,
    so synthesising the whole answer first cost 8+ seconds of dead air)."""
    ws = await live_data_ws()
    if ws is None:
        raise RuntimeError("no data plane")
    ring_send("speaking", color=(60, 220, 130))   # green: not your turn
    clock = {"t": time.monotonic()}
    sent = 0
    interrupted = False
    if not stream:
        sent = await _send_pcm(ws, _synth(text), clock)
    else:
        parts = [p for p in _SENT.split(text) if p.strip()] or [text]
        q = asyncio.Queue()

        t_synth = time.monotonic()

        async def producer():
            try:
                first = True
                for part in parts:
                    pcm = await asyncio.to_thread(_synth, part)
                    if first:
                        log(f"first sentence ready in {time.monotonic()-t_synth:.2f}s")
                        first = False
                    await q.put(pcm)
            finally:
                await q.put(None)

        task = asyncio.create_task(producer())
        try:
            while True:
                pcm = await q.get()
                if pcm is None:
                    break
                if clock["t"] < time.monotonic():
                    clock["t"] = time.monotonic()
                sent += await _send_pcm(ws, pcm, clock)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            task.cancel()
            try:
                await ws.send_bytes(b"")     # end the stream even when cut
            except Exception:
                pass
            ring_send("off")
            log(f"spoke {sent} frames{' (interrupted)' if interrupted else ''}: {text[:60]}")
        return sent
    await ws.send_bytes(b"")
    ring_send("off")
    log(f"spoke {sent} frames: {text[:80]}")
    return sent


_WAKE = ("hermes", "hear me", "hey hermes")
MUTED = False


def _tone(kind) -> bytes:
    """Short, soft cue tones synthesised at the speaker's 48 kHz."""
    import array
    sr = 48000
    if kind == "listen":
        notes = [(523.25, 0.085), (783.99, 0.105)]
    elif kind == "muted":
        notes = [(392.00, 0.09), (261.63, 0.13)]
    elif kind == "done":
        notes = [(659.25, 0.07)]
    else:
        notes = [(440.0, 0.08)]
    buf = array.array("h")
    for freq, dur in notes:
        n = int(sr * dur)
        attack, release = int(sr * 0.008), int(sr * 0.030)
        for i in range(n):
            env = 1.0
            if i < attack:
                env = i / attack
            elif i > n - release:
                env = max(0.0, (n - i) / release)
            # two partials keeps it soft rather than a harsh beep
            v = (0.62 * math.sin(2 * math.pi * freq * i / sr)
                 + 0.18 * math.sin(4 * math.pi * freq * i / sr))
            buf.append(int(max(-1.0, min(1.0, v * env * 0.16)) * 32000))
        buf.extend([0] * int(sr * 0.02))
    return buf.tobytes()


async def live_data_ws(timeout=6.0):
    """The device redials its planes; wait for a socket we can actually write to."""
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        ws = DATA_WS
        if ws is not None and not getattr(ws, "closed", False):
            return ws
        await asyncio.sleep(0.2)
    return None


def _warm():
    """Load Whisper and synthesise once so the first real turn is as fast as
    the tenth: cold Whisper 5.00 s vs warm 0.95 s, measured."""
    try:
        t = time.monotonic()
        _whisper(bytes(32000))
        log(f"whisper warmed in {time.monotonic()-t:.2f}s")
    except Exception as e:
        log("whisper warm failed:", e)
    try:
        t = time.monotonic()
        _synth("Ready.")
        log(f"tts warmed in {time.monotonic()-t:.2f}s")
    except Exception as e:
        log("tts warm failed:", e)


async def cue(kind="listen"):
    """Play a UI tone: the audible half of 'is it listening?'"""
    ws = await live_data_ws()
    if ws is None:
        log("cue: no data plane")
        return
    try:
        pcm = _tone(kind)
        # 3200 bytes @ 33 ms is the pacing the speaker path is proven to take.
        # 9600-byte frames kill the data plane (DATA closed 0 bytes, device
        # redials mid-turn) — that was the bug behind "it hears nothing".
        for i in range(0, len(pcm), 3200):
            await ws.send_bytes(b"\x02" + pcm[i:i + 3200])
            await asyncio.sleep(0.033)
        await asyncio.sleep(0.05)
        await ws.send_bytes(b"\x03")
        log(f"cue {kind} played ({len(pcm)} bytes)")
    except Exception as e:
        log("cue failed:", e)

# Always-on listening is PAUSED by default: the mic still streams (the button
# path needs it) but nothing is transcribed or acted on until the action
# button is pressed. Start the controller with ALWAYS_ON=1 to re-enable.
ALWAYS_ON = os.environ.get("ALWAYS_ON", "0") == "1"


def _is_wake(text: str) -> bool:
    try:
        import voice as hvoice
        return hvoice.is_wake(text)
    except Exception:
        return any(w in (text or "").lower() for w in _WAKE)


def _strip_wake(text: str) -> str:
    t = (text or "").strip()
    for lead in ("hey hermes", "ok hermes", "hermes", "hear me"):
        if t.lower().startswith(lead):
            t = t[len(lead):].lstrip(" ,.:;-")
            break
    return t or text


async def run_turn():
    global BUSY
    if BUSY:
        log("turn already running")
        return
    BUSY = True
    try:
        if HAVE_APP:
            await convo.turn(asyncio_mod=asyncio, log=log, ring=ring_send,
                             transcribe=_whisper, ask=_hermes_ask, speak=speak,
                             follow_up=True)
        else:
            await button_turn()
    except Exception as e:
        log("run_turn failed:", e)
        ring_send("error")
    finally:
        BUSY = False
        await cue("done")


async def bounded_turn(max_wait=15.0):
    """Ask the device for one VAD-gated turn and collect what it streams."""
    ev = turn_event()
    ev.clear()
    TURN["buf"] = bytearray()
    TURN["end"] = None
    TURN["active"] = True
    try:
        for attempt in range(1, 4):
            TURN["end"] = None
            ev.clear()
            # Always release first. The device keeps the mic locked between
            # turns; asking for it while it is still held comes back with a
            # three-byte stub instead of audio.
            try:
                await ctl({"type": "mic_stop"})
            except Exception:
                pass
            await asyncio.sleep(0.15 if attempt == 1 else 0.45)
            await ctl({"type": "mic_start", "lock_mic": True})
            log(f"mic_start lock_mic=true (attempt {attempt})")
            # The device primes the mic slowly (~1.5-2 s before the first
            # frame). Give it 2.5 s, and NEVER clear what already arrived: an
            # earlier 1.2 s check threw away real speech and then transcribed
            # the tail of the sentence as silence.
            first = time.monotonic() + 2.5
            while time.monotonic() < first and not len(TURN["buf"]):
                try:
                    await asyncio.wait_for(ev.wait(), 0.15)
                    break
                except asyncio.TimeoutError:
                    pass
            # A few bytes is not audio: the device answers mic_start with a
            # short stub when the mic is still held. Treat that as "did not
            # open" and try again rather than transcribing three bytes.
            if len(TURN["buf"]) >= 2048:
                break
            if len(TURN["buf"]):
                log(f"attempt {attempt}: mic returned a stub ({len(TURN['buf'])} bytes)")
                TURN["buf"].clear()
            else:
                log(f"attempt {attempt}: mic opened but silence")

        started = time.monotonic()
        last_len, last_change = 0, time.monotonic()
        while time.monotonic() - started < max_wait:
            try:
                await asyncio.wait_for(ev.wait(), 0.15)
                break                       # device sent 0x04 / 0x05
            except asyncio.TimeoutError:
                pass
            n = len(TURN["buf"])
            if n != last_len:
                last_len, last_change = n, time.monotonic()
            elif n >= 8000 and time.monotonic() - last_change > 1.2:
                # device VAD sentinel did not arrive; its preroll+aec keeps the
                # gate open. Silence in the stream is just as good an ending.
                TURN["end"] = "silence"
                log(f"turn ended on 1.2s silence ({n} bytes)")
                break
        else:
            TURN["end"] = "timeout"
            log(f"turn timeout after {len(TURN['buf'])} bytes")
    except Exception as e:
        TURN["end"] = f"error: {e}"
        log("bounded_turn:", e)
    finally:
        TURN["active"] = False
        try:
            await ctl({"type": "mic_stop"})
        except Exception:
            pass
    return bytes(TURN["buf"]), TURN["end"]


FOLLOW_UPS = int(os.environ.get("VOICE_FOLLOWUPS", "0"))


async def voice_loop(max_turns=None, follow_up_wait=9.0):
    """Push-to-talk: listen, answer out loud. By default one press is one turn
    (max_turns = 1 + VOICE_FOLLOWUPS) — a follow-up window that stays open hears
    the room and answers strangers, which is what "it answered Bye" was."""
    if max_turns is None:
        try:
            fu = int(hdash.load_config().get("followups", FOLLOW_UPS)) if HAVE_DASH else FOLLOW_UPS
        except Exception:
            fu = FOLLOW_UPS
        max_turns = 1 + fu
    for n in range(1, max_turns + 1):
        ring_send("listening")
        pcm, why = await bounded_turn(max_wait=15.0 if n == 1 else follow_up_wait)
        if why == "no_speech":
            log(f"turn {n}: no speech")
            ring_send("off")
            if n == 1:
                await cue("muted")
            break
        if not pcm:
            log(f"turn {n}: no audio ({why})")
            ring_send("off")
            break
        ring_send("thinking")
        text = await asyncio.to_thread(_whisper, pcm)
        log(f"turn {n} heard: {text!r} ({len(pcm)} bytes, {why})")
        if not text.strip():
            ring_send("off")
            break
        reply = ""
        low = text.lower().strip()
        want = music_query(low)
        if want:
            if MUSIC["stop"] is not None:
                stop_music()
                await asyncio.sleep(0.3)
            if MUSIC["stop"] is not None:
                stop_music()
                await asyncio.sleep(0.3)
            log(f"turn {n} music request: {want!r}")
            # No spoken confirmation here on purpose: TTS lands on the voice
            # plane, music on its own plane, and the device sums them then
            # saturates -- "Playing X" over the intro is audible clipping. The
            # chime in start_music is the acknowledgement instead.
            await start_music(want)
            break
        if is_hallucination(low) or (len(low.split()) <= 1 and low not in _ONE_WORD_OK):
            log(f"ignored: likely the music, not you ({heard!r})")
            break
        if is_trivial(low):
            log(f"turn {n} ignored trivial utterance: {low!r}")
            break
        if low in ("stop", "stop music", "pause", "pause music", "be quiet",
                   "stop playing", "turn it off"):
            stop_music()
            log(f"turn {n} music stop")
            await speak("Stopped.")
            break
        if not needs_agent(text):
            if HAVE_WEB and SETTINGS.get("google", True) and not os.environ.get("DOT_DISABLE_GOOGLE"):
                try:
                    ga, kind = await hweb.google_answer(text)
                    log(f"turn {n} google lane ({kind}): {ga[:110]}")
                    reply = ga
                except Exception as e:
                    log("google lane failed:", e)
            if not reply.strip() and HAVE_QUICK:
                try:
                    reply, secs, how = await asyncio.to_thread(quick.answer, text)
                    log(f"turn {n} snippet lane ({how}) {secs:.1f}s: {reply[:120]}")
                except Exception as e:
                    log("snippet lane failed:", e)
                    reply = ""
        if not reply.strip():
            reply = await asyncio.to_thread(
                _hermes_ask, convo.SPOKEN_INSTRUCTION + text if HAVE_APP else text)
        if not reply.strip():
            reply = _local_answer(text)
            if reply:
                log(f"turn {n} local answer: {reply[:60]}")
        if not reply.strip():
            log(f"turn {n}: brain returned nothing")
            await speak("Sorry, I'm a bit busy. Give me a second and ask again.")
            break
        log(f"turn {n} reply: {reply[:200]}")
        await speak(convo.spoken_text(reply) if HAVE_APP else reply)
        if n == max_turns:
            break
        await asyncio.sleep(0.2)
    ring_send("off")


async def on_press():
    """The action button: start a turn, or interrupt the one in progress.

    A press while it is speaking cancels the answer mid-sentence, flushes the
    speaker buffer and starts listening again. Waiting for a long reply to finish
    before you can talk is the behaviour people describe as "it won't let me
    interrupt"."""
    global BUSY
    await asyncio.sleep(0.25)          # let mute_state land first
    if BUSY:
        prev = TURN_TASK.get("task")
        log("press: interrupt requested")
        if prev is not None and not prev.done() and prev is not asyncio.current_task():
            prev.cancel()
            try:
                await prev
            except asyncio.CancelledError:
                pass                   # that was the old turn, not this one
            except Exception as e:
                log("press: old turn ended with", repr(e))
        try:
            await ctl({"type": "speaker_flush"})   # drop what is still buffered
        except Exception:
            pass
        log("press: interrupted, listening again")
        BUSY = False
    if MUTED:
        log("press: mic is hardware-muted -> cue 'muted', nothing recorded")
        ring_send("muted")
        await cue("muted")
        return
    BUSY = True
    interrupted = False
    TURN_TASK["task"] = asyncio.current_task()
    # Music first: a press means "stop that and hear me". Cancel the stream,
    # flush the speaker buffer so the tail cannot bleed into the mic, then listen.
    if await quiesce_music():
        log("press: music stopped for the turn")
        # The flush stops the producer, not what the device has already queued:
        # several seconds of the song are still in its buffer. Opening the mic
        # immediately records the tail of the track, which Whisper turns into a
        # confident sentence ("Okay, I should not do that.") and we answer it.
        await asyncio.sleep(0.9)
    log("press: mic live -> listening")
    try:
        ring_send("listening")
        await cue("listen")
        await asyncio.sleep(0.30)      # let the cue clear before the mic opens
        await voice_loop()
    except asyncio.CancelledError:
        interrupted = True
        log("turn: cancelled (interrupted)")
        raise
    except Exception as e:
        log("turn failed:", e)
        ring_send("error")
    finally:
        BUSY = False
        TURN_TASK["task"] = None
        ring_send("off")
        if not interrupted:
            await cue("done")

# ── action button -> a real Hermes turn ───────────────────────────────────────
BUSY = False
VOICE_SESSION = "kitchen"
VOICE_PROFILE = "dot"


async def capture(seconds=7.0, end_silence=1.2):
    """Collect mic PCM for a turn: up to `seconds`, ending after quiet."""
    pcm = bytearray()
    deadline = time.monotonic() + seconds
    quiet = 0.0
    while time.monotonic() < deadline:
        try:
            chunk = await asyncio.wait_for(AUDIO_Q.get(), 0.5)
        except asyncio.TimeoutError:
            continue
        pcm += chunk
        n = len(chunk) // 2
        if n:
            s = struct.unpack("<%dh" % n, chunk[:n * 2])
            rms = math.sqrt(sum(v * v for v in s) / n)
            if rms < 700:
                quiet += n / 16000.0
            else:
                quiet = 0.0
        if quiet >= end_silence and len(pcm) > 16000 * 2 * 0.6:
            break
    return bytes(pcm)


def _whisper(text_pcm):
    from faster_whisper import WhisperModel
    global _WM
    try:
        _WM
    except NameError:
        _WM = None
    if _WM is None:
        _WM = WhisperModel("tiny", device="cpu", compute_type="int8")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(text_pcm)
        segs, _ = _WM.transcribe(p, language="en", vad_filter=True,
                                 condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        if not text and len(text_pcm) > 16000:
            # VAD can drop a clipped or AEC-thinned utterance entirely; one
            # pass without it is cheaper than asking the person to repeat.
            segs, _ = _WM.transcribe(p, language="en", vad_filter=False,
                                     condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs).strip()
        return text


def _local_answer(text):
    """Answers that need nothing: no key, no browser, no model, no network.

    A machine with none of the optional pieces still gets a useful speaker --
    which is the difference between "you need my exact setup" and "install it".
    """
    import datetime
    low = (text or "").lower()
    now = datetime.datetime.now()
    if "time" in low:
        # "1:39 PM" reads correctly through TTS; "1:39 p m" does not.
        return now.strftime("%I:%M %p").lstrip("0")
    if "date" in low or "what day" in low or "today" in low:
        return now.strftime("%A, %B ") + str(now.day)
    if "what can you do" in low or "help" == low.strip():
        return ("I can play music, tell you the time and date, and answer questions "
                "when a brain is connected.")
    return ""


PERSONA = (
    "You are the voice assistant for a small household speaker. Answer the way a "
    "person would say it out loud: at most two short sentences, no markdown, no "
    "lists, no code, no URLs read aloud, no file paths. If you do not know, say so "
    "plainly."
)


def _llm_ask(text):
    """Any OpenAI-compatible endpoint: OpenAI, Groq, vLLM, LM Studio, llama.cpp,
    Ollama's /v1, or a box in the cupboard. This is the lane that makes the
    project agent-agnostic -- nothing here needs Hermes to be installed."""
    import urllib.request
    url = SETTINGS.get("llm_url") or os.environ.get("DOT_LLM_URL", "")
    base = url.rstrip("/")
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    body = {
        "model": SETTINGS.get("llm_model") or os.environ.get("DOT_LLM_MODEL", "gpt-4o-mini"),
        "messages": [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": text}],
        "max_tokens": int(os.environ.get("DOT_LLM_MAX_TOKENS", "220")),
        "temperature": 0.3,
    }
    key = os.environ.get("DOT_LLM_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base, data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=float(
            os.environ.get("DOT_LLM_TIMEOUT", "25"))) as r:
        j = json.loads(r.read().decode())
    return (j["choices"][0]["message"].get("content") or "").strip()


def _hermes_ask(text):
    """One turn against the Dot's own named session.

    `--continue kitchen --create-if-missing` keeps the thread (verified: a fact
    stored on one turn was recalled on the next, same session_id) and, with -Q,
    prints only the answer plus a session_id line we strip.
    """
    if SETTINGS.get("llm_url") or os.environ.get("DOT_LLM_URL"):
        try:
            out = _llm_ask(text)
            log(f"brain: llm endpoint answered ({len(out)} chars)")
            return out
        except Exception as e:
            log("brain: llm endpoint failed, falling back to the CLI:", e)
    cmd = ["hermes", "chat", "-q", text,
           "--continue", VOICE_SESSION, "--create-if-missing", "-Q"]
    out = ""
    for attempt in range(1, 4):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip()
        # One turn at a time is enforced upstream, but a stale CLI process can
        # still hold the session. Never speak that error into the room.
        if "session_already_owned" in out or "couldn't start the model" in out:
            log(f"brain busy (attempt {attempt})")
            time.sleep(1.5 * attempt)
            continue
        break
    lines = [ln for ln in out.splitlines()
             if ln.strip() and not ln.startswith(
                 ("session_id:", "Warning:", "Session local-",
                  "Use 'hermes sessions", "No session found"))]
    reply = chr(10).join(lines).strip()
    if not reply or "couldn't start the model" in reply or "session_already_owned" in reply:
        err = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
        if err:
            log("brain error:", err[-1][:160])
        return ""
    return reply


async def listen_test(phrase="what time is it"):
    """Self-test: open a real bounded turn, speak through the speaker so the
    mic array hears it, and see what the pipeline transcribes."""
    t = asyncio.create_task(bounded_turn(max_wait=12.0))
    await asyncio.sleep(0.80)
    await speak(phrase)
    pcm, why = await t
    text = await asyncio.to_thread(_whisper, pcm) if pcm else ""
    return why, len(pcm), text


async def button_turn():
    """Action button pressed: listen, transcribe, ask Hermes, speak the answer."""
    global BUSY
    if BUSY:
        return
    BUSY = True
    try:
        ring_send("listening")
        log("button turn: listening")
        pcm = await capture()
        if len(pcm) < 16000 * 2 * 0.4:
            log("button turn: too short")
            ring_send("error")
            return
        ring_send("thinking")
        text = await asyncio.to_thread(_whisper, pcm)
        log(f"button turn heard: {text!r}")
        if not text:
            ring_send("error")
            return
        reply = await asyncio.to_thread(_hermes_ask, text)
        log(f"hermes replied ({len(reply)} chars): {reply[:160]!r}")
        spoken = reply if len(reply) <= 600 else reply[:597] + "..."
        await speak(spoken)
    except Exception as e:
        log("button turn failed:", e)
        ring_send("error")
    finally:
        BUSY = False


async def responder():
    """Turn-end VAD -> Whisper -> answer, so the Dot answers when spoken to."""
    model = None
    buf = bytearray()
    speech = False
    silence = 0
    while True:
        chunk = await AUDIO_Q.get()
        if HAVE_APP and convo.CAPTURE["on"]:
            convo.feed(chunk)          # a button turn owns the stream right now
            continue
        if not ALWAYS_ON:
            continue                   # paused: button-driven turns only
        buf += chunk
        n = len(chunk) // 2
        if n:
            samples = struct.unpack("<%dh" % n, chunk[:n * 2])
            rms = math.sqrt(sum(v * v for v in samples) / n)
        else:
            rms = 0
        if rms > 700:
            if not speech:
                ring_send("listening")
            speech, silence = True, 0
        elif speech:
            silence += n
        if not speech:
            if len(buf) > 32000:
                del buf[:-32000]
            continue
        if silence > 16000 * 0.9 and len(buf) > 16000 * 2 * 0.6:
            clip = bytes(buf)
            buf = bytearray()
            speech, silence = False, 0
            ring_send("thinking")
            try:
                if model is None:
                    from faster_whisper import WhisperModel
                    log("loading whisper tiny...")
                    model = WhisperModel("tiny", device="cpu", compute_type="int8")
                    log("whisper ready")
                with tempfile.TemporaryDirectory() as tmp:
                    p = os.path.join(tmp, "t.wav")
                    with wave.open(p, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(16000)
                        w.writeframes(clip)
                    segs, info = model.transcribe(p, language="en", vad_filter=True,
                                                  condition_on_previous_text=False)
                    text = " ".join(s.text.strip() for s in segs).strip()
                if not text:
                    continue
                log(f"heard: {text!r}")
                if _is_wake(text):
                    log("wake word accepted:", text[:80])
                    await convo.converse(
                        asyncio_mod=asyncio, log=log, ring=ring_send,
                        transcribe=_whisper, ask=_hermes_ask, speak=speak,
                        text=_strip_wake(text), follow_up=True)
                else:
                    log("no wake word — ignoring")
            except Exception as e:
                log("responder error:", e)


def tts_pcm(text, voice="Microsoft Zira Desktop"):
    """Synthesise text with Windows SAPI, return raw 16 kHz mono s16le PCM."""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "say.wav").replace("\\", "/")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$sp = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"try {{ $sp.SelectVoice('{voice}') }} catch {{ }}; "
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo"
            "(48000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$sp.SetOutputToWaveFile('{wav_path}', $fmt); "
            f"$sp.Speak({json.dumps(text)}); $sp.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=60)
        if not os.path.exists(wav_path):
            raise RuntimeError("tts produced no file")
        with wave.open(wav_path, "rb") as w:
            frames = w.readframes(w.getnframes())
            rate = w.getframerate()
            if len(frames) < 1600 or rate != 48000:
                raise RuntimeError(f"tts output wrong: {len(frames)} bytes at {rate} Hz")
            return frames


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.0.0.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


async def control(ws, req):
    global DEVICE_WS
    DEVICE_WS = ws
    log("CONTROL connected from", req.remote)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                log("control non-text frame", len(msg.data))
                continue
            try:
                d = json.loads(msg.data)
            except Exception:
                log("control raw:", msg.data[:200])
                continue
            t = d.get("type")
            if t == "register":
                log("REGISTER")
                for k in ("device_id", "version", "base_os", "board",
                          "kernel_arch", "kernel_release", "ip"):
                    if k in d:
                        log(f"   {k}: {d[k]}")
                caps = d.get("capabilities") or []
                log(f"   capabilities ({len(caps)}): {', '.join(caps)}")
                ack = {
                    "type": "ack",
                    "device_id": d.get("device_id"),
                    "features": ["ble_adverts_data", "listen_session", "output_chain", "ble_connect"],
                }
                await ws.send_str(json.dumps(ack))
                log("   ack sent")
                asyncio.create_task(amp_on())      # one shell trip per connection
            elif t == "volume_state":
                VOLUME["level"] = int(d.get("level", VOLUME["level"]))
            elif t == "mute_state":
                global MUTED
                MUTED = bool(d.get("muted"))
                log("mute_state:", MUTED)
                ring_send("muted" if MUTED else "off")
            elif t == "button":
                log("button:", json.dumps(d)[:200])
                if not d.get("down"):
                    asyncio.create_task(on_press())
            elif t in ("stats",):
                log("stats:", json.dumps(d)[:200])
            else:
                if t == "playback_stats":
                    st = d.get("stats") or d
                    try:
                        PLAYBACK["periods"] = int(st.get("periods", 0))
                        PLAYBACK["bytes"] = int(st.get("bytesRecv", 0))
                    except Exception:
                        pass
                log(f"{t}: {json.dumps(d)[:240]}")
    except Exception as e:
        log("control error:", e)
    DEVICE_WS = None
    log("CONTROL closed")


async def data(ws, req):
    global DATA_WS
    DATA_WS = ws
    log("DATA connected from", req.remote)
    total = 0
    dump = os.environ.get("DUMP_PCM")
    fh = open(dump, "ab") if dump else None
    if fh:
        log("dumping PCM to", dump)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                data = msg.data
                code, payload = data[0], data[1:]
                total += len(payload)
                if code == 0x01:            # mic PCM, mono S16_LE 16 kHz
                    if fh:
                        fh.write(payload)
                    AUDIO_Q.put_nowait(payload)
                    if TURN["active"]:
                        TURN["buf"] += payload
                elif code in (0x04, 0x05):
                    TURN["end"] = "speech_end" if code == 0x04 else "no_speech"
                    log(f"turn sentinel: {TURN['end']} after {len(TURN['buf'])} bytes")
                    if TURN["done"] is not None:
                        TURN["done"].set()
                elif code == 0x07:
                    log("frame 0x07 session audio")
                if total % 320000 < 4096:
                    log(f"data: {total} bytes PCM")
    except Exception as e:
        log("data error:", e)
    finally:
        if fh:
            fh.close()
        if DATA_WS is ws:
            DATA_WS = None
    log("DATA closed", total, "bytes")


async def shell(ws, req):
    global SHELL_WS
    SHELL_WS = ws
    log("SHELL connected from", req.remote, "path", req.path)
    SHELL_READY.set()
    try:
        async for msg in ws:
            if isinstance(msg.data, (bytes, bytearray)):
                text = bytes(msg.data).decode("utf-8", "replace").strip()
                if text:
                    log("  shell>", text[:200])
            elif msg.data:
                log("  shell>", str(msg.data)[:200])
    except Exception:
        pass
    SHELL_WS = None
    log("SHELL closed")


async def any_route(request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        path = request.path
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        if path == "/control":
            await control(ws, request)
        elif path == "/data":
            await data(ws, request)
        elif path.startswith("/shell/"):
            await shell(ws, request)
        else:
            log("unknown ws path", path)
            await ws.close()
        return ws
    if request.method == "GET" and request.path in ("/ui", "/ui/"):
        if not HAVE_DASH:
            return web.Response(text="dashboard module missing", status=503)
        return web.Response(text=hdash.PAGE, content_type="text/html")

    if request.method == "GET" and request.path == "/api/state":
        cfg = hdash.load_config() if HAVE_DASH else {}
        task = MUSIC.get("task")
        return web.json_response({
            "settings": {**{k: v for k, v in SETTINGS.items() if k != "llm_key"},
                         "has_key": bool(SETTINGS.get("llm_key"))},
            "device": DEVICE_WS is not None and not getattr(DEVICE_WS, "closed", True),
            "playing": bool(task and not task.done()),
            "now_playing": MUSIC.get("title") or "",
            "query": MUSIC.get("query") or "",
            "volume": VOLUME["level"],
            "eq": cfg.get("eq", {"bass": 0, "mid": 0, "treble": 0}),
            "followups": int(cfg.get("followups", 0)),
            "always_on": bool(cfg.get("always_on", False)),
            "history": hdash.history(30) if HAVE_DASH else [],
            "lighting": LIGHT,
        })

    if request.method == "POST" and request.path == "/api/play":
        body = await request.json()
        q = (body.get("q") or "").strip()
        if not q:
            return web.json_response({"ok": False, "error": "no query"}, status=400)
        await start_music(q)
        return web.json_response({"ok": True, "playing": q})

    if request.method == "POST" and request.path == "/api/stop":
        stopped = stop_music()
        try:
            await ctl({"type": "music_flush"})
            await ctl({"type": "speaker_flush"})
        except Exception:
            pass
        return web.json_response({"ok": True, "stopped": stopped})

    if request.method == "POST" and request.path == "/api/volume":
        body = await request.json()
        level = max(0, min(100, int(body.get("level", 60))))
        VOLUME["level"] = level
        try:
            await ctl({"type": "volume_set", "level": level})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        return web.json_response({"ok": True, "volume": level})

    if request.method == "GET" and request.path == "/api/settings":
        safe = dict(SETTINGS)
        safe["llm_key"] = ""          # never echo a secret back to the page
        safe["has_key"] = bool(SETTINGS.get("llm_key"))
        return web.json_response(safe)

    if request.method == "POST" and request.path == "/api/settings":
        body = await request.json()
        for k in ("title", "llm_url", "llm_model", "llm_key", "voice"):
            if k in body and isinstance(body[k], str):
                SETTINGS[k] = body[k].strip()
        if "google" in body:
            SETTINGS["google"] = bool(body["google"])
        if HAVE_DASH:
            try:
                hdash.save_config({"settings": SETTINGS})
            except Exception as e:
                log("settings save failed:", e)
        log("settings updated:", {k: ("***" if k == "llm_key" and v else v)
                                  for k, v in SETTINGS.items()})
        safe = dict(SETTINGS); safe["llm_key"] = ""
        return web.json_response({"ok": True, "settings": safe})

    if request.method == "GET" and request.path == "/api/lighting":
        return web.json_response({**LIGHT, "presets": list(PRESETS)})

    if request.method == "POST" and request.path == "/api/lighting":
        body = await request.json()
        if "enabled" in body:
            LIGHT["enabled"] = bool(body["enabled"])
        if "preset" in body:
            name = str(body["preset"])
            if name not in PRESETS:
                return web.json_response(
                    {"ok": False, "error": f"unknown preset {name}"}, status=400)
            LIGHT["preset"] = name
        if "hue" in body:
            LIGHT["hue"] = int(body["hue"]) % 360
        if "brightness" in body:
            LIGHT["brightness"] = max(0.05, min(1.0, float(body["brightness"])))
        if HAVE_DASH:
            try:
                hdash.save_config({"lighting": LIGHT})
            except Exception as e:
                log("lighting: save failed:", e)
        log(f"lighting -> {LIGHT}")
        return web.json_response({"ok": True, **LIGHT})

    if request.method == "POST" and request.path == "/api/eq":
        body = await request.json()
        cur = hdash.load_config().get("eq", {}) if HAVE_DASH else {}
        cur.update({k: float(body[k]) for k in ("bass", "mid", "treble") if k in body})
        hdash.save_config({"eq": cur})
        return web.json_response({"ok": True, "eq": cur})

    if request.method == "POST" and request.path == "/api/config":
        body = await request.json()
        patch = {}
        for k in ("followups", "always_on"):
            if k in body:
                patch[k] = int(body[k]) if k == "followups" else bool(body[k])
        cfg = hdash.save_config(patch) if patch else hdash.load_config()
        return web.json_response({"ok": True, "followups": cfg.get("followups", 0),
                                  "always_on": cfg.get("always_on", False)})

    if request.method == "POST" and request.path == "/_press":
        # Same entry point the action button uses, for testing without hands.
        asyncio.create_task(on_press())
        return web.Response(text="press injected")

    if request.method == "POST" and request.path == "/_lane":
        body = await request.json()
        q = body.get("q", "")
        if needs_agent(q):
            return web.Response(text=f"lane=agent q={q}")
        if HAVE_WEB:
            ga, kind = await hweb.google_answer(q)
            if ga:
                return web.Response(text=f"lane=google({kind}) {ga[:220]}")
        if HAVE_QUICK:
            r, secs, how = await asyncio.to_thread(quick.answer, q)
            return web.Response(text=f"lane=snippets({how}) {secs:.1f}s {r[:200]}")
        return web.Response(text="lane=none")

    if request.method == "POST" and request.path == "/_music":
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        q = body.get("q", "")
        if not q:
            return web.Response(text="no q", status=400)
        if MUSIC["stop"] is not None:
            stop_music()
        g = body.get("gain")
        await start_music(q, gain_db=(float(g) if g is not None else None))
        return web.Response(text=f"music: {q} gain={g}")

    if request.method == "POST" and request.path == "/_tone":
        # Diagnostic: a clean 440 Hz sine through the MUSIC plane, so transport
        # can be judged separately from any source material.
        ws = await live_data_ws()
        if ws is None:
            return web.Response(text="no data plane", status=503)
        import math as _m
        secs = 5
        n = 48000 * secs
        buf = bytearray()
        for i in range(n):
            v = int(0.5 * 16000 * _m.sin(2 * _m.pi * 440 * i / 48000))
            buf += v.to_bytes(2, "little", signed=True)
        clock = {"t": time.monotonic()}
        frames = 0
        for i in range(0, len(buf), 4096):
            chunk = buf[i:i + 4096]
            if len(chunk) < 4096:
                chunk += bytes(4096 - len(chunk))
            clock["t"] += 2048 / 48000
            await ws.send_bytes(b"" + chunk)
            frames += 1
            await _visualise(chunk)
            if frames <= prime:
                clock["t"] = max(clock["t"], time.monotonic())
                continue
            slack = clock["t"] - time.monotonic()
            if slack > 0:
                await asyncio.sleep(slack)
        await ws.send_bytes(b"")
        return web.Response(text=f"tone sent, {frames} frames")

    if request.method == "POST" and request.path == "/_stopmusic":
        return web.Response(text="stopped" if stop_music() else "not playing")

    if request.method == "POST" and request.path == "/_full_test":
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        q = body.get("text", "what time is it")
        t = asyncio.create_task(voice_loop(max_turns=1))
        await asyncio.sleep(0.8)
        await speak(q)
        await t
        return web.Response(text="full loop completed - see log")

    if request.method == "POST" and request.path == "/_listen_test":
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        spoke, n, text = await listen_test(body.get("text", "what time is it"))
        return web.Response(text=f"spoke={spoke} bytes={n} heard={text!r}")

    if request.method == "POST" and request.path == "/_ask":
        body = await request.json()
        text = body.get("text", "")
        if not text:
            return web.Response(text="no text", status=400)
        try:
            reply = await asyncio.to_thread(_hermes_ask, text)
            await speak(convo.spoken_text(reply) if HAVE_APP else reply)
        except Exception as e:
            return web.Response(text=f"ask failed: {e}", status=502)
        return web.Response(text=f"ok: {reply[:200]}")

    if request.method == "POST" and request.path == "/_cue":
        body = await request.json()
        kind = body.get("kind", "listen")
        await cue(kind)
        return web.Response(text=f"cue {kind} sent")

    if request.method == "POST" and request.path == "/_say":
        body = await request.json()
        text = body.get("text", "")
        if DATA_WS is None:
            return web.Response(text="no data plane", status=503)
        try:
            n = await speak(text)
        except Exception as e:
            return web.Response(text=f"say failed: {e}", status=502)
        return web.Response(text=f"ok {n} frames")

    if request.method == "POST" and request.path == "/_send":
        body = await request.text()
        if DEVICE_WS is None:
            return web.Response(text="no device connected\n", status=503)
        try:
            await DEVICE_WS.send_str(body)
        except Exception as e:
            return web.Response(text=f"send failed: {e}\n", status=500)
        log("sent to device:", body[:140])
        return web.Response(text="ok\n")

    log(f"HTTP probe {request.method} {request.path} from {request.remote}")
    return web.Response(text="hermes controller\n", status=200)
class _FakeDevice:
    """A stand-in for the Dot that accepts the real protocol and no-ops it.

    It exists so someone can clone this repo, run one command and see the
    dashboard and the ring logic work without owning a rooted Echo.  It counts
    what it was sent rather than pretending to play it."""

    def __init__(self, name="sim"):
        self.name = name
        self.closed = False
        self.frames = 0
        self.bytes = 0
        self.last = None

    async def send_str(self, data):
        self.frames += 1
        self.last = data
        if self.frames % 40 == 0:
            log(f"sim/{self.name}: {self.frames} frames, {self.bytes} bytes of audio")

    async def send_bytes(self, data):
        self.bytes += len(data)


async def main():
    ip = lan_ip()
    @web.middleware
    async def gate(request, handler):
        """Optional shared-secret on the browser surface only.

        The device sockets (/control, /data, /shell) stay open -- they are how
        the Dot reaches us, and it cannot send a header.  Everything a human or
        a curl can reach (the dashboard and the JSON API) is gated when
        DOT_UI_TOKEN is set, because this port is bound on the LAN."""
        if UI_TOKEN and request.path.startswith(("/ui", "/api", "/_")):
            given = (request.query.get("t") or request.headers.get("X-Dot-Token", ""))
            if not hmac.compare_digest(given, UI_TOKEN):
                return web.Response(status=401,
                                    text="DOT_UI_TOKEN required: open /ui?t=<token>")
        return await handler(request)

    app = web.Application(middlewares=[gate])

    if SIM:
        global DEVICE_WS, DATA_WS
        DEVICE_WS = _FakeDevice("control")
        DATA_WS = _FakeDevice("data")
        log("SIMULATE: fake device attached — no Echo needed, nothing is played")
        log("SIMULATE: the dashboard and ring logic are live; audio goes nowhere")
    app.router.add_route("*", "/{tail:.*}", any_route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"controller listening on {ip}:{PORT}")
    # A bigger default pool: every blocking call (whisper, yt-dlp, ffmpeg reads,
    # searches) runs through it, and a starved pool silently stalls playback.
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_event_loop().set_default_executor(ThreadPoolExecutor(max_workers=16))
    load_lighting()
    load_settings()
    asyncio.create_task(asyncio.to_thread(_warm))

    info = ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=PORT,
        properties={"version": "1", "server": MDNS_NAME},
        server=f"{MDNS_NAME}.local.",
    )
    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        await azc.async_register_service(info, allow_name_change=True)
    except Exception as e:
        log("mDNS registration failed, continuing with static endpoints:", e)
    log("mDNS advertised; waiting for the Echo")
    asyncio.create_task(responder())
    log("responder armed (say 'Hermes ...')")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
