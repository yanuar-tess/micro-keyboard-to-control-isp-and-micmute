"""
Mikrotik SSH client for ISP failover control.
Uses paramiko so the transport stays open across commands (fast polling during confirmation).
Settings not given explicitly (user / key / port) fall back to the matching
Host block in ~/.ssh/config.
"""
import os
import threading
import paramiko


def _resolve_ssh_config(host):
    config_path = os.path.expanduser('~/.ssh/config')
    if not os.path.exists(config_path):
        return {}
    cfg = paramiko.SSHConfig()
    with open(config_path) as f:
        cfg.parse(f)
    return cfg.lookup(host)


class MikrotikClient:
    def __init__(self, router, isps, keep_dst_regex=()):
        """router: config.RouterConfig; isps: two config.Isp entries."""
        resolved = _resolve_ssh_config(router.host)
        self.host = resolved.get('hostname', router.host)
        self.username = router.user or resolved.get('user')
        self.key_filename = router.key_file or resolved.get('identityfile')
        self.port = int(router.port or resolved.get('port', 22))
        if isinstance(self.key_filename, list):
            self.key_filename = self.key_filename[0] if self.key_filename else None
        self.legacy_ssh_rsa = router.legacy_ssh_rsa
        self.isps = list(isps)
        self.keep_dst_regex = list(keep_dst_regex)
        self._client = None
        self._lock = threading.Lock()

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            allow_agent=True,
            look_for_keys=(self.key_filename is None),
        )
        if self.legacy_ssh_rsa:
            connect_kwargs['disabled_algorithms'] = {'pubkeys': ['rsa-sha2-256', 'rsa-sha2-512']}
        if self.key_filename:
            key_path = os.path.expanduser(self.key_filename)
            if self.legacy_ssh_rsa:
                # RouterOS old SSH servers reject rsa-sha2 signatures — loading the
                # key explicitly as an RSAKey forces paramiko to sign with ssh-rsa.
                connect_kwargs['pkey'] = paramiko.RSAKey.from_private_key_file(key_path)
            else:
                connect_kwargs['key_filename'] = key_path
            connect_kwargs['look_for_keys'] = False
            connect_kwargs['allow_agent'] = False

        client.connect(**connect_kwargs)
        return client

    def _run(self, command):
        with self._lock:
            if self._client is None or self._client.get_transport() is None \
                    or not self._client.get_transport().is_active():
                self._client = self._connect()
            stdin, stdout, stderr = self._client.exec_command(command, timeout=10)
            out = stdout.read().decode('utf-8', errors='replace').strip()
            err = stderr.read().decode('utf-8', errors='replace').strip()
            if err:
                raise RuntimeError(f"Mikrotik error: {err}")
            return out

    def _isp(self, name):
        for isp in self.isps:
            if isp.name == name:
                return isp
        raise ValueError(f"Unknown ISP: {name}")

    def opposite(self, name):
        return next(isp.name for isp in self.isps if isp.name != name)

    def get_active_isp(self):
        """Name of the ISP whose default route is currently active."""
        out = self._run(':put [/ip route get [find where dst-address=0.0.0.0/0 active=yes] comment]')
        lower = out.lower()
        for isp in self.isps:
            if isp.route_comment.lower() in lower:
                return isp.name
        raise RuntimeError(f"Could not identify active ISP from comment: {out!r}")

    def switch_to(self, target):
        winner = self._isp(target)
        loser = self._isp(self.opposite(target))
        # Lower the old route first so both are never at distance=1 together.
        self._run(
            f'/ip route set [find where comment~"{loser.route_comment}" and dst-address=0.0.0.0/0] distance=2'
        )
        self._run(
            f'/ip route set [find where comment~"{winner.route_comment}" and dst-address=0.0.0.0/0] distance=1'
        )

    def flush_internet_connections(self):
        # Conntrack pins each flow to the NAT decision made on the old WAN (e.g. one
        # WAN un-NATed because the upstream router NATs, the other masqueraded), so
        # after a swap those flows keep leaving with the wrong source and die.
        # Drop everything not bound for the kept LAN/VPN ranges so it is re-NATed fresh.
        # Per-item on-error: entries can expire between find and remove.
        if self.keep_dst_regex:
            keep = ' or '.join(f'dst-address~"{rx}"' for rx in self.keep_dst_regex)
            where = f' where !({keep})'
        else:
            where = ''
        self._run(
            f':foreach c in=[/ip firewall connection find{where}] '
            'do={ :do { /ip firewall connection remove $c } on-error={} }'
        )

    def close(self):
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
