"""
Interactive probes for the mini keyboard's two LEDs.

  uv run led_probe.py           mode sweep: tries LED modes 0x0-0xF (low nibble of byte 12)
  uv run led_probe.py headers   header-byte tests: can the two LEDs be set independently?
  uv run led_probe.py blink     preview mode 2 (hardware blink) as the swap indicator
  uv run led_probe.py colors    unexplored color nibbles 0x0, 0x8-0xF (looking for a live "off")

Stop isp_switcher.py first, otherwise its 3s LED refresh overwrites the test colors.

Header results (2026-10-28): every variation changed BOTH LEDs, and the color
shows without the commit packet.

Mode results (2026-10-28), low nibble of byte 12:
  0 off | 1 steady | 2 fast blink, both LEDs | 3 press-reactive variant (other LED
  blinks while held, both after release) | 4 pressed key's LED blinks | 5 steady white
  (color ignored) | 6-F rejected (LED keeps its previous state). No static per-key mode.
"""
import sys
import time

from led_controller import LedController, COLOR_CODES

# Known-good packet: 03 FE B0 01 08 00 00 00 00 00 01 00 [color|mode]
BASE = [0x03, 0xFE, 0xB0, 0x01, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00]
COMMIT = [0x03, 0xFD, 0xFE, 0xFF]

# (description, {byte_index: value}, color, send_commit)
HEADER_TESTS = [
    ("baseline: known command, BLUE", {}, 'blue', True),
    ("no commit packet, RED (does packet 2 matter?)", {}, 'red', False),
    ("byte 3 = 0x02, GREEN", {3: 0x02}, 'green', True),
    ("byte 10 = 0x02, GREEN", {10: 0x02}, 'green', True),
    ("byte 11 = 0x01, PURPLE", {11: 0x01}, 'purple', True),
    ("byte 5 = 0x01, ORANGE", {5: 0x01}, 'orange', True),
]

MODE_COLOR = 'blue'  # high nibble stays blue (0x6_) so only the mode varies


def send(led, overrides, color_byte, commit):
    pkt = list(BASE)
    for i, v in overrides.items():
        pkt[i] = v
    with led._lock:
        led._send(pkt + [color_byte])
        if commit:
            time.sleep(0.02)
            led._send(COMMIT)


def run_headers(led):
    results = []
    for i, (desc, overrides, color, commit) in enumerate(HEADER_TESTS, 1):
        # Reset both LEDs to a known state so each test starts clean.
        led.set_color('cyan')
        time.sleep(0.5)
        send(led, overrides, COLOR_CODES[color], commit)
        print(f"\n[{i}/{len(HEADER_TESTS)}] {desc}")
        ans = input("  What changed? (b=both, 1=first only, 2=second only, n=nothing, other=describe): ").strip()
        results.append((desc, ans or 'n'))
    return results


def run_modes(led):
    results = []
    color_nibble = COLOR_CODES[MODE_COLOR] & 0xF0
    for mode in range(0x10):
        # Start each mode from steady CYAN so leftover effects are obvious.
        led.set_color('cyan')
        time.sleep(0.5)
        color_byte = color_nibble | mode
        # Commit in case a mode only takes effect once saved.
        send(led, {}, color_byte, commit=True)
        desc = f"mode 0x{mode:X} (byte 12 = 0x{color_byte:02X}, {MODE_COLOR})"
        print(f"\n[{mode + 1}/16] {desc}")
        print("  Watch a few seconds, then press each of the two keys once.")
        ans = input("  What happened? (e.g. off / steady / breathing / blink / press-reactive / "
                    "per-key / colour cycle / nothing): ").strip()
        results.append((desc, ans or 'nothing'))
    return results


BLINK_MODE = 0x2

# (description, color, mode, commit). Blink frames are live-only, like the real
# swap would send them; the steady colors at the end commit as set_color() does.
BLINK_TESTS = [
    ("hardware blink YELLOW (candidate swap-in-progress)", 'yellow', BLINK_MODE, False),
    ("hardware blink GREEN (alternative swap-in-progress)", 'green', BLINK_MODE, False),
    ("hardware blink RED (candidate swap-failed)", 'red', BLINK_MODE, False),
    ("swap finishes: blink YELLOW -> steady ORANGE (ISP B)", 'orange', 0x1, True),
    ("swap finishes: blink YELLOW -> steady BLUE (ISP A)", 'blue', 0x1, True),
]


def run_blink(led):
    results = []
    for i, (desc, color, mode, commit) in enumerate(BLINK_TESTS, 1):
        if mode != BLINK_MODE:
            # Show the transition: blink for 3s, then settle like a confirmed swap.
            send(led, {}, (COLOR_CODES['yellow'] & 0xF0) | BLINK_MODE, commit=False)
            print(f"\n[{i}/{len(BLINK_TESTS)}] blinking YELLOW for 3s, then steady...")
            time.sleep(3)
        send(led, {}, (COLOR_CODES[color] & 0xF0) | mode, commit)
        print(f"\n[{i}/{len(BLINK_TESTS)}] {desc}")
        ans = input("  How does it look? (ok / too fast / too slow / not blinking / "
                    "wrong color / describe): ").strip()
        results.append((desc, ans or 'ok'))
    return results


def run_colors(led):
    # Live-only (no commit), mode 1, exactly as a software blink frame would be sent.
    results = []
    nibbles = [0x0] + list(range(0x8, 0x10))
    for i, nib in enumerate(nibbles, 1):
        led.set_color('blue')
        time.sleep(0.5)
        color_byte = (nib << 4) | 0x1
        send(led, {}, color_byte, commit=False)
        desc = f"color nibble 0x{nib:X} (byte 12 = 0x{color_byte:02X})"
        print(f"\n[{i}/{len(nibbles)}] {desc}  (was BLUE just before)")
        ans = input("  What does it show? (off / stays blue / white / <color> / describe): ").strip()
        results.append((desc, ans or 'stays blue'))
    return results


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else 'modes'
    runners = {'modes': run_modes, 'headers': run_headers, 'blink': run_blink,
               'colors': run_colors}
    if which not in runners:
        print(__doc__)
        return

    try:
        import config
        cfg = config.load()
        led = LedController(cfg.keyboard_vid, cfg.keyboard_pid)
    except config.ConfigError:
        led = LedController()  # no config.toml yet: built-in default IDs
    results = []
    try:
        results = runners[which](led)
    finally:
        # Back to steady blue, mode 1.
        led.set_color('blue')
        led.close()

    print("\n=== results (started from CYAN each time) ===")
    for desc, ans in results:
        print(f"  {desc:42s} -> {ans}")


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
