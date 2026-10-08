import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from academic_watcher_auth_broker import launch, install_launcher
from .test_launch import FakeChild


class LocalLauncherTests(unittest.TestCase):
    def supervisor(self, **changes):
        config = {'python': 'fixed-python', 'backend': 'http://127.0.0.1:8000',
                  'origins': ['http://localhost:8080', 'http://127.0.0.1:8080'], **changes}
        with patch.object(launch, 'safe_file', side_effect=lambda value: value):
            return launch.Supervisor(config, child=FakeChild, ready=lambda: True)

    @patch.object(launch, 'port_free', return_value=True)
    def test_local_mode_owns_only_broker_not_existing_backend(self, free):
        s = self.supervisor()
        self.assertEqual(s.tick(0), 'STARTING')
        self.assertIsNone(s.tunnel)
        broker = s.broker
        self.assertEqual(broker.env['AW_BROKER_BACKEND'], 'http://127.0.0.1:8000')
        self.assertEqual(broker.env['AW_BROKER_ORIGINS'], 'http://localhost:8080,http://127.0.0.1:8080')
        self.assertEqual(s.tick(60), 'RUNNING')
        self.assertIs(s.broker, broker)
        free.assert_called_with(8765)
        s.ready = lambda: False
        self.assertEqual(s.tick(61), 'CALLBACK_UNAVAILABLE')
        self.assertIs(s.broker, broker); self.assertFalse(broker.stopped)
        s.close()

    @patch.object(launch, 'port_free', return_value=True)
    def test_local_dead_broker_recovers_owned_child(self, free):
        s = self.supervisor(); s.tick(0)
        previous = s.broker; previous.dead = True
        self.assertEqual(s.tick(1), 'STARTING')
        self.assertTrue(previous.stopped); self.assertIsNot(s.broker, previous)
        s.close()

    @patch.object(launch, 'port_free', return_value=False)
    def test_local_foreign_broker_port_is_not_stopped(self, free):
        s = self.supervisor()
        with self.assertRaises(launch.PortOccupied):
            s.tick(0)
        self.assertIsNone(s.broker)

    def test_local_config_rejects_remote_callback_wildcards_and_uri_commands(self):
        for changes in ({'backend': 'https://server.example:8000'}, {'backend': 'http://127.0.0.1:8765'},
                        {'origins': []}, {'origins': 'http://localhost:8080'}, {'origins': ['http://*.example']},
                        {'origins': ['http://localhost:8080/path']}, {'extra': 'execute'}):
            with self.assertRaises((ValueError, TypeError)):
                self.supervisor(**changes)

    @unittest.skipUnless(os.name == 'nt', 'Windows installer')
    def test_default_install_writes_public_config_with_fixed_command(self):
        registry = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(install_launcher, 'runtime_root', return_value=root), \
                 patch.object(install_launcher, 'safe_file', side_effect=lambda value: value), \
                 patch.object(launch, 'safe_file', side_effect=lambda value: value), \
                 patch.object(install_launcher, 'existing_handler', return_value=None):
                install_launcher.install(registry=registry)
            config = json.loads((root / 'config.json').read_text())
            self.assertEqual(set(config), {'python', 'backend', 'origins'})
            self.assertEqual(config['backend'], 'http://127.0.0.1:8000')
            command = registry.SetValueEx.call_args_list[-1].args[-1]
            self.assertIn('-m academic_watcher_auth_broker.launch "%1"', command)
            self.assertNotIn('powershell', command.lower())

    @unittest.skipUnless(os.name == 'nt', 'Windows installer')
    def test_foreign_handler_is_preserved(self):
        registry = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(install_launcher, 'runtime_root', return_value=root), \
                 patch.object(install_launcher, 'safe_file', side_effect=lambda value: value), \
                 patch.object(launch, 'safe_file', side_effect=lambda value: value), \
                 patch.object(install_launcher, 'existing_handler', return_value=('foreign', 'other-command')):
                with self.assertRaises(RuntimeError):
                    install_launcher.install(registry=registry)
            registry.CreateKeyEx.assert_not_called()
            self.assertFalse((root / 'config.json').exists())
