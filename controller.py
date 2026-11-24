"""Hermes controller for an Echo Dot running emOS (aiohttp version).

Serves HTTP probes with 200 (the device's probeTCP expects an answer) and
upgrades real WebSocket clients on /control, /data and /shell/{id}.
Advertises _emcontroller._tcp.local over mDNS exactly as the EchoMuse
controller does: version=1, server=<name>, port 8767, no tls_port.
"""
import asyncio
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_app"))
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
    return await shell_run(
        'tinymix -D 0 "Ext_Speaker_Amp_Switch" On; '
        'tinymix -D 0 "PCM Playback Volume" 127 127; '
        'tinymix -D 0 "Ext_Amp_Gain" 12dB; '
        'tinymix -D 0 "Audio Mrgrx Volume" 98304', settle=0.5)


_SENT = re.compile(r"(?<=[.!?])\s+")

MUSIC = {"task": None, "stop": None, "title": "", "query": ""}
VOLUME = {"level": 60}


async def play_music(query, ws=None, gain_db=None):
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
    clock = {"t": time.monotonic()}
    frames = 0
    try:
        while not stop.is_set():
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
            slack = clock["t"] - time.monotonic()
            if slack > 0:
                await asyncio.sleep(slack)
            elif slack < -0.5:
                clock["t"] = time.monotonic()
        await ws.send_bytes(b"\x05")
    finally:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    log(f"music stopped after {frames} frames")


def stop_music():
    """Silence music completely: stop the producer AND cancel the task that is
    writing frames, otherwise the old song keeps streaming into the speaker
    while the mic is open."""
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


async def start_music(query, gain_db=None):
    """Only ever one song: a new request replaces what is playing, with a flush
    and a soft cue so the switch is a transition rather than a hard cut into
    silence while yt-dlp resolves."""
    if stop_music():
        try:
            await ctl({"type": "music_flush"})
            await ctl({"type": "speaker_flush"})
        except Exception:
            pass
        await asyncio.sleep(0.15)
        await cue("done")
    async def guarded():
        try:
            await play_music(query, gain_db=gain_db)
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
    ring_send("speaking")
    clock = {"t": time.monotonic()}
    sent = 0
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
        finally:
            task.cancel()
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
            if attempt == 1:
                try:
                    await ctl({"type": "mic_stop"})
                except Exception:
                    pass
                await asyncio.sleep(0.15)
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
            if len(TURN["buf"]):
                break
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
            await start_music(want)
            await speak(f"Playing {want}.")
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
            if HAVE_WEB and not os.environ.get("DOT_DISABLE_GOOGLE"):
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
    """The action button: exactly one turn at a time, ever."""
    global BUSY
    await asyncio.sleep(0.25)          # let mute_state land first
    if BUSY:
        log("press ignored: a turn is already running")
        return
    if MUTED:
        log("press: mic is hardware-muted -> cue 'muted', nothing recorded")
        ring_send("muted")
        await cue("muted")
        return
    BUSY = True
    # Music first: a press means "stop that and hear me". Cancel the stream,
    # flush the speaker buffer so the tail cannot bleed into the mic, then listen.
    if stop_music():
        log("press: music stopped for the turn")
        try:
            await ctl({"type": "music_flush"})
            await ctl({"type": "speaker_flush"})
        except Exception:
            pass
        await asyncio.sleep(0.25)
    log("press: mic live -> listening")
    try:
        ring_send("listening")
        await cue("listen")
        await asyncio.sleep(0.30)      # let the cue clear before the mic opens
        await voice_loop()
    except Exception as e:
        log("turn failed:", e)
        ring_send("error")
    finally:
        BUSY = False
        ring_send("off")
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


def _hermes_ask(text):
    """One turn against the Dot's own named session.

    `--continue kitchen --create-if-missing` keeps the thread (verified: a fact
    stored on one turn was recalled on the next, same session_id) and, with -Q,
    prints only the answer plus a session_id line we strip.
    """
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
            "device": DEVICE_WS is not None and not getattr(DEVICE_WS, "closed", True),
            "playing": bool(task and not task.done()),
            "now_playing": MUSIC.get("title") or "",
            "query": MUSIC.get("query") or "",
            "volume": VOLUME["level"],
            "eq": cfg.get("eq", {"bass": 0, "mid": 0, "treble": 0}),
            "followups": int(cfg.get("followups", 0)),
            "always_on": bool(cfg.get("always_on", False)),
            "history": hdash.history(30) if HAVE_DASH else [],
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
async def main():
    ip = lan_ip()
    app = web.Application()
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
        # A second controller (or a stale registration) answers to the same
        # name. The device also holds a static endpoint, so mDNS is a
        # convenience — never let it take the process down.
        log("mDNS registration failed, continuing with static endpoints:", e)
    log("mDNS advertised; waiting for the Echo")
    asyncio.create_task(responder())
    log("responder armed (say 'Hermes ...')")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
