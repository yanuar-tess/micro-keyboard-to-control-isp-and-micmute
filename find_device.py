"""
Find the mini keyboard's USB VID/PID for config.toml [keyboard].

  uv run find_device.py          list HID devices; mark the ones that accept LED commands
  uv run find_device.py --diff   unplug -> Enter -> plug in -> Enter: shows only what appeared
  uv run find_device.py --test 0x514C 0x8851
                                 flash the LED red/green/blue on that device, then restore

A device that charges/lights up but never appears here is usually on a
charge-only USB cable: Windows sees no data connection at all.
"""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pywinusb import hid

LED_REPORT_ID = 3


def _has_led_report(device):
    # The LED protocol uses HID output report 3; most keyboards/mice don't have it.
    try:
        device.open()
        try:
            reports = device.find_output_reports() + device.find_feature_reports()
            return any(r.report_id == LED_REPORT_ID for r in reports)
        finally:
            device.close()
    except Exception:
        return None  # in use exclusively / access denied


def _snapshot():
    found = {}
    for d in hid.find_all_hid_devices():
        key = (d.vendor_id, d.product_id)
        entry = found.setdefault(key, {'name': (d.product_name or '').strip(),
                                       'vendor': (d.vendor_name or '').strip(),
                                       'interfaces': 0, 'led': False})
        entry['interfaces'] += 1
        if _has_led_report(d):
            entry['led'] = True
    return found


def _print(devices):
    if not devices:
        print("  (none)")
        return
    print(f"  {'VID':6s} {'PID':6s} {'ifaces':6s} {'LED?':5s} name")
    for (vid, pid), e in sorted(devices.items()):
        mark = 'YES' if e['led'] else '-'
        label = ' / '.join(x for x in (e['vendor'], e['name']) if x)
        print(f"  {vid:04X}   {pid:04X}   {e['interfaces']:<6d} {mark:5s} {label}")


def cmd_list():
    devices = _snapshot()
    print("HID devices (LED? = accepts report ID 3, the keyboard LED protocol):")
    _print(devices)
    candidates = [k for k, e in devices.items() if e['led']]
    if candidates:
        print("\nCandidate(s) for config.toml:")
        for vid, pid in candidates:
            print(f'  [keyboard]\n  vid = "0x{vid:04X}"\n  pid = "0x{pid:04X}"')
        print("\nConfirm with:  uv run find_device.py --test "
              f"0x{candidates[0][0]:04X} 0x{candidates[0][1]:04X}")
    else:
        print("\nNo device accepts LED commands. Try --diff, and check the USB cable carries data.")


def cmd_diff():
    input("Unplug the mini keyboard, then press Enter...")
    before = _snapshot()
    input("Plug it back in, wait ~3 seconds, then press Enter...")
    time.sleep(1)
    after = _snapshot()
    new = {k: v for k, v in after.items() if k not in before}
    print("\nDevices that appeared:")
    _print(new)
    if not new:
        print("\nNothing new. The keyboard is probably on a charge-only cable, a hub without "
              "data, or in wireless/Bluetooth mode. Switch it to wired USB and retry.")


def cmd_test(vid, pid):
    from led_controller import LedController
    led = LedController(vid, pid)
    try:
        for color in ('red', 'green', 'blue'):
            print(f"  LED -> {color}")
            # Live-only frames (no commit), so the saved power-on color is untouched.
            led._apply(color, commit=False)
            time.sleep(1)
    finally:
        led.close()
    print("Did both LEDs show red, green, blue? Then these IDs are correct. "
          "(Replug or start isp_switcher.py to restore the normal color.)")


def main():
    args = sys.argv[1:]
    if not args:
        cmd_list()
    elif args[0] == '--diff':
        cmd_diff()
    elif args[0] == '--test' and len(args) == 3:
        cmd_test(int(args[1], 0), int(args[2], 0))
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
