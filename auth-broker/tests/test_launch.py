import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from academic_watcher_auth_broker import launch
from academic_watcher_auth_broker.install_launcher import command_for


class FakeChild:
    def __init__(self, args, *, env=None):
        self.args = args; self.env = env; self.dead = False; self.stopped = False
    def poll(self):
        return 1 if self.dead else None
    def stop(self):
        self.stopped = True; self.dead = True


class LaunchTests(unittest.TestCase):
    def test_start_only_uri_and_no_extra_arguments(self):
        for value in launch.START_URIS:
            launch.validate_activation([value])
        for value in ('academicwatcher://stop', 'academicwatcher://start?cmd=calc', 'academicwatcher://start#x',
                      'academicwatcher://start/path', 'academicwatcher://user@start', 'academicwatcher://%73tart',
                      'academicwatcher://start" -c calc', 'https://start'):
            with self.assertRaises(ValueError):
                launch.validate_activation([value])
        for args in ([], ['academicwatcher://start', '--execute'], ['academicwatcher://start', '']):
            with self.assertRaises(ValueError):
                launch.validate_activation(args)

    def test_invalid_uri_has_no_side_effects(self):
        with patch.object(launch, 'runtime_root') as root:
            with self.assertRaises(ValueError):
                launch.run(['academicwatcher://start?host=evil'])
            root.assert_not_called()

    def test_exact_private_origin(self):
        self.assertEqual(launch.lan_origin('https://192.168.1.10:8080'), 'https://192.168.1.10:8080')
        for value in ('https://8.8.8.8:8080', 'https://172.32.1.1:8080', 'http://192.168.1.10:8080',
                      'https://192.168.1.10:8080/?x=1', 'https://user@192.168.1.10:8080',
                      'https://192.168.1.10:8080/', 'https://192.168.1.10:443'):
            with self.assertRaises(ValueError):
                launch.lan_origin(value)

    def test_remote_metadata_fixture(self):
        data = json.loads(Path(__file__).with_name('lan_fixture.json').read_text())
        expected = launch.origin_from_metadata(data)
        self.assertEqual(expected, 'https://192.168.1.10:8080')
        self.assertEqual(launch.origin_from_metadata({'ip': data['ip']}), expected)
        with self.assertRaises(ValueError):
            launch.origin_from_metadata({**data, 'origin': 'https://8.8.8.8:8080'})

    def test_port_conflict_has_specific_fixed_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch.record(root, 'PORT_OCCUPIED')
            self.assertEqual(json.loads((root / 'status.json').read_text())['state'], 'PORT_OCCUPIED')
        source = Path(launch.__file__).read_text()
        self.assertIn('except PortOccupied:', source)
        self.assertIn('notify_port_conflict()', source)
        self.assertIn('No other process was stopped.', source)

    def make_supervisor(self):
        with patch.object(launch, 'safe_file', side_effect=lambda x: x):
            return launch.Supervisor({'python': 'installed-python', 'tunnel': 'fixed-tunnel.py'},
                                     child=FakeChild, discover=lambda: 'https://192.168.1.10:8080', ready=lambda: True)

    @patch.object(launch, 'port_free', return_value=True)
    def test_owned_start_duplicate_and_recovery(self, free):
        s = self.make_supervisor()
        self.assertEqual(s.tick(0), 'CONNECTING')
        first_tunnel = s.tunnel
        self.assertEqual(s.tick(2), 'STARTING')
        first_broker = s.broker
        self.assertEqual(s.broker.args, ['installed-python', '-m', 'academic_watcher_auth_broker'])
        self.assertEqual(s.broker.env['AW_BROKER_ORIGINS'], 'https://192.168.1.10:8080')
        self.assertEqual(s.tick(4), 'RUNNING')
        self.assertIs(s.tunnel, first_tunnel); self.assertIs(s.broker, first_broker)
        first_tunnel.dead = True
        self.assertEqual(s.tick(6), 'CONNECTING')
        self.assertTrue(first_broker.stopped); self.assertTrue(first_tunnel.stopped)
        self.assertIsNot(s.tunnel, first_tunnel)
        s.close()

    @patch.object(launch, 'port_free', return_value=False)
    def test_foreign_port_owner_not_stopped(self, free):
        s = self.make_supervisor()
        with self.assertRaises(launch.PortOccupied):
            s.tick(0)
        self.assertIsNone(s.tunnel); self.assertIsNone(s.broker)

    @patch.object(launch, 'port_free', return_value=True)
    def test_callback_unavailable_does_not_claim_ready(self, free):
        s = self.make_supervisor(); s.tick(0); s.ready = lambda: False
        self.assertEqual(s.tick(2), 'CALLBACK_UNAVAILABLE')
        self.assertIsNone(s.broker)
        s.close()

    @patch.object(launch, 'port_free', return_value=True)
    def test_periodic_discovery_failure_retains_healthy_children_then_retries(self, free):
        s = self.make_supervisor(); s.tick(0); s.tick(2)
        broker, tunnel, origin = s.broker, s.tunnel, s.origin
        s.discover = unittest.mock.Mock(side_effect=TimeoutError)
        self.assertEqual(s.tick(61), 'RUNNING')
        self.assertIs(s.broker, broker); self.assertIs(s.tunnel, tunnel)
        self.assertFalse(broker.stopped); self.assertFalse(tunnel.stopped)
        self.assertEqual(s.origin, origin)
        s.tick(62)
        self.assertEqual(s.discover.call_count, 1)
        s.discover = lambda: 'https://192.168.1.11:8080'
        self.assertEqual(s.tick(121), 'STARTING')
        self.assertTrue(broker.stopped); self.assertIs(s.tunnel, tunnel)
        self.assertEqual(s.origin, 'https://192.168.1.11:8080')
        s.close()

    @patch.object(launch, 'port_free', return_value=True)
    def test_origin_change_restarts_only_owned_broker(self, free):
        s = self.make_supervisor(); s.tick(0); s.tick(2)
        old = s.broker; tunnel = s.tunnel
        s.discover = lambda: 'https://192.168.1.11:8080'
        s.tick(61)
        self.assertTrue(old.stopped); self.assertIs(s.tunnel, tunnel)
        self.assertEqual(s.broker.env['AW_BROKER_ORIGINS'], 'https://192.168.1.11:8080')
        s.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows lock')
    def test_singleton_duplicate_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with launch.singleton(root) as first:
                self.assertTrue(first)
                with launch.singleton(root) as duplicate:
                    self.assertFalse(duplicate)
            with launch.singleton(root) as again:
                self.assertTrue(again)

    def test_registry_command_is_fixed_module_not_shell(self):
        with patch('academic_watcher_auth_broker.install_launcher.safe_file', return_value=r'C:\path with spaces\pythonw.exe'):
            cmd = command_for('ignored')
        self.assertEqual(cmd, '"C:\\path with spaces\\pythonw.exe" -m academic_watcher_auth_broker.launch "%1"')
        self.assertNotIn('powershell', cmd)


if __name__ == '__main__':
    unittest.main()
