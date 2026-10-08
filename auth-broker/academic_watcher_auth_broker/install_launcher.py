"""One-time HKCU protocol registration. No admin rights or browser policy changes."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .launch import runtime_root, safe_directory, safe_file, singleton, Supervisor

REGISTRY = r'Software\Classes\academicwatcher'
OWNER = 'AcademicWatcherBrokerLauncher-v1'


def command_for(pythonw):
    executable = safe_file(pythonw)
    return f'"{executable}" -m academic_watcher_auth_broker.launch "%1"'


def existing_handler(registry):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, REGISTRY) as key:
            owner = registry.QueryValueEx(key, 'AcademicWatcherOwner')[0]
        with registry.OpenKey(registry.HKEY_CURRENT_USER, REGISTRY + r'\shell\open\command') as key:
            command = registry.QueryValueEx(key, '')[0]
        return owner, command
    except FileNotFoundError:
        # Distinguish no handler from an unowned pre-existing protocol key.
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, REGISTRY):
                return 'UNOWNED', ''
        except FileNotFoundError:
            return None


def install(tunnel=None, *, backend='http://127.0.0.1:8000',
            origins=('http://localhost:8080', 'http://127.0.0.1:8080'), registry=None):
    if registry is None:
        import winreg as registry
    root = runtime_root()
    safe_directory(root)
    python = safe_file(sys.executable)
    pythonw = safe_file(str(Path(python).with_name('pythonw.exe')))
    if tunnel:
        script = safe_file(tunnel)
        safe_file(str(Path(script).with_name('migration.py')))
        installed = {'python': python, 'tunnel': script}
    else:
        installed = {'python': python, 'backend': backend, 'origins': list(origins)}
    Supervisor(installed)  # Validate before registry/config writes.
    command = command_for(pythonw)
    previous = existing_handler(registry)
    if previous is not None and previous != (OWNER, command):
        raise RuntimeError('Existing URI handler not owned by this installation; not overwritten')
    # Never change live supervisor config behind its back.
    with singleton(root) as acquired:
        if not acquired:
            raise RuntimeError('Stop existing managed launcher before installation')
        config = root / 'config.json'
        if config.is_symlink():
            raise ValueError('Unsafe config path')
        if config.exists():
            backup = root / ('config.before-' + str(time.time_ns()) + '.json')
            backup.write_bytes(config.read_bytes())
        temporary = root / ('config-' + str(time.time_ns()) + '.new')
        temporary.write_text(json.dumps(installed), encoding='utf-8')
        temporary.replace(config)
        (root / 'stop.request').unlink(missing_ok=True)
        for suffix, values in (
            ('', {'': 'URL:Academic Watcher Broker', 'URL Protocol': '', 'AcademicWatcherOwner': OWNER}),
            (r'\shell\open\command', {'': command}),
        ):
            with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, REGISTRY + suffix, 0, registry.KEY_WRITE) as key:
                for name, value in values.items():
                    registry.SetValueEx(key, name, 0, registry.REG_SZ, value)
    print('Current-user Start Broker handler installed. Browser confirmation remains enabled.')


def stop():
    root = runtime_root()
    safe_directory(root)
    with singleton(root) as acquired:
        if acquired:
            (root / 'stop.request').unlink(missing_ok=True)
            return
    (root / 'stop.request').write_text('stop', encoding='ascii')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with singleton(root) as acquired:
            if acquired:
                (root / 'stop.request').unlink(missing_ok=True)
                return
        time.sleep(1)
    raise RuntimeError('Managed launcher did not stop; no unrelated process was terminated')


def uninstall():
    import winreg as registry
    command = command_for(str(Path(sys.executable).with_name('pythonw.exe')))
    if existing_handler(registry) != (OWNER, command):
        raise RuntimeError('Handler ownership mismatch; nothing removed')
    stop()
    for suffix in (r'\shell\open\command', r'\shell\open', r'\shell', ''):
        registry.DeleteKey(registry.HKEY_CURRENT_USER, REGISTRY + suffix)
    print('Owned URI handler removed; browser profiles and credentials retained.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'stop', 'uninstall'))
    parser.add_argument('--tunnel')
    parser.add_argument('--backend')
    parser.add_argument('--origins', help='Comma-separated exact frontend origins')
    args = parser.parse_args()
    try:
        if args.action == 'install':
            if args.tunnel and (args.backend or args.origins):
                parser.error('--tunnel cannot be combined with local backend/origins')
            install(args.tunnel, backend=args.backend or 'http://127.0.0.1:8000',
                    origins=(args.origins or 'http://localhost:8080,http://127.0.0.1:8080').split(','))
        elif args.action == 'stop':
            stop()
        else:
            uninstall()
    except Exception as error:
        print(f'Launcher operation failed: {type(error).__name__}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
