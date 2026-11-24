"""Music for the Dot: yt-dlp picks the audio stream, ffmpeg decodes it to the
speaker's native format, and the controller streams it.

Ads are not blocked, they are never fetched: yt-dlp asks for the audio-only
stream of the song itself, so there is no ad break, no preroll and no sponsor
segment in the pipeline at all.
"""

import asyncio
import os
import shutil
import subprocess

FFMPEG = shutil.which("ffmpeg") or (
    r"C:\Users\you\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
)

SR = 48000
CHANNELS = 1          # The WIRE is mono S16LE on both planes; the device's
                      # toStereo() converts to the stereo the ALSA device needs
                      # (pcm_speaker.go:787). Sending stereo doubles the rate
                      # and sounds torn.
PERIOD = 2048         # device periodSize, in frames
FRAME = PERIOD * CHANNELS * 2      # 4096 bytes = one mono period on the wire
FRAME_S = PERIOD / SR              # 42.67 ms


def resolve(query):
    """Return (title, direct_audio_url) for the best match of `query`."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "noplaylist": True,
        # Prefer a real bitrate over "whatever is smallest": bestaudio can be a
        # thin 48 kbps opus, which a mono downmix turns hollow.
        "format": "bestaudio[abr>=128]/bestaudio[ext=m4a]/bestaudio/best",
        "format_sort": ["abr", "asr"],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    if info.get("entries"):
        info = info["entries"][0]
    url = info.get("url")
    if not url:
        for f in reversed(info.get("formats") or []):
            if f.get("acodec") not in (None, "none") and f.get("url"):
                url = f["url"]
                break
    if not url:
        raise RuntimeError("no audio stream found")
    return info.get("title", query), url


def read_exact(proc, n):
    """Read exactly n bytes, or fewer only at end of stream.

    A pipe read returns whatever is available, not the count asked for. Sending
    those short chunks put every later period out of phase with the device's
    2048-frame buffer, which is audible as distortion rather than as an error.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = proc.stdout.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# The device's music guard pulls 21-32 dB out of whatever arrives, and the Dot
# is already at max volume, so the only lever left is source level. +6 dB with a
# limiter ahead of it is audibly louder without hard clipping (clipped=0/0).
GAIN_DB = float(os.environ.get("MUSIC_GAIN_DB", "0"))


def _eq_chain():
    """Equaliser bands from the dashboard config, or "" when flat."""
    try:
        import dashboard
        return dashboard.eq_filter()
    except Exception:
        return ""


def _ffmpeg_cmd(url, gain_db=None):
    cmd = [FFMPEG, "-nostdin", "-loglevel", "error", "-i", url, "-vn"]
    g = GAIN_DB if gain_db is None else gain_db
    # The device's output guard resets to 0 dB at the start of every stream and
    # only settles once it has measured the material -- measured 0.00 dB at the
    # start of one stream against 39.07 dB at the end of the previous one. That
    # 20-40 dB jump is the "clip that clears itself up". A half-second fade-in
    # holds the start quiet until the guard has caught up.
    af = ["afade=t=in:st=0:d=0.50"]
    eq = _eq_chain()
    if eq:
        af.append(eq)
    if g:
        af.append(f"volume={g}dB,alimiter=limit=0.95:level=disabled")
    # always keep the fade, even with no EQ and no gain
    cmd += ["-af", ",".join(af)]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", str(CHANNELS), "-ar", str(SR), "pipe:1"]
    return cmd


def open_ffmpeg(url, gain_db=None):
    """Start ffmpeg decoding url to 48 kHz mono s16 on stdout."""
    return subprocess.Popen(_ffmpeg_cmd(url, gain_db), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)


async def open_ffmpeg_async(url, gain_db=None):
    """Same, but as an asyncio subprocess.

    Reading a blocking pipe through the default thread pool stalls the whole
    playback path when the pool is busy (music then produces no frames at all
    while ffmpeg sits running). The event loop reads this one directly.
    """
    return await asyncio.create_subprocess_exec(
        *_ffmpeg_cmd(url, gain_db),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


def read_exact(proc, n):
    """Read exactly n bytes, or fewer only at end of stream.

    A pipe read returns whatever is available, not the count asked for. Sending
    those short chunks put every later period out of phase with the device's
    2048-frame buffer, which is audible as distortion rather than as an error.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = proc.stdout.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# The device's music guard pulls 21-32 dB out of whatever arrives, and the Dot
# is already at max volume, so the only lever left is source level. +6 dB with a
# limiter ahead of it is audibly louder without hard clipping (clipped=0/0).
GAIN_DB = float(os.environ.get("MUSIC_GAIN_DB", "0"))


def _eq_chain():
    """Equaliser bands from the dashboard config, or "" when flat."""
    try:
        import dashboard
        return dashboard.eq_filter()
    except Exception:
        return ""


def open_ffmpeg(url, gain_db=None):
    """Start ffmpeg decoding url to 48 kHz mono s16 on stdout.

    gain_db matters: the device's music output guard pulled our full-scale
    stream down 32.11 dB (guard_reduction=32.11dB, clipped=0/0), which thins
    the sound out. Feeding it quieter asks less of the guard.
    """
    cmd = [FFMPEG, "-nostdin", "-loglevel", "error", "-i", url, "-vn"]
    g = GAIN_DB if gain_db is None else gain_db
    # The device's output guard resets to 0 dB at the start of every stream and
    # only settles once it has measured the material -- measured 0.00 dB at the
    # start of one stream against 39.07 dB at the end of the previous one. That
    # 20-40 dB jump is the "clip that clears itself up". A half-second fade-in
    # holds the start quiet until the guard has caught up.
    af = ["afade=t=in:st=0:d=0.50"]
    eq = _eq_chain()
    if eq:
        af.append(eq)
    if g:
        af.append(f"volume={g}dB,alimiter=limit=0.95:level=disabled")
    # always keep the fade, even with no EQ and no gain
    cmd += ["-af", ",".join(af)]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", str(CHANNELS), "-ar", str(SR), "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)
