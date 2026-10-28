"""
Startup checks. FATAL problems stop the switcher; WARNINGs are printed and it runs anyway.

Fatal:
  - not Windows
  - [router] config incomplete (host, user, key file) or SSH login fails
  - the router is not a MikroTik
  - mic control missing: no default microphone (keys.mic_toggle_by_script = true),
    or MicMute not installed (mic_toggle_by_script = false, MicMute does the toggling)
  - router config below the minimum requirement (see check_routes / check_user_policy)

Warning: keyboard not found, not elevated, RouterOS other than 6.x, ISP route
inactive / unusual distance / no check-gateway, another app bound to the mic key.

  uv run preflight.py     (or: isp_switcher.exe --check)
"""
import ctypes
import os
import re
import sys

MICMUTE_EXE = r"C:\Program Files (x86)\MicMute\MicMute.exe"
MICMUTE_REG = r"Software\MicMute"


class Report:
    def __init__(self):
        self.fatal = []
        self.warn = []
        self.ok = []

    def print(self):
        for m in self.ok:
            print(f"[preflight] ok      {m}")
        for m in self.warn:
            print(f"[preflight] WARNING {m}")
        for m in self.fatal:
            print(f"[preflight] FATAL   {m}")


def check_platform(rep):
    if sys.platform != 'win32':
        rep.fatal.append(f"Windows is required (running on {sys.platform})")
        return False
    rep.ok.append("running on Windows")
    return True


def check_router_config(rep, client):
    if not client.host:
        rep.fatal.append("[router] host is empty")
    if not client.username:
        rep.fatal.append("[router] user is empty and no User in ~/.ssh/config for this host")
    if client.key_filename and not os.path.exists(os.path.expanduser(client.key_filename)):
        rep.fatal.append(f"[router] key_file not found: {client.key_filename}")
    if not client.key_filename:
        rep.warn.append("[router] no key_file: relying on ssh-agent / default keys")
    return not rep.fatal


def check_mikrotik(rep, client):
    try:
        platform = client._run(':put [/system resource get platform]')
        version = client._run(':put [/system resource get version]')
        board = client._run(':put [/system resource get board-name]')
    except Exception as e:
        rep.fatal.append(f"cannot log in to router {client.username}@{client.host}:{client.port}: {e}")
        return False
    if platform.strip() != 'MikroTik':
        rep.fatal.append(f"router at {client.host} is not a MikroTik (platform={platform!r})")
        return False
    rep.ok.append(f"MikroTik {board} RouterOS {version}")
    if not version.startswith('6.'):
        rep.warn.append(f"tested on RouterOS 6.x only, this router runs {version}")
    return True


def _default_routes(client):
    # comment goes last so a '|' inside it can't shift the other fields
    out = client._run(
        ':foreach r in=[/ip route find where dst-address=0.0.0.0/0] do={ :put ('
        '[/ip route get $r distance] . "|" . [/ip route get $r disabled] . "|" . '
        '[/ip route get $r active] . "|" . [/ip route get $r dynamic] . "|" . '
        '[/ip route get $r check-gateway] . "|" . [/ip route get $r comment]) }'
    )
    routes = []
    for line in out.splitlines():
        parts = line.strip().split('|', 5)
        if len(parts) != 6:
            continue
        distance, disabled, active, dynamic, check_gw, comment = parts
        routes.append(dict(distance=int(distance or 0), disabled=disabled == 'true',
                           active=active == 'true', dynamic=dynamic == 'true',
                           check_gateway=check_gw, comment=comment))
    return routes


def check_routes(rep, client, isps):
    """Minimum requirement:
      R1 each ISP has exactly one enabled 0.0.0.0/0 route whose comment matches its
         route_comment (RouterOS `comment~` regex, case-sensitive, as used for switching)
      R2 no route matches both ISPs
      R3 no other enabled default route has distance <= 2 (it would tie with or beat
         the ISP routes the switcher toggles between 1 and 2)
    """
    try:
        routes = [r for r in _default_routes(client) if not r['disabled']]
    except Exception as e:
        rep.fatal.append(f"cannot read default routes: {e}")
        return False

    ok = True
    matched = set()
    for isp in isps:
        hits = [i for i, r in enumerate(routes) if re.search(isp.route_comment, r['comment'])]
        if not hits:
            rep.fatal.append(f'R1: no enabled 0.0.0.0/0 route with a comment matching '
                             f'"{isp.route_comment}" (ISP {isp.name})')
            ok = False
            continue
        if len(hits) > 1:
            rep.fatal.append(f'R1: {len(hits)} enabled default routes match "{isp.route_comment}" '
                             f'(ISP {isp.name}); exactly one is required')
            ok = False
        both = matched.intersection(hits)
        if both:
            rep.fatal.append(f'R2: route "{routes[both.pop()]["comment"]}" matches both ISPs')
            ok = False
        matched.update(hits)

        r = routes[hits[0]]
        if r['distance'] not in (1, 2):
            rep.warn.append(f'ISP {isp.name} route has distance={r["distance"]}; '
                            f'the first swap will set it to 1 or 2')
        if not r['check_gateway']:
            rep.warn.append(f'ISP {isp.name} route has no check-gateway: the router '
                            f'will not fail over by itself if this ISP goes down')

    for i, r in enumerate(routes):
        if i not in matched and r['distance'] <= 2:
            kind = 'dynamic' if r['dynamic'] else 'static'
            rep.fatal.append(f'R3: other enabled {kind} default route "{r["comment"] or "(no comment)"}" '
                             f'has distance={r["distance"]} and would override the ISP switch '
                             f'(disable it, or give it distance >= 3)')
            ok = False

    if ok:
        active = [r for i, r in enumerate(routes) if i in matched and r['active']]
        if not active:
            rep.warn.append("neither ISP default route is active right now (link down?)")
        rep.ok.append(f"default routes meet the minimum requirement ({len(isps)} ISPs)")
    return ok


