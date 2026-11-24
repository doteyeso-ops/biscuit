"""Conversational turn engine for the Echo Dot endpoint.

One consumer owns the mic queue (the responder in the controller). While a
button turn is capturing, the responder hands chunks here instead of running
its own VAD - that was the bug that made the first button press capture
nothing at all.

A turn is:
    capture (adaptive: starts on speech, ends after quiet)
    -> transcribe
    -> ask Hermes with a spoken-answer instruction
    -> speak a markdown-free, length-bounded reply
    -> optional follow-up window so the conversation can continue hands-free
"""
import math
import re
import struct
import time

SR = 16000
SPEECH_RMS = 700
END_SILENCE_S = 1.1
MAX_TURN_S = 9.0
FOLLOW_UP_S = 6.0
MAX_TURNS = 3

CAPTURE = {"on": False, "buf": bytearray(), "started": 0.0}

# The agent writes for a screen by default; voice needs short, plain sentences.
SPOKEN_INSTRUCTION = (
    "You are Hermes Dot, speaking out loud through a small smart speaker in a "
    "kitchen. The person talking is an adult household member unless they say "
    "otherwise - never ask for a grown-up, never ask permission, never treat a "
    "normal question as a child's request. Answer the way a person would say "
    "it: at most two short sentences, no markdown, no lists, no code, no URLs "
    "read aloud, no file paths, times and numbers spoken naturally. If the "
    "request is a task, say what you did or that you started it. "
    "Request: "
)


def feed(chunk: bytes):
    """Called by the controller's responder for every mic chunk while capturing."""
    CAPTURE["buf"] += chunk


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    s = struct.unpack("<%dh" % n, pcm[:n * 2])
    return math.sqrt(sum(v * v for v in s) / n)


async def begin_capture(asyncio_mod, max_s=MAX_TURN_S, end_silence=END_SILENCE_S,
                        min_speech_s=0.45):
    """Turn capture on and wait for the speaker to stop talking.

    min_speech_s is deliberately not zero: a short blip (the button cue tone
    leaking back through the speaker into the mic) is not a spoken turn.
    """
    CAPTURE["on"] = True
    CAPTURE["buf"] = bytearray()
    CAPTURE["started"] = time.monotonic()
    spoke = False
    quiet = 0.0
    voiced = 0.0
    try:
        while time.monotonic() - CAPTURE["started"] < max_s:
            await asyncio_mod.sleep(0.15)
            tail = bytes(CAPTURE["buf"][-4800:])          # last ~150 ms
            level = _rms(tail)
            if level >= SPEECH_RMS:
                voiced += 0.15
                if voiced >= min_speech_s:
                    spoke = True
                quiet = 0.0
            elif spoke:
                quiet += 0.15
                if quiet >= end_silence:
                    break
            else:
                voiced = 0.0
    finally:
        CAPTURE["on"] = False
    return bytes(CAPTURE["buf"]), spoke


def spoken_text(reply: str, limit=300) -> str:
    """Turn a screen answer into something worth hearing."""
    t = reply or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", t, flags=re.M)
    t = re.sub(r"^[ \t]*[-*+][ \t]+", "", t, flags=re.M)
    t = re.sub(r"^[ \t]*\d+\.[ \t]+", "", t, flags=re.M)
    t = t.replace("**", "").replace("__", "").replace("*", "")
    # punctuation a speaker reads badly
    t = t.replace("→", " to ").replace("—", ", ").replace("–", ", ")
    t = re.sub(r"\(([^)]{0,60})\)", r", \1,", t)
    t = t.replace(" ;", ";").replace(" ,", ",")
    t = re.sub(r",\s*,", ",", t)
    t = t.replace("\\", "")
    t = re.sub(r"\s*\n\s*", ". ", t)
    t = re.sub(r"\.{2,}", ".", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .,") + "."
    if len(t) > limit:
        cut = t[:limit]
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = cut[:stop + 1] if stop > 120 else cut.rsplit(" ", 1)[0] + "."
    return t


async def converse(*, asyncio_mod, log, ring, transcribe, ask, speak,
                   text=None, pcm=None, follow_up=True):
    """Ask -> speak -> hands-free follow-up, until they stop talking."""
    turns = 0
    if text is None:
        if not pcm:
            ring("off")
            return
        ring("thinking")
        text = await asyncio_mod.to_thread(transcribe, pcm)
        log(f"heard: {text!r}")
    while True:
        if not text or len(text) < 2:
            ring("off")
            return
        turns += 1
        reply = await asyncio_mod.to_thread(ask, SPOKEN_INSTRUCTION + text)
        log(f"turn {turns} reply: {reply[:200]!r}")
        await speak(spoken_text(reply))
        if not follow_up or turns >= MAX_TURNS:
            ring("off")
            return
        ring("listening")
        pcm, spoke = await begin_capture(asyncio_mod, max_s=FOLLOW_UP_S,
                                         end_silence=1.4)
        if not spoke:
            ring("off")
            return
        ring("thinking")
        text = await asyncio_mod.to_thread(transcribe, pcm)
        log(f"turn {turns + 1} heard: {text!r}")


async def turn(*, asyncio_mod, log, ring, transcribe, ask, speak,
               follow_up=True):
    """Run one full conversational turn from the action button."""
    pcm, spoke = await begin_capture(asyncio_mod)
    if not spoke or len(pcm) < SR * 2 * 0.3:
        log("turn: nothing said")
        ring("error")
        await asyncio_mod.sleep(0.6)
        ring("off")
        return
    await converse(asyncio_mod=asyncio_mod, log=log, ring=ring,
                   transcribe=transcribe, ask=ask, speak=speak,
                   pcm=pcm, follow_up=follow_up)
