# How it works

Three processes, one protocol, and a rule about who is allowed to do what.

```
┌────────────┐  /control  (JSON: buttons, ring, volume, mic_start/stop)
│ Echo Dot 2 │◀───────────────────────────────────────────────┐
│  emOS +    │  /data     (binary: 0x01 mic →  controller)    │
│  client    │◀───────────── 0x04 music, 0x02 voice ──────────┤
└────────────┘  /shell    (root shell plane, on demand)       │
                                                              │
                                              ┌───────────────┴──────────────┐
                                              │  controller (this repo)      │
                                              │  aiohttp: 8767 + mDNS        │
                                              └───────────────┬──────────────┘
                                                              │
        ┌──────────────────────┬──────────────────────────────┼───────────────────┐
        ▼                      ▼                              ▼                   ▼
  Google AI Overview     snippets + model              Hermes agent          music
  (user's browser, CDP)  (DuckDuckGo + provider)       (tools, house)        (yt-dlp → ffmpeg)
```

## The turn

1. **Button press** arrives on `/control` as
   `{"type":"button","clickType":138,"down":false,"heldMs":208,"muted":false}`.
2. Music is stopped and **both** planes flushed — `music_flush` and
   `speaker_flush`. The device buffers several seconds of music; flushing only the
   speaker plane leaves the tail audible inside the turn.
3. A soft cue plays and the ring turns **cyan**, then:

   ```json
   {"type": "mic_start", "lock_mic": true}
   ```

   The microphone is closed until this arrives. `lock_mic` asks for a *bounded,
   VAD-gated* turn rather than the always-on wake stream.
4. The device streams 16 kHz mono S16 on `/data`, and ends the turn itself:
   `0x04` = spoke, then silence; `0x05` = no speech arrived. A 1.2 s gap in the
   stream is treated as the end too, because the sentinel is not reliably sent.
5. Whisper transcribes locally (~0.9 s warm). The controller decides the lane:

   | utterance | lane |
   |---|---|
   | "play …", "put on …", "I want to hear …" | music |
   | "ok", "thanks", "bye" | ignored |
   | house, laptop, tools, clock, "my …" | agent |
   | anything else | Google, then snippets |

6. The answer is shaped for speech — two sentences, no markup, no URLs — and
   synthesised **sentence by sentence**, so audio starts as soon as the first
   sentence is ready rather than after the whole reply.

## Design decisions that were paid for in blood

- **The wire is mono on both planes.** The device converts mono to the stereo its
  codec needs (`toStereo()` in `pcm_speaker.go`). Send stereo and it plays at
  double rate.
- **One period per frame.** Music: 2048 frames = 4096 bytes at 42.67 ms. Read the
  pipe with an exact-length read; short reads put every later period out of
  phase and sound like distortion rather than an error.
- **Pace against a deadline, not a sleep.** `sleep(0.033)` per frame drifts to
  0.71x realtime, which starves the buffer and warbles.
- **Loudness is analogue.** The device's music guard is a fixed-target AGC: more
  input just gets more reduction (32 dB → 40 dB cap, output unchanged). Volume
  comes from `Ext_Amp_Gain` and `Audio Mrgrx Volume`.
- **Never speak over the start of a song.** Voice and music are separate planes
  and the device *sums* them, saturating rather than wrapping, so a TTS
  confirmation over the intro is audible clipping. The chime is the
  acknowledgement; the confirmation sentence is gone.
- **The output guard ramps.** It reads 0 dB at the start of a stream and settles
  around 28-39 dB once it has measured the material, so a song's first moments
  play far hotter than its steady state -- the "clip that clears itself up". A
  0.5 s fade-in holds the start quiet until the guard catches up.
- **Music runs on an asyncio subprocess.** Reading ffmpeg's pipe through the
  default thread pool stalls the whole playback path when a search hangs. The
  event loop reads it directly.

## Files

| file | what it is |
|---|---|
| `hermes_controller2.py` | the controller: protocol, router, TTS streaming, music, dashboard routes |
| `app/dashboard.py` | settings + history + the `/ui` page |
| `app/music.py` | yt-dlp resolve, ffmpeg decode, exact reads |
| `app/quick.py` | snippet lane (DDG + a provider), with a bounded search |
| `app/webanswer.py` | Google AI Overview over CDP |
| `app/convo.py` | answer shaping for speech |
| `app/tts.py` | Piper with a warm daemon, SAPI fallback |
| `app/ring.py`, `voice.py` | LED states, wake/VAD helpers |
| `supervise.py` | restarts the controller when it exits |

## Known limits

- The button is the only input; there is no far-field wake word in the default
  build (the flag exists, but a continuous stream transcribes the room).
- One speaker: music and voice share it, so a turn always interrupts playback.
- The Google lane needs a browser running with `--remote-debugging-port=9222`.
  Without it, answers come from the snippet lane.
- Scope is a Dot 2 on emOS. Other boards announce different capabilities and are
  untested here.
