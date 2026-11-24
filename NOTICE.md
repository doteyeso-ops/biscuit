# Third-party components

This repository contains none of the components below. It depends on them, links
to them, or talks to them over a network protocol. Each keeps its own licence and
its own copyright, and none of them are relicensed here.

| component | role | licence | where it lives |
|---|---|---|---|
| [EchoMuse](https://github.com/wilbowes/EchoMuse) | emOS userspace + device client that speaks the controller protocol | MIT | installed on the device, not vendored here |
| `firebreak` | bootROM unlock for the Dot 2 | see upstream | user-installed, never invoked by this code |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | audio-only stream extraction | Unlicense | pip dependency |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | local speech-to-text | MIT | pip dependency |
| [Piper](https://github.com/rhasspy/piper) | local neural TTS | MIT | pip dependency |
| [aiohttp](https://github.com/aio-libs/aiohttp) | HTTP + WebSocket server | Apache-2.0 | pip dependency |
| [ffmpeg](https://ffmpeg.org) | decode to raw PCM | LGPL-2.1+ / GPL-2+ depending on build | system binary, invoked as a subprocess |

## Notes

- **EchoMuse is the substantial one.** The device-side protocol, the emOS
  userspace and the client binary are Wil Bowes's work under MIT. This project is
  an independent controller for that protocol; it is not a fork and does not
  redistribute the EchoMuse tree. If you use both, the MIT notice from EchoMuse
  travels with *their* files, which is where it already is.
- **ffmpeg is executed, not linked.** Calling the system `ffmpeg` as a subprocess
  keeps this project out of the GPL-vs-LGPL question that statically linking it
  would raise.
- **No media and no model weights are distributed here.** Whisper and Piper
  weights download on first use, from their own projects, under their own terms.