def check_user_policy(rep, client):
    """Minimum requirement R4: the SSH user's group has the `write` policy
    (needed to change route distances and remove conntrack entries)."""
    try:
        policy = client._run(
            f':put [/user group get [/user get [find name="{client.username}"] group] policy]')
    except Exception as e:
        rep.fatal.append(f"R4: cannot read policy of router user {client.username}: {e}")
        return False
    granted = {p for p in policy.replace(';', ',').split(',') if p and not p.startswith('!')}
    if 'write' not in granted:
        rep.fatal.append(f"R4: router user {client.username} lacks the 'write' policy "
                         f"(group policy: {policy})")
        return False
    rep.ok.append(f"router user {client.username} has write policy")
    return True


def check_mic(rep, cfg):
    if cfg.mic_toggle_by_script:
        names, reason = [], ""
        try:
            import comtypes
            from mic_controller import device_name, targets
            comtypes.CoInitialize()
            try:
                names = [device_name(dev_id) for dev_id, _ in targets()]
            finally:
                comtypes.CoUninitialize()
        except Exception as e:
            reason = f" ({e})"
        if not names:
            rep.fatal.append(f"no microphone to mute/unmute{reason}")
            return False
        rep.ok.append(f"microphone target now: {', '.join(names)} "
                      f"(mics in use, else the last used / MicMute's / default mic; "
                      f"toggled by this script)")
        _warn_micmute_conflict(rep, cfg.mic_key)
        return True

    if not os.path.exists(MICMUTE_EXE):
        rep.fatal.append(f"keys.mic_toggle_by_script = false but MicMute is not installed "
                         f"({MICMUTE_EXE}); install it or set mic_toggle_by_script = true")
        return False
    rep.ok.append("MicMute installed (it toggles the mic, LED follows)")
    import psutil
    if not any((p.info['name'] or '').lower() == 'micmute.exe'
               for p in psutil.process_iter(['name'])):
        # Nothing is handed to MicMute, so no ordering issue: the mic key simply does
        # nothing until it runs, and the LED picks the state up whenever it changes.
        rep.warn.append("MicMute is not running: the mic key does nothing until it starts")
    return True


def _warn_micmute_conflict(rep, mic_key):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MICMUTE_REG) as k:
            hotkey, _ = winreg.QueryValueEx(k, 'Hotkey')
    except OSError:
        return
    if mic_key.lower() in [p.strip().lower() for p in str(hotkey).split(',')]:
        rep.warn.append(f"MicMute's toggle hotkey is also {mic_key.upper()}: one press will "
                        f"mute and unmute. Clear it in MicMute or set mic_toggle_by_script = false")


def check_keyboard(rep, cfg):
    from pywinusb import hid
    if hid.HidDeviceFilter(vendor_id=cfg.keyboard_vid, product_id=cfg.keyboard_pid).get_devices():
        rep.ok.append(f"keyboard {cfg.keyboard_vid:04X}:{cfg.keyboard_pid:04X} connected")
    else:
        rep.warn.append(f"keyboard {cfg.keyboard_vid:04X}:{cfg.keyboard_pid:04X} not found: LED "
                        f"stays dark until it is plugged in (run find_device.py)")


def check_elevation(rep):
    if not ctypes.windll.shell32.IsUserAnAdmin():
        rep.warn.append("not elevated: the keys won't register while an elevated (admin) "
                        "window has focus")


def run(cfg):
    """Returns a Report; callers stop on rep.fatal."""
    from mikrotik_client import MikrotikClient

    rep = Report()
    if not check_platform(rep):
        return rep

    client = MikrotikClient(cfg.router, cfg.isps, cfg.keep_dst_regex)
    try:
        if check_router_config(rep, client) and check_mikrotik(rep, client):
            check_routes(rep, client, cfg.isps)
            check_user_policy(rep, client)
    finally:
        client.close()

    check_mic(rep, cfg)
    check_keyboard(rep, cfg)
    check_elevation(rep)
    return rep


def main():
    import config
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"[preflight] FATAL   {e}")
        sys.exit(2)
    rep = run(cfg)
    rep.print()
    sys.exit(2 if rep.fatal else 0)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
