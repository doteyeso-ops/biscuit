# Legal notes — read before you publish or ship this

Not legal advice. It is a plain statement of what this repository contains, what
it deliberately does **not** contain, and where the risk sits, so that a reader
(or a lawyer) can check the reasoning instead of taking my word for it.

## 1. What is in this repository

Only original controller code, written for this project, plus documentation.

It contains **no** exploit, **no** bootloader payload, **no** encryption keys,
**no** Amazon firmware, **no** Amazon binaries, and **no** device images. There
is nothing here that circumvents a technical protection measure.

## 2. What is not in this repository, and why

Unlocking the Dot requires bypassing a signed bootloader. In the United States
that is the kind of act the DMCA's anti-circumvention provision (§1201) governs;
similar rules exist in the EU (InfoSoc Directive Art. 6) and elsewhere. Shipping
a tool that does it is a materially different risk from *documenting* that
someone else's tool exists.

So the split is deliberate:

- **The unlock is the user's own act**, performed with third-party tools that
  they install themselves — `firebreak` (MIT) for the bootROM unlock, and
  EchoMuse's emOS payload for the userspace. Both live in their own repositories
  under their own licences and their own authors' responsibility.
- **This repository starts after that** and speaks the documented protocol of
  the resulting device. It is a client, not a circumvention device.

If you are ever asked "does this project break the lock?", the honest answer is
no — it assumes a device that is already open, and it neither performs nor
automates the opening.

## 3. Trademarks

"Amazon", "Echo", "Alexa" and "Echo Dot" are trademarks of Amazon Technologies,
Inc. This project is **not affiliated with, endorsed by, or sponsored by
Amazon**. The names are used only descriptively, to say which piece of hardware
the software runs on — nominative use — and never in the project's own name, its
logo, its domain or its package identifier. The repository is called
`biscuit` (the device's internal codename), not "Echo anything".

If you fork this and rename it, keep that rule: do not put an Amazon mark in the
name or the branding. Saying "works with a rooted Echo Dot 2" in prose is fine;
calling your product "EchoBrain" is not.

## 4. Your hardware, your warranty

Rooting voids the warranty and can permanently brick the device. There is no
recovery path that does not involve opening the case and shorting a test point.
Every instruction in this repository assumes you accept that, on hardware you
own, with no expectation of support from anyone.

## 5. Music and search lanes — terms of service, not copyright

Two optional lanes reach third-party services:

- **Music** resolves a song through `yt-dlp`, which extracts the audio-only
  stream of the matching video. This repository distributes no media and stores
  none; it fetches at your request, the way a browser would. That said, YouTube's
  Terms of Service do not permit extraction of streams outside their player, so
  using this lane is a decision you make about your own account and your own
  jurisdiction. If you want to avoid the question entirely, point the music
  module at local files — the controller only needs PCM on a pipe.
- **Google search answers** are read out of a browser session you start and are
  already signed into, via the DevTools protocol. Automated access to Google
  search results is restricted by their terms. The lane is convenience, not a
  right; disable it with `DOT_DISABLE_GOOGLE=1` and the controller falls back to
  the snippet lane.

Neither of these is a copyright claim against this repository. Both are reasons
a cautious user might turn a lane off, which is why they are switches.

## 6. Privacy

- Audio is captured only during a turn: the mic is opened by an explicit
  command, the device performs its own voice-activity detection, and the turn
  ends on silence.
- Transcription runs **locally** (`faster-whisper`, on your machine).
- The **text** of your question leaves the machine only to whichever answer lane
  you enable — that is the same exposure as typing it into a search box.
- Nothing is uploaded to this project. There is no telemetry, no analytics and
  no account.

## 7. No warranty, no liability

The software is MIT licensed and provided as is. If it deletes a file, plays the
wrong song at 2 a.m. or sets your kitchen on fire, the licence's warranty
disclaimer is the whole of the agreement.

## 8. If someone asks you to change this

The three things that should stay true in any public release: **no circumvention
code**, **no Amazon marks in the name**, **no bundled third-party IP**. Everything
else is negotiable.
