"""
Mini keyboard -> MikroTik ISP switcher + microphone mute, with LED status.

On startup: reads the active default-route ISP from the router and sets the
keyboard LED to that ISP's color (from config.toml).

ISP key (default F18, button #1): swaps the active ISP on the router. While
swapping the LED blinks green/yellow at 100ms until the router confirms the new
ISP is active, then settles on the new ISP's color. If the router does not
confirm, the swap is reverted.

Mic key (default F19, button #2): toggles the Windows default microphone (by
this script when keys.mic_toggle_by_script is true, else by another app; the
mic state is always tracked). Both keyboard LEDs share one color, so the mic
state is shown by blinking: mic muted -> steady ISP color, mic live -> the ISP's
mic_live_color (default: its color) as an Airbus-style double flash on a white base. Mutes made elsewhere (Teams, MicMute, ...) show too.

Options:
  --config PATH   config file (default: ISP_SWITCHER_CONFIG, else config.toml next to the exe/script)
  --check         run the preflight checks only
  --wait SEC      retry failed preflight checks for up to SEC seconds before giving
                  up (logon task: the network may not be up yet)

All output also goes to a size-rotated log file ([log] in the config), which is
the only output of the windowless isp_switcher_bg.exe build.
"""
import ctypes
import sys
import threading
import time

from pynput import keyboard

import config as config_mod
import log_setup
import preflight
from led_controller import LedController
from mic_controller import MicMonitor
from mikrotik_client import MikrotikClient

BLINK_A = 'green'
BLINK_B = 'yellow'
BLINK_INTERVAL = 0.1

# Mic-live Airbus-style strobe (double flash, ~46/min): (color, seconds); None =
# the ISP's mic_live_color. White is the base (there is no live "off"; dark needs
# a commit per frame), and it starts with it so the LED changes the moment the mic
# goes live. ~0.1 s is the shortest flash that reads reliably over USB HID writes
# and Windows timer resolution.
MIC_LIVE_PATTERN = [('white', 1.0), (None, 0.1), ('white', 0.1), (None, 0.1)]

CONFIRM_TIMEOUT_SEC = 30
CONFIRM_POLL_SEC = 0.5

LED_REFRESH_SEC = 3

# Per-session mutex: two copies would both act on every key press, so a mic
# toggle would cancel itself out.
MUTEX_NAME = 'Local\\micro-keyboard-to-control-isp-and-micmute'
ERROR_ALREADY_EXISTS = 183

PREFLIGHT_RETRY_SEC = 15


def _arg(name):
    args = sys.argv[1:]
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else None
    return None


