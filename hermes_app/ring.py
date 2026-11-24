#!/usr/bin/env python
"""ring.py — reusable LED-ring state renderer for the EchoMuse Echo Dot.

The Echo Dot (2nd gen, "biscuit") has twelve addressable LEDs and no screen, so
every state it can be in has to be expressible as a ring pattern.  This module
owns that vocabulary: it turns a *state name* into the JSON frame the device
understands and posts it to the running Hermes controller, which forwards it
verbatim down the device's /control WebSocket.

Two device-side message shapes are used (see device/internal/server/animator.go
and device/internal/client/control.go):

  {"type": "leds",     "leds": [{"id": 0, "r": .., "g": .., "b": ..}, ...],
                       "listening": false}

      Static frame.  All twelve ids are always sent because a new leds frame
      *replaces* the previous one — omitting an id would leave it unset, and
      stale pixels are worse than dark ones.

  {"type": "led_anim", "anim": {"pattern": "spin",
                                 "colors": [[r, g, b], ...],
                                 "periodMs": 80,
                                 "listening": false,
                                 "ttlSec": 25}}

      Device-rendered animation.  Patterns: off, solid, spin, rotate, pulse,
      meter.  The device animates on its own ticker, so smoothness does not
      depend on controller scheduling.  `ttlSec` is a dead-man switch: if no
      newer frame arrives the device clears the ring itself, which protects
      against a controller that dies mid-turn.

Usage as a module::

    from ring import RingRenderer, render
    r = RingRenderer()                 # 127.0.0.1:8767
    r.send_state("listening")
    r.send_payload(render("volume", level=7))

Usage as a CLI::

    python ring.py list
    python ring.py listening
    python ring.py thinking --ttl 60
    python ring.py volume 7
    python ring.py color 255 0 0
    python ring.py raw '{"type":"leds","leds":[]}'
    python ring.py demo

Every command prints the exact payload it sent plus the controller's HTTP
status and body, so output can be used as evidence of what the device received.

Stdlib only — no third-party dependency, because this has to run on whatever
Python happens to be on the box next to the controller.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

__all__ = [
    "LED_COUNT",
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT",
    "STATES",
    "Led",
    "RingRenderer",
    "arc_frame",
    "black",
    "leds_payload",
    "anim_payload",
    "render",
]

LED_COUNT = 12
DEFAULT_ENDPOINT = "http://127.0.0.1:8767"
DEFAULT_TIMEOUT = 10.0

Led = "tuple[int, int, int]"

# Named colours. These are the palette the firmware docs use, so a colour that
# appears here also appears in docs/led-ring-states.md with the same meaning.
BLACK = (0, 0, 0)
CYAN = (0, 180, 255)      # listening (Alexa's own listening colour)
BLUE = (0, 90, 255)       # thinking trail
VIOLET = (150, 70, 255)   # identify
RED = (180, 0, 0)         # muted — solid, device-sovereign
ORANGE = (255, 120, 0)    # controller unreachable
WHITE = (255, 255, 255)   # awaiting approval
GREEN = (0, 200, 80)      # success / acknowledged
AMBER = (255, 170, 0)     # notification


def black() -> list[tuple[int, int, int]]:
    """A dark ring, as a list of twelve colours."""
    return [BLACK] * LED_COUNT


def arc_frame(level: int, color: tuple[int, int, int] = CYAN) -> list[tuple[int, int, int]]:
    """Volume arc: `level` of twelve LEDs lit clockwise from id 0.

    Mirrors the device's own volume display (volume.go), which lights a
    proportional arc rather than a number.
    """
    level = max(0, min(LED_COUNT, int(level)))
    return [color if i < level else BLACK for i in range(LED_COUNT)]


def _expand(colors, pad: bool = True) -> list[tuple[int, int, int]]:
    """Normalise a colour argument to exactly twelve entries.

    * one colour   -> fills the whole ring
    * N colours    -> mapped per-LED; the remainder stay dark
    * twelve       -> used 1:1

    `pad=False` keeps the list as given — `rotate` cycles through its palette
    modulo its own length, so padding it with black would insert dark gaps.
    """
    colors = list(colors)
    if not colors:
        return black()
    if len(colors) == 1:
        return [tuple(colors[0])] * LED_COUNT
    if not pad:
        return [tuple(c) for c in colors]
    out = [tuple(c) for c in colors[:LED_COUNT]]
    out.extend([BLACK] * (LED_COUNT - len(out)))
    return out


def leds_payload(colors, listening: bool = False) -> dict:
    """Build a static `leds` frame. Always carries all twelve LEDs."""
    frame = _expand(colors)
    return {
        "type": "leds",
        "listening": bool(listening),
        "leds": [
            {"id": i, "r": int(c[0]), "g": int(c[1]), "b": int(c[2])}
            for i, c in enumerate(frame)
        ],
    }


def anim_payload(
    pattern: str,
    colors,
    period_ms: int = 0,
    listening: bool = False,
    ttl_sec: int = 0,
    **meter,
) -> dict:
    """Build a device-rendered `led_anim` frame.

    `meter` accepts the optional response-curve keys the firmware understands
    (attack, decay, floor, gamma, ref, curve) for the `meter` pattern only.
    """
    anim = {
        "pattern": pattern,
        "colors": [
            [int(c[0]), int(c[1]), int(c[2])]
            for c in _expand(colors, pad=(pattern != "rotate"))
        ],
        "periodMs": int(period_ms),
        "listening": bool(listening),
        "ttlSec": int(ttl_sec),
    }
    for key in ("attack", "decay", "floor", "gamma", "ref", "curve"):
        if key in meter and meter[key] is not None:
            anim[key] = float(meter[key])
    return {"type": "led_anim", "anim": anim}


# ── the state vocabulary ────────────────────────────────────────────────────
# Each entry is a callable taking keyword options and returning a payload.
# TTLs follow docs/led-ring-states.md §2/§4: every provisional state carries a
# deadline so a dead controller cannot leave the ring lying indefinitely.
STATES: dict[str, dict] = {
    "off": {
        "help": "ring dark; idle",
        "make": lambda **o: leds_payload(black()),
    },
    "listening": {
        "help": "solid scene colour, direction overlay armed",
        "make": lambda ttl=30, **o: anim_payload(
            "solid", [o.get("color") or CYAN], listening=True, ttl_sec=ttl
        ),
    },
    "thinking": {
        "help": "spinner: head+trail around the ring",
        "make": lambda ttl=135, **o: anim_payload(
            "spin", [o.get("color") or CYAN, BLUE], period_ms=80, ttl_sec=ttl
        ),
    },
    "speaking": {
        "help": "meter: brightness follows live speaker RMS",
        "make": lambda ttl=135, **o: anim_payload(
            "meter", [o.get("color") or CYAN], ttl_sec=ttl
        ),
    },
    "muted": {
        "help": "solid red (device mute itself is sovereign and wins)",
        "make": lambda **o: leds_payload([o.get("color") or RED]),
    },
    "disconnected": {
        "help": "orange sine pulse: controller unreachable",
        "make": lambda **o: anim_payload(
            "pulse", [o.get("color") or ORANGE], period_ms=2000
        ),
    },
    "pending": {
        "help": "slow white pulse: awaiting approval",
        "make": lambda **o: anim_payload(
            "pulse", [o.get("color") or WHITE], period_ms=3000
        ),
    },
    "identify": {
        "help": "violet pulse: which device is this",
        "make": lambda ttl=30, **o: anim_payload(
            "pulse", [o.get("color") or VIOLET], period_ms=600, ttl_sec=ttl
        ),
    },
    "error": {
        "help": "red pulse: turn failed",
        "make": lambda ttl=15, **o: anim_payload(
            "pulse", [o.get("color") or RED], period_ms=800, ttl_sec=ttl
        ),
    },
    "success": {
        "help": "solid green acknowledgement, self-clears",
        "make": lambda ttl=3, **o: anim_payload(
            "solid", [o.get("color") or GREEN], ttl_sec=ttl
        ),
    },
    "notify": {
        "help": "amber spinner: something wants attention",
        "make": lambda ttl=20, **o: anim_payload(
            "spin", [o.get("color") or AMBER, RED], period_ms=120, ttl_sec=ttl
        ),
    },
    "pride": {
        "help": "palette rotating around the ring",
        "make": lambda ttl=10, **o: anim_payload(
            "rotate",
            [(255, 0, 0), (255, 120, 0), (255, 255, 0),
             (0, 200, 0), (0, 120, 255), (140, 0, 255)],
            period_ms=100, ttl_sec=ttl,
        ),
    },
    "volume": {
        "help": "cyan arc of N LEDs (needs --level 0..12)",
        "make": lambda level=6, **o: leds_payload(
            arc_frame(level, o.get("color") or CYAN)
        ),
    },
    "color": {
        "help": "flat custom colour (needs --color R G B)",
        "make": lambda **o: leds_payload([o.get("color") or WHITE]),
    },
    "raw": {
        "help": "pass a JSON payload through untouched (needs --raw)",
        "make": lambda **o: json.loads(o["raw"]),
    },
}


def render(state: str, **options) -> dict:
    """Turn a state name into a payload dict. Raises KeyError on unknown state."""
    if state not in STATES:
        raise KeyError(
            f"unknown state {state!r}; try one of: {', '.join(sorted(STATES))}"
        )
    return STATES[state]["make"](**options)


# ── transport ───────────────────────────────────────────────────────────────
class RingRenderer:
    """Posts ring frames to the Hermes controller's local HTTP surface.

    The controller exposes POST /_send, which forwards the body verbatim to the
    device's /control WebSocket, and answers `ok`.  A 503 there means no device
    is attached, so a 200 is a real end-to-end delivery, not just a local
    accept — that is what makes it usable as proof.
    """

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = DEFAULT_TIMEOUT):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def send_payload(self, payload: dict) -> tuple[int, str]:
        """POST one payload. Returns (http_status, response_body)."""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/_send",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace").strip()
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"cannot reach controller at {self.endpoint}: {e.reason}"
            ) from e

    def send_state(self, state: str, **options) -> tuple[int, str]:
        return self.send_payload(render(state, **options))

    def say(self, text: str) -> tuple[int, str]:
        """Make the Dot speak; handy for correlating a ring change with a moment."""
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/_say",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace").strip()


def _ok(status: int, body: str) -> bool:
    return status == 200 and body.strip() == "ok"


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print_states() -> None:
    width = max(len(n) for n in STATES)
    print("States:\n")
    for name in sorted(STATES):
        print(f"  {name.ljust(width)}  {STATES[name]['help']}")
    print("\nPatterns the device renders locally: off, solid, spin, rotate, pulse, meter")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ring.py",
        description="Render an Echo Dot LED-ring state to the live device.",
        epilog="Examples: ring.py thinking --ttl 60 | ring.py volume 7 | ring.py list",
    )
    p.add_argument("state", nargs="?",
                   help="state name (see `ring.py list`), or 'list' / 'demo'")
    p.add_argument("level_pos", nargs="?", type=int, metavar="LEVEL",
                   help="shorthand for --level (e.g. `ring.py volume 7`)")
    p.add_argument("--level", type=int, default=None, help="volume state: LEDs lit 0..12")
    p.add_argument("--color", type=int, nargs=3, metavar=("R", "G", "B"),
                   help="override the state's colour, 0..255")
    p.add_argument("--pattern", help="raw led_anim pattern override")
    p.add_argument("--period", type=int, default=0, help="led_anim periodMs")
    p.add_argument("--ttl", type=int, default=None, help="led_anim ttlSec dead-man")
    p.add_argument("--listening", action="store_true",
                   help="mark a static frame as listening (enables direction overlay)")
    p.add_argument("--raw", help="JSON payload to send verbatim")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="controller base URL")
    p.add_argument("--dry-run", action="store_true", help="print payload, send nothing")
    p.add_argument("--quiet", action="store_true", help="print only the result line")
    args = p.parse_args(argv)

    if args.state is None:
        p.print_help()
        return 0

    if args.state in ("list", "states", "--list"):
        _print_states()
        return 0

    color = tuple(args.color) if args.color else None
    level = args.level if args.level is not None else (
        args.level_pos if args.level_pos is not None else 6
    )
    opts: dict = {"color": color, "level": level, "raw": args.raw}
    if args.ttl is not None:
        opts["ttl"] = args.ttl

    def emit(state: str, **extra) -> int:
        o = dict(opts)
        o.update(extra)
        try:
            payload = render(state, **o)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as e:
            print(f"error: --raw is not valid JSON: {e}", file=sys.stderr)
            return 2

        if args.pattern:  # escape hatch: any pattern, this module's palette rules
            payload = anim_payload(
                args.pattern,
                [color or CYAN],
                period_ms=args.period,
                listening=args.listening,
                ttl_sec=args.ttl or 0,
            )
        elif args.listening and payload.get("type") == "leds":
            payload["listening"] = True

        text = json.dumps(payload, separators=(",", ":"))
        if args.dry_run:
            print(text)
            return 0

        r = RingRenderer(args.endpoint)
        try:
            status, body = r.send_payload(payload)
        except ConnectionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3

        if not args.quiet:
            print(f"payload: {text}")
        mark = "ok  " if _ok(status, body) else "FAIL"
        print(f"{mark} POST {args.endpoint}/_send -> {status} {body!r}  [{state}]")
        return 0 if _ok(status, body) else 1

    if args.state == "demo":
        seq = ["listening", "thinking", "speaking", "success", "notify", "error", "off"]
        rc = 0
        for s in seq:
            rc |= emit(s)
            time.sleep(0.8)
        return rc

    if args.raw:
        return emit("raw")

    return emit(args.state)


if __name__ == "__main__":
    sys.exit(main())
