# biscuit-voice

A self-hosted voice endpoint for a first-generation-rooted **Echo Dot 2 (2016)**.
Press the button, ask a question, get an answer out of the same little speaker —
plus music, a web dashboard and no subscription.

This repository is the **controller**: the software on your computer that the
Dot talks to. It is not a jailbreak and it is not a firmware image.

```
 you ──press──▶ Dot ──16 kHz mic──▶ controller ──▶ answer lanes ──▶ TTS ──▶ Dot speaker
                                     │
                                     ├─ Google "AI Overview"  (via your browser, no API key)
                                     ├─ DuckDuckGo + a model (fallback)
                                     └─ Hermes agent         (house, clock, tools)
```

## What it does

- **Push to talk.** The action button opens a bounded, VAD-gated mic turn. The
  device ends the turn itself (`0x04` spoken / `0x05` no speech) with a silence
  fallback, so it never records the room indefinitely.
- **Answers out loud** in two sentences, generated for speech — no markdown, no
  URLs read aloud, no "I'd be happy to".
- **Free answer lanes**, in order: Google's own AI Overview from your signed-in
  browser, then search snippets plus a model, then a full agent with tools.
- **Music** with no ads in the stream: `yt-dlp` takes the audio-only track, so
  there is no ad break to skip in the first place.
- **Dashboard** at `http://127.0.0.1:8767/ui` — now playing, search and play,
  volume, 3-band EQ, recently played, and toggles for follow-ups and wake word.
- **Survives itself.** A supervisor restarts the controller if it exits, and the
  Dot redials on its own.

## Requirements

| piece | why |
|---|---|
| an Echo Dot 2 (2016), already unlocked | this repo starts where the unlock ends |
| [EchoMuse](https://github.com/wilbowes/EchoMuse) on the device | the emOS userspace + device client |
| Python 3.11+, `ffmpeg` on PATH | controller, audio decode |
| optional: Brave/Chromium | for the Google AI Overview lane |

**Prerequisites are separate projects, by other people, under their own licences.**
You unlock your own hardware with [firebreak](https://github.com/W1CT0R/firebreak),
you flash emOS with EchoMuse, and then this controller plugs into it. See
[NOTICE.md](NOTICE.md) and [docs/LEGAL.md](docs/LEGAL.md) — read the legal note
before you start.

## Install

```bash
git clone <this repo> biscuit-voice && cd biscuit-voice
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Point it at your Dot's controller address — the device dials *you*, so the
controller must be reachable on the LAN:

```bash
export DOT_PYTHON=$(which python)        # interpreter the supervisor should use
./scripts/run_controller.sh              # or: python supervise.py
```

Then open <http://127.0.0.1:8767/ui>.

## Configuration

Everything comes from the environment; nothing secret is stored in the repo.

| variable | default | meaning |
|---|---|---|
| `DOT_PORT` | `8767` | controller HTTP/WebSocket port |
| `DOT_PYTHON` | current interpreter | what `supervise.py` launches |
| `DOT_ENV_FILE` | unset | optional `.env` holding provider keys |
| `FFMPEG` | `ffmpeg` | ffmpeg binary |
| `VOICE_FOLLOWUPS` | `0` | extra turns after one press |
| `MUSIC_GAIN_DB` | `0` | pre-guard gain for music |
| `ALWAYS_ON` | `0` | wake-word listening instead of the button |

The dashboard writes `dot_config.json` (EQ, toggles) and `music_history.json`
beside the controller. Both are gitignored.

## See it without the hardware

`demo/simulation.html` — a 60-second replay of the real interaction (labelled as
a simulation): press, question, answer, music, dashboard. Open it in a browser.

## Status

Working end to end on a Dot 2 running emOS: button turns, spoken answers, music,
dashboard. Known rough edges are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#known-limits).

## Credits

The device side is not mine: **EchoMuse** (MIT, Wil Bowes) provides emOS and the
device client, **firebreak** performs the unlock, and this controller speaks the
protocol they document. Music extraction is **yt-dlp**; speech is
**faster-whisper** and **Piper**; the web layer is **aiohttp**.

## Licence

MIT for the code in this repository — see [LICENSE](LICENSE).
