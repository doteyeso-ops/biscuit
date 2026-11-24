# ring.py — LED-ring state renderer for the Echo Dot

Turns a **state name** into the exact JSON frame an Echo Dot (2nd gen, "biscuit")
understands, and posts it to the running Hermes controller on
`127.0.0.1:8767`, which forwards it verbatim to the device's `/control`
WebSocket. Stdlib only (Python 3.8+); no third-party packages.

```
app/
  ring.py           module + CLI
  README-ring.md    this file
```

## Why two message shapes

Confirmed from the device source, not guessed:

| Shape | Source of truth | Use |
|---|---|---|
| `{"type":"leds","leds":[{"id":0,"r":..,"g":..,"b":..}, …],"listening":bool}` | `device/internal/client/control.go:816` → `pkg/led` | static frame; **replaces** the whole ring |
| `{"type":"led_anim","anim":{"pattern":…,"colors":[[r,g,b],…],"periodMs":int,"listening":bool,"ttlSec":int}}` | `device/internal/server/animator.go:19` (`AnimSpec`) | device-rendered animation on its own ticker |

`led_anim` patterns the firmware implements: **`off`, `solid`, `spin`, `rotate`,
`pulse`, `meter`**. Anything else is logged device-side as `unknown pattern` and
clears the ring, so this module never emits an unlisted pattern.

Colour semantics per pattern (`animator.go`):

- `solid` — palette painted 1:1: one colour fills the ring, otherwise per-LED.
- `spin` — `[head, trail]`.
- `rotate` — palette rotated one LED per frame (palette length is *not* padded;
  padding it with black would insert dark gaps).
- `pulse` — palette brightness modulated sinusoidally over `periodMs`
  (default 2000ms, 15%–100%).
- `meter` — brightness follows live speaker RMS; `periodMs` ignored (40ms tick).
  Optional curve keys (`attack`, `decay`, `floor`, `gamma`, `ref`, `curve`) are
  pass-through and clamped by the device.

`ttlSec` is a **dead-man switch**: with no newer frame the device clears the
ring itself, so a controller that dies mid-turn cannot leave the ring lying.
Every provisional state here sets one, following `docs/led-ring-states.md` §7.

## States

| State | Visual | Payload kind | TTL |
|---|---|---|---|
| `off` | all twelve LEDs dark | `leds` | — |
| `listening` | solid cyan, `listening:true` (arms direction overlay) | `led_anim solid` | 30s (`--ttl`) |
| `thinking` | cyan head + blue trail spinner, 80ms | `led_anim spin` | 135s |
| `speaking` | cyan meter throbbing with speaker RMS | `led_anim meter` | 135s |
| `muted` | solid red `(180,0,0)` | `leds` | — |
| `disconnected` | orange sine pulse, 2s | `led_anim pulse` | — |
| `pending` | slow white pulse, 3s | `led_anim pulse` | — |
| `identify` | violet pulse, 600ms | `led_anim pulse` | 30s |
| `error` | red pulse, 800ms | `led_anim pulse` | 15s |
| `success` | solid green, self-clears | `led_anim solid` | 3s |
| `notify` | amber/red spinner, 120ms | `led_anim spin` | 20s |
| `pride` | 6-colour palette rotating | `led_anim rotate` | 10s |
| `volume` | cyan arc of N LEDs (positional or `--level`) | `leds` | — |
| `color` | flat custom colour from `--color R G B` | `leds` | — |
| `raw` | any JSON you pass to `--raw`, untouched | pass-through | — |

Caveat on `muted`: the device's own mute layer is **sovereign**
(`docs/led-ring-states.md` §2 layer 2). While the Dot is physically muted the
firmware paints red itself and drops controller frames; this state is the
matching visual for an unmuted device being told "show muted", not a way to
mute anything.

## CLI

```bash
python ring.py                      # help
python ring.py list                 # every state and its meaning
python ring.py listening --ttl 45
python ring.py thinking
python ring.py muted
python ring.py volume 7             # 7 of 12 LEDs lit
python ring.py color --color 255 0 0
python ring.py success --color 0 255 120
python ring.py raw --raw '{"type":"leds","leds":[{"id":0,"r":9,"g":9,"b":9}]}'
python ring.py demo                 # walk listening→thinking→speaking→success→notify→error→off
python ring.py thinking --dry-run   # print the payload, send nothing
python ring.py listening --endpoint http://127.0.0.1:8767 --quiet
```

Override any animation: `--pattern spin --period 40 --ttl 10 --listening`.

Exit codes: `0` sent and acknowledged, `1` controller returned something other
than `200 ok`, `2` bad state / bad JSON, `3` controller unreachable.

Every run prints the exact payload sent and the response:

```
payload: {"type":"led_anim","anim":{"pattern":"spin",...}}
ok   POST http://127.0.0.1:8767/_send -> 200 'ok'  [thinking]
```

## Module

```python
from ring import RingRenderer, render, arc_frame, leds_payload, anim_payload

r = RingRenderer()                    # or RingRenderer("http://127.0.0.1:8767", timeout=5)
r.send_state("listening")             # (200, 'ok')
r.send_state("volume", level=4)
r.send_payload(render("error", ttl=5))
r.send_payload(anim_payload("rotate", [(255,0,0),(0,0,255)], period_ms=60, ttl_sec=10))
r.say("boom")                         # POST /_say
```

`render(state, **opts) -> dict` is pure — useful for tests and for anything that
wants the frame without sending it. `OFF` frames always carry all twelve ids,
because a partial `leds` frame leaves stale pixels lit.

## Verification (2026-11-23, live device)

Sent to the running controller, one second apart, each acknowledged:

| # | Command | Result |
|---|---|---|
| 1 | `ring.py listening --ttl 45` | `200 'ok'` |
| 2 | `ring.py thinking --ttl 45` | `200 'ok'` |
| 3 | `ring.py muted` | `200 'ok'` |
| 4 | `ring.py error` | `200 'ok'` |
| 5 | `ring.py volume 9` | `200 'ok'` |
| 6 | `ring.py off` | `200 'ok'` |

`_send` returns **503 `no device connected`** when no Dot is attached and 500 on
a socket write failure (`hermes_controller2.py:269-278`), so a `200 ok` means
the frame was written to the live `/control` WebSocket — not merely accepted
locally. A negative control against a dead port raised
`ConnectionError: WinError 10061`, confirming the check can actually fail.
`_say` also returned `200 ok` after the ring sequence.

**Not** visually confirmed: this module has no way to see the ring. The
evidence is HTTP status plus the controller's passthrough semantics.
