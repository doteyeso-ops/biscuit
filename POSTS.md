# Post drafts — not published, not scheduled

Voice rules this is written to: lead with the artifact, not the feelings; numbers
instead of adjectives; one dry joke maximum; ask once, specifically, at the end.
Nothing here says "passionate", "journey", "grind" or "let me know what you think".

Fill in `[DAY N]`, `[NAME]` and `[REPO]` before posting.

---

## A. X / Twitter thread

**1/**
Day [DAY N] without a job.
Day 1 of my kitchen assistant not needing Amazon.

2016 Echo Dot → unlocked → my own controller. Press the button, ask, it answers
out of the same speaker. Album: 🧵

**2/**
What it does that the original doesn't: no account, no subscription, no wake word
listening to the room, and it plays music with no ad break — not blocked, never
fetched.

**3/**
The honest split: I didn't write the jailbreak. firebreak and EchoMuse do the
unlock and the userspace; I wrote the half that makes the device worth owning —
the controller, the voice loop, the music pipeline, the dashboard.

**4/**
Numbers, since they're the only thing worth posting:

· mic → local Whisper: 0.9 s
· answer → first spoken word: 0.44 s (sentence-streamed TTS)
· Google's own AI Overview read out of a browser I'm already signed into. No API key.
· music: mono 4096-byte periods at 42.67 ms, 0 underruns

**5/**
Music detail I liked: yt-dlp takes the audio-only stream, so there is no ad
break in the pipeline at all. (YouTube ToS does not love this. It's a switch in
the repo, and local files work too.)

**6/**
The ugly part, for people who like that part:

· the mic stays closed until the controller sends `mic_start lock_mic`
· the device buffers ~5.5 s of music — flush the *music* plane, not just the speaker
· ffmpeg piped through a thread pool means playback silently produces nothing when a search hangs

**7/**
It's a Dot 2: 512 MB of RAM, 2019 hardware, doing more now than the day I bought it.

Repo goes public when I've stopped finding bugs in it — a few days. [REPO]

I'm looking for embedded / voice / AI-tooling work, or hardware someone wants
turned into something useful. DMs open.

---

## B. LinkedIn

**I gave a 2016 Echo Dot a second life, and it taught me more than the last year of job hunting.**

The device was end-of-life: 512 MB of RAM, no new features coming, a voice
assistant owned by someone else's cloud.

It now runs a voice endpoint I wrote. Press the button, ask it something, and it
answers out loud in about four seconds. It plays music with no ad break in the
stream, it keeps a dashboard with an equaliser and what it played recently, and
it needs no account and no API key.

What I actually did: I wrote the controller — the protocol client, the turn loop,
the answer routing, the music pipeline, the local speech-to-text and TTS, and a
small web UI. I did not write the bootloader unlock; that's other people's work
under their own licences, and the repository deliberately ships none of it.

The engineering was mostly debugging things that fail silently: a microphone that
never opens unless you ask for it, an audio buffer that keeps playing for five
seconds after you stop sending, a thread pool that starves playback when a web
search hangs. Each one looked like "it's broken" and turned out to be a number I
hadn't measured yet.

It's open source (MIT) once I've shaken out the last rough edges. If you're
hiring for embedded, voice or agent-tooling work — or you have hardware that
should be doing something more useful — I'd like to talk.

---

## C. Show HN (if you want the technical crowd)

**Show HN: A self-hosted voice assistant on a rooted 2016 Echo Dot**

I unlocked an old Dot 2 and wrote the controller for it: press the button, ask a
question, get a spoken answer from a local pipeline, plus music and a small
dashboard.

Deliberately not included: the unlock. That's firebreak and EchoMuse (both
upstream, both MIT or their own licence) — this repo is the client that talks to
the resulting device, and it contains no circumvention code. Read the legal note
in the repo before asking.

The parts that were interesting to build:

- A bounded mic turn (`mic_start lock_mic`) with device-side VAD, because a
  continuous stream transcribes the whole room.
- Sentence-streamed TTS: Piper synthesises at roughly 0.8× realtime, so waiting
  for a whole answer is eight seconds of dead air; per-sentence it's 0.44 s to
  the first word.
- Answer routing by utterance shape: music intents, trivial acknowledgements
  ("ok", "bye") which otherwise get answered by a dictionary, house/tool requests
  to an agent, everything else to a search lane.
- Music as mono 4096-byte periods at the device's own period size — stereo plays
  at double rate, and sub-period reads sound like distortion rather than failing.

Known limits: one Dot 2 on emOS, no far-field wake word by default, the Google
lane needs a browser you've started with a debugging port, and music via yt-dlp
carries the ToS caveat that comes with it.

Happy to answer anything about the protocol.

---

## D. What not to say

- "I built an Alexa" — you didn't, and it invites a trademark argument.
- "Jailbreak tutorial" — this repo isn't one, and saying so attracts the wrong
  audience and the wrong lawyers.
- "Unemployed and desperate" — state the situation once, then talk about the
  work. The work is the argument; the situation is context.
- "Day [N]" *every single post.* It's a hook, not a format. Use it on the big
  ones; let the technical posts stand alone or the series becomes the story.
- Anything with "revolutionary", "game-changing", "10x". The numbers already say
  it better.

## E. Cadence, if you want one

1. **Day 1** — the thread above, with the 75-second video.
2. **+2 days** — one technical post: "the mic stays closed until you ask" with the
   20-line snippet. This is the one engineers will actually share.
3. **+4 days** — the repo, public, with the honest limitations list at the top.
4. Then stop promoting it and start using it in replies. Job leads come from the
   people who ask a follow-up question, not from the reach of the first post.
