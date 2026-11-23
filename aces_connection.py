"""Opt-in ACES SSH via TAMU's SSH certificate and jump host.

Credentials are passed only in memory. Never log, cache, or persist key material.
"""
import io
import paramiko

JUMP_HOST = 'aces-jump.hprc.tamu.edu'
LOGIN_HOST = 'login.aces.hprc.tamu.edu'
JUMP_PORT = 8822


class AcesSSH:
    def __init__(self, client, jump):
        self.client = client
        self.jump = jump

    def __getattr__(self, name):
        return getattr(self.client, name)

    def close(self):
        try:
            self.client.close()
        finally:
            self.jump.close()


def parse_private_key(key_data, passphrase=None):
    if isinstance(key_data, bytes):
        key_data = key_data.decode('utf-8')
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_data), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise ValueError('Private key is encrypted. Enter its passphrase.') from None
        except (paramiko.SSHException, ValueError):
            continue
    raise ValueError('Unsupported SSH private key format or incorrect passphrase.')


def load_aces_host_keys(client, known_hosts_text):
    """Fail closed. The host keys MUST be verified independently with TAMU."""
    keys = paramiko.HostKeys()
    for raw in known_hosts_text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
        if entry is None or entry.key is None or not entry.hostnames:
            raise ValueError('Invalid ACES_KNOWN_HOSTS entry.')
        for hostname in entry.hostnames:
            keys.add(hostname, entry.key.get_name(), entry.key)
    needed = (f'[{JUMP_HOST}]:{JUMP_PORT}', LOGIN_HOST)
    missing = [host for host in needed if keys.lookup(host) is None]
    if missing:
        raise ValueError('ACES_KNOWN_HOSTS must contain independently verified host keys for: ' + ', '.join(missing))
    for hostname, algorithms in keys.items():
        for algorithm, key in algorithms.items():
            client.get_host_keys().add(hostname, algorithm, key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


def connect_aces(username, private_key, certificate, host_keys, passphrase=None):
    """Connect to ACES login host through the TAMU jump host with SSH certificate auth."""
    if not certificate.strip().startswith(('ssh-ed25519-cert-v01@openssh.com',
                                           'ssh-rsa-cert-v01@openssh.com',
                                           'ecdsa-sha2-nistp256-cert-v01@openssh.com')):
        raise ValueError('Upload id_aces_tamu-cert.pub (SSH certificate), not a regular public key.')
    key = parse_private_key(private_key, passphrase)
    key.load_certificate(certificate.strip())
    jump = paramiko.SSHClient()
    inner = paramiko.SSHClient()
    try:
        load_aces_host_keys(jump, host_keys)
        jump.connect(JUMP_HOST, port=JUMP_PORT, username=username, pkey=key,
                     look_for_keys=False, allow_agent=False, timeout=18, auth_timeout=25, banner_timeout=25)
        channel = jump.get_transport().open_channel('direct-tcpip',
                                                     (LOGIN_HOST, 22), ('127.0.0.1', 0), timeout=20)
        load_aces_host_keys(inner, host_keys)
        inner.connect(LOGIN_HOST, port=22, username=username, pkey=key, sock=channel,
                      look_for_keys=False, allow_agent=False, timeout=20, auth_timeout=25, banner_timeout=25)
        return AcesSSH(inner, jump)
    except Exception:
        inner.close()
        jump.close()
        raise
