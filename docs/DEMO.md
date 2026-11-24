# Demo — 75 seconds, no talking head required

Shoot it in one take on a phone if you like. Three things have to be true on
camera: the device is a stock-looking Dot, the button does something audible, and
the answers are useful rather than a parlor trick.

## Before you record

- Dot on the kitchen counter, laptop out of frame or visibly to the side.
- Phone on a stand, landscape, 30–60 cm from the Dot so the speaker is the audio.
- Close every other source of sound: TV off, phone notifications off.
- Warm the controller first (play one song, then stop) so the first answer isn't
  slowed by model loading.
- Have Brave open on the dashboard, scrolled to *Recently played*.

## Shot list

| # | time | on screen | you say (or caption) |
|---|---|---|---|
| 1 | 0–6 s | The Dot, finger pressing the button, ring turning cyan | *(no VO — just the cue tone and the ring)* |
| 2 | 6–14 s | Dot answers out loud | *"What is the tallest mountain? … Eight thousand eight hundred and forty-nine metres, measured jointly by Nepal and China in 2020."* |
| 3 | 14–18 s | Cut to the dashboard, cursor over the transcript | *"No API key. It read Google's own answer out of a browser I was already signed into."* |
| 4 | 18–30 s | Press button again: "play some mozart" → music starts, keep the mic audio | *"Same button stops the song, listens, and picks up where it left off."* |
| 5 | 30–42 s | Dashboard: drag the bass slider, then *Recently played* | *"Three-band EQ, volume, and everything it played — because it's my speaker, not a subscription."* |
| 6 | 42–52 s | Show the Dot from underneath / a still of the board | *"It's a 2016 Echo Dot. 512 MB of RAM. Amazon stopped giving it features years ago."* |
| 7 | 52–62 s | Dot answering something house-specific ("what time is it", "how's the build going") | *"Everything local except the question itself: speech to text, the voice, the music pipeline."* |
| 8 | 62–75 s | Static end card: repo name + one line + your handle | *"The controller is open source. It's not a jailbreak — I didn't write the unlock, and I don't ship it. I wrote the half that makes the device worth owning."* |

## The one line to get right

> "I didn't make the jailbreak. I made the reason you'd want one."

Say it plainly, once, near the end. It is the sentence that separates this from
the hundred other "I rooted a thing" posts, and it is also the true one: the
unlock is somebody else's work, the voice endpoint is yours.

## If you would rather not film

Screen-record the dashboard plus an audio capture instead: the ring and the voice
are the only things a camera adds. A 40-second capture of *button → question →
answer → music → EQ change* tells the same story.

## The simulation (built)

`demo/simulation.html` replays the whole interaction on one 1280x720 page: idle
ring, button press, the question typing in, thinking, the spoken answer, music
starting, a press cutting it, the dashboard with the EQ moving, and an end card.
Open it in a browser and screen-record it.

```bash
# fullscreen, controls hidden, honest label kept
open demo/simulation.html?record=1
```

- It is labelled **"simulated from recorded sessions"** in the corner and stays
  that way: present it as a replay, never as live hardware.
- Keep the window **visible** while recording. Browsers throttle timers in a
  hidden or minimised tab, so the sequence will crawl if it is behind another
  window.
- 1280x720 scales itself to the window, so record at any size and crop to 16:9.

## Things that will ruin the take

- Answering "what can you do" — it is the least impressive question and every
  demo opens with it. Ask something with a fact in it.
- Music playing under the voice track. Stop it before each spoken turn (the
  button does this for you).
- Letting the laptop speakers be the audio source. The whole point is the Dot.
- Cutting too fast. Leave the 1.2 s of silence after each answer in — it is what
  makes it feel like a conversation rather than a command.