def _acquire_single_instance():
    """Returns the mutex handle (keep it alive), or None if another copy holds it."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def _parse_key(name):
    # Named keys ("f18", "media_play_pause", ...) or a single character.
    try:
        return keyboard.Key[name.lower()]
    except KeyError:
        if len(name) == 1:
            return keyboard.KeyCode.from_char(name)
        raise config_mod.ConfigError(f"Unknown key name: {name!r}") from None


class IspSwitcher:
    def __init__(self, led, mt, isp_colors, isp_live_colors=None, flush_after_swap=True):
        self.led = led
        self.mt = mt
        self.isp_colors = isp_colors
        self.isp_live_colors = isp_live_colors or {}
        self.flush_after_swap = flush_after_swap
        self._swap_lock = threading.Lock()
        self._swapping = False
        self._led_lock = threading.Lock()
        self._color = None  # ISP color, or 'red' on error
        self._live_color = None  # ISP color used in the mic-live strobe
        self._mic_live = False

    def _show(self, color, live_color=None):
        self._color = color
        self._live_color = live_color or color
        self._render()

    def _show_isp(self, name):
        color = self.isp_colors[name]
        self._show(color, self.isp_live_colors.get(name))
        return color

    def show_error(self):
        self._show('red')

    def set_mic_live(self, live):
        self._mic_live = live
        self._render()

    def _render(self):
        # Single place that drives the LED. The swap blink owns it while a swap
        # runs; _do_swap re-renders when it finishes.
        with self._led_lock:
            with self._swap_lock:
                if self._swapping:
                    return
            color, live_color = self._color, self._live_color
            if color is None:
                return
            # LED is cosmetic: a keyboard write failure (e.g. unplugged) must never
            # be mistaken for a router failure and trigger a revert.
            try:
                self.led.stop_blink()
                # Steady write first so the ISP color is what gets saved as the
                # keyboard's power-on color, even while the mic blink runs.
                self.led.set_color(color)
                if self._mic_live:
                    self.led.start_pattern([(c or live_color, sec) for c, sec in MIC_LIVE_PATTERN])
            except Exception as e:
                print(f"[led] set {color} failed (keyboard unplugged?): {e}")

    def _flush_connections(self):
        if not self.flush_after_swap:
            return
        # Best-effort like the LED: the route swap already succeeded, so a flush
        # failure must not trigger a revert.
        try:
            self.mt.flush_internet_connections()
            print("[swap] flushed internet conntrack entries")
        except Exception as e:
            print(f"[swap] conntrack flush failed: {e}")

    def sync_led_to_current_isp(self):
        current = self.mt.get_active_isp()
        color = self._show_isp(current)
        print(f"[startup] active ISP = {current.upper()} -> LED {color}")
        return current

    def request_swap(self):
        with self._swap_lock:
            if self._swapping:
                print("[swap] already in progress, ignoring")
                return
            self._swapping = True

        t = threading.Thread(target=self._do_swap, daemon=True)
        t.start()

    def _do_swap(self):
        previous = None
        try:
            previous = self.mt.get_active_isp()
            target = self.mt.opposite(previous)
            print(f"[swap] {previous.upper()} -> {target.upper()}")

            with self._led_lock:
                self.led.start_blink(BLINK_A, BLINK_B, BLINK_INTERVAL)

            self.mt.switch_to(target)

            deadline = time.time() + CONFIRM_TIMEOUT_SEC
            confirmed = False
            while time.time() < deadline:
                try:
                    if self.mt.get_active_isp() == target:
                        confirmed = True
                        break
                except Exception as e:
                    print(f"[swap] poll error: {e}")
                time.sleep(CONFIRM_POLL_SEC)

            if confirmed:
                self._flush_connections()
                color = self._show_isp(target)
                print(f"[swap] confirmed {target.upper()} active -> LED {color}")
            else:
                print(f"[swap] timeout - reverting to {previous.upper()}")
                self._revert(previous)
        except Exception as e:
            print(f"[swap] error: {e}")
            if previous is not None:
                self._revert(previous)
            else:
                self.show_error()
        finally:
            with self._swap_lock:
                self._swapping = False
            self._render()

    def _revert(self, previous):
        try:
            self.mt.switch_to(previous)
        except Exception as e:
            print(f"[revert] switch_to({previous}) failed: {e}")
            self.show_error()
            return

        deadline = time.time() + CONFIRM_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                restored = self.mt.get_active_isp() == previous
            except Exception as e:
                print(f"[revert] poll error: {e}")
                restored = False
            if restored:
                self._flush_connections()
                color = self._show_isp(previous)
                print(f"[revert] restored {previous.upper()} -> LED {color}")
                return
            time.sleep(CONFIRM_POLL_SEC)

        print(f"[revert] timeout - router did not confirm {previous.upper()}")
        self.show_error()


def main():
    check_only = '--check' in sys.argv[1:]
    try:
        cfg = config_mod.load(_arg('--config'))
        isp_key = _parse_key(cfg.isp_key)
        mic_key = _parse_key(cfg.mic_key)
    except config_mod.ConfigError as e:
        # No usable [log] settings yet: still leave a trace for the windowless build.
        log_setup.setup(config_mod.DEFAULT_LOG_DIR, 1024 * 1024, 2)
        print(f"[preflight] FATAL   {e}")
        sys.exit(2)

    log_path = log_setup.setup(cfg.log_dir, cfg.log_max_bytes, cfg.log_backups)
    print(f"[start] logging to {log_path}")

    mutex = None
    if not check_only:
        mutex = _acquire_single_instance()
        if mutex is None:
            print("[start] another isp_switcher is already running in this session - exiting")
            sys.exit(3)

    try:
        wait_sec = float(_arg('--wait') or 0)
    except ValueError:
        wait_sec = 0
    deadline = time.time() + wait_sec
    while True:
        rep = preflight.run(cfg)
        rep.print()
        if not rep.fatal or time.time() >= deadline:
            break
        print(f"[start] fatal problems, retrying in {PREFLIGHT_RETRY_SEC}s (--wait)")
        time.sleep(PREFLIGHT_RETRY_SEC)
    if rep.fatal:
        sys.exit(2)
    if check_only:
        return

    led = LedController(cfg.keyboard_vid, cfg.keyboard_pid)
    mt = MikrotikClient(cfg.router, cfg.isps, cfg.keep_dst_regex)
    switcher = IspSwitcher(led, mt, {isp.name: isp.color for isp in cfg.isps},
                           {isp.name: isp.mic_live_color for isp in cfg.isps},
                           flush_after_swap=cfg.flush_after_swap)

    try:
        switcher.sync_led_to_current_isp()
    except Exception as e:
        print(f"[startup] failed to read active ISP: {e}")
        switcher.show_error()

    mic = MicMonitor(on_change=switcher.set_mic_live)
    mic.start()

    def on_press(key):
        if key == isp_key:
            print(f"[key] {key} pressed -> swap ISP")
            switcher.request_swap()
        elif key == mic_key and cfg.mic_toggle_by_script:
            print(f"[key] {key} pressed -> toggle mic")
            mic.request_toggle()

    mic_owner = "this script" if cfg.mic_toggle_by_script else "another app (LED follows)"
    print(f"[ready] {cfg.isp_key.upper()} = swap ISP, {cfg.mic_key.upper()} = toggle mic "
          f"via {mic_owner} - press Ctrl+C to exit")
    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    try:
        while True:
            time.sleep(LED_REFRESH_SEC)
            # Re-push the color so a replugged keyboard comes back on its own.
            try:
                led.refresh()
            except Exception:
                pass
    except KeyboardInterrupt:
        print("\n[exit] shutting down")
    finally:
        mic.stop()
        listener.stop()
        led.close()
        mt.close()


if __name__ == '__main__':
    main()
