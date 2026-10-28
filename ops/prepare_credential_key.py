"""Initial host key provisioning. Never replace a missing key for an existing vault."""
import base64
import os
import secrets
import stat
from pathlib import Path

KEY = Path(os.environ.get('AW_KEY_FILE', '/etc/academic-watcher/master.key'))
VAULT = Path(os.environ.get('AW_VAULT_FILE', '/var/lib/academic-watcher/credentials/vault.enc'))


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Requires root')
    for path in (KEY, VAULT):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise RuntimeError('Unsafe credential path')
    if not KEY.exists() and VAULT.exists():
        raise RuntimeError('Existing vault requires its original key; restore that key, do not regenerate')
    KEY.parent.mkdir(mode=0o700, exist_ok=True)
    info = KEY.parent.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Credential key directory must be root-owned 0700')
    if not KEY.exists():
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(KEY.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    info = KEY.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('Credential key must be root-owned 0600')
    if len(KEY.read_bytes()) != 44:
        raise RuntimeError('Invalid master key; do not replace it')
    print('SERVER_MASTER_KEY_READY')


if __name__ == '__main__':
    main()
