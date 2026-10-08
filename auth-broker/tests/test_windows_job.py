import ctypes
from ctypes import wintypes
import os
import socket
from pathlib import Path
import sys
import tempfile
import time
import unittest

from academic_watcher_auth_broker.windows_process import OwnedChild, BasicLimits, ExtendedLimits, StartupInfo


@unittest.skipUnless(os.name == 'nt', 'Windows owned job')
class WindowsJobTests(unittest.TestCase):
    def test_dead_wrapper_cleanup_releases_descendant_listener_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'port.txt'
            code = ('import socket,sys,time; from pathlib import Path; '
                    's=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); '
                    'Path(sys.argv[1]).write_text(str(s.getsockname()[1])); time.sleep(60)')
            child = OwnedChild([sys.executable, '-c', code, str(marker)])
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists())
                port = int(marker.read_text())
                child.api.TerminateProcess(child.process, 1)
                child.api.WaitForSingleObject(child.process, 5000)
                child.stop()
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with socket.socket() as replacement:
                            replacement.bind(('127.0.0.1', port))
                            replacement.listen()
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.05)
            finally:
                child.stop()

    def test_real_venv_child_tree_is_owned_and_terminated(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(BasicLimits), 64)
            self.assertEqual(ctypes.sizeof(ExtendedLimits), 144)
            self.assertEqual(ctypes.sizeof(StartupInfo), 104)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'pid.txt'
            code = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)'
            child = OwnedChild([sys.executable, '-c', code, str(marker)])
            handle = None
            try:
                until = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < until:
                    time.sleep(.1)
                self.assertTrue(marker.exists(), 'Owned child did not start')
                api = child.api
                api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                api.OpenProcess.restype = wintypes.HANDLE
                handle = api.OpenProcess(0x100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                self.assertIsNone(child.poll())
                child.stop()
                self.assertEqual(api.WaitForSingleObject(handle, 5000), 0, 'Venv descendant survived owned job termination')
            finally:
                child.stop()
                if handle:
                    child.api.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
