"""
Loads config.toml (template: config.example.toml).

The path defaults to config.toml next to this file and can be overridden with
the ISP_SWITCHER_CONFIG environment variable.
"""
import os
import sys
import tomllib
from dataclasses import dataclass

from led_controller import COLOR_CODES

# A PyInstaller build looks for config.toml next to the .exe, not in its temp dir.
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
DEFAULT_PATH = os.path.join(HERE, 'config.toml')


@dataclass
class Isp:
    name: str
    route_comment: str
    color: str


@dataclass
class RouterConfig:
    host: str
    user: str | None = None
    port: int | None = None
    key_file: str | None = None
    legacy_ssh_rsa: bool = True


@dataclass
class Config:
    router: RouterConfig
    isps: list[Isp]
    flush_after_swap: bool
    keep_dst_regex: list[str]
    isp_key: str
    mic_key: str
    mic_toggle_by_script: bool
    keyboard_vid: int
    keyboard_pid: int


class ConfigError(Exception):
    pass


def _int(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def _no_quotes(label, value):
    # These strings are pasted into RouterOS commands inside "..." quotes.
    if '"' in value or '\\' in value:
        raise ConfigError(f'{label} must not contain quotes or backslashes: {value!r}')
    return value


def _path(value, base):
    if not value:
        return None
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(base, value)


def load(path=None):
    path = path or os.environ.get('ISP_SWITCHER_CONFIG') or DEFAULT_PATH
    if not os.path.exists(path):
        raise ConfigError(f"Config not found: {path}\n"
                          f"Copy config.example.toml to config.toml and fill it in.")
    base = os.path.dirname(os.path.abspath(path))
    with open(path, 'rb') as f:
        raw = tomllib.load(f)

    try:
        r = raw['router']
        router = RouterConfig(
            host=r['host'],
            user=r.get('user') or None,
            port=_int(r['port']) if r.get('port') else None,
            key_file=_path(r.get('key_file'), base),
            legacy_ssh_rsa=bool(r.get('legacy_ssh_rsa', True)),
        )

        isps = [Isp(name=i['name'],
                    route_comment=_no_quotes('isp.route_comment', i['route_comment']),
                    color=i['color'].lower())
                for i in raw['isp']]
        if len(isps) != 2:
            raise ConfigError(f"Exactly two [[isp]] entries are required, got {len(isps)}")
        for isp in isps:
            if isp.color not in COLOR_CODES:
                raise ConfigError(f"isp {isp.name}: unknown color {isp.color!r}, "
                                  f"use one of {', '.join(COLOR_CODES)}")

        ct = raw.get('conntrack', {})
        keys = raw.get('keys', {})
        kb = raw.get('keyboard', {})

        return Config(
            router=router,
            isps=isps,
            flush_after_swap=bool(ct.get('flush_after_swap', True)),
            keep_dst_regex=[_no_quotes('conntrack.keep_dst_regex', x)
                            for x in ct.get('keep_dst_regex', [])],
            isp_key=keys.get('isp', 'f18'),
            mic_key=keys.get('mic', 'f19'),
            mic_toggle_by_script=bool(keys.get('mic_toggle_by_script', True)),
            keyboard_vid=_int(kb.get('vid', 0x514C)),
            keyboard_pid=_int(kb.get('pid', 0x8851)),
        )
    except KeyError as e:
        raise ConfigError(f"{path}: missing required setting {e}") from None
