"""Bluetooth interpreter selection and error-reporting regressions."""
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import qs668_runtime as runtime

BASE = Path(__file__).resolve().parents[1]


def console_module():
    spec = importlib.util.spec_from_file_location('ble_console_test', BASE / 'qs668-console.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        runtime._select.cache_clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def create(self, relative):
        p = self.base / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    def test_existing_ble_environment_is_preferred_and_cached(self):
        ble = self.create('nextproto/tools/.venv-ble/bin/python')
        asr = self.create('funasr-env/bin/python')
        with patch.dict(os.environ, {'QS668_BLE_PYTHON': ''}), patch.object(runtime, 'probe_ble_python', return_value=(True, '')) as probe:
            self.assertEqual(runtime.ble_python(self.base, asr), str(ble.absolute()))
            self.assertEqual(runtime.ble_python(self.base, asr), str(ble.absolute()))
            probe.assert_called_once_with(ble)

    def test_broken_dedicated_environment_falls_back(self):
        ble = self.create('nextproto/tools/.venv-ble/bin/python')
        asr = self.create('funasr-env/bin/python')
        with patch.dict(os.environ, {'QS668_BLE_PYTHON': ''}), patch.object(runtime, 'probe_ble_python', side_effect=[(False, 'no bleak'), (True, '')]):
            self.assertEqual(runtime.ble_python(self.base, asr), str(asr.absolute()))

    def test_explicit_override_is_honored_without_silent_fallback(self):
        override = self.create('custom/bin/python')
        with patch.dict(os.environ, {'QS668_BLE_PYTHON': str(override)}), patch.object(runtime, 'probe_ble_python', return_value=(False, 'no bleak')):
            with self.assertRaisesRegex(RuntimeError, 'QS668_BLE_PYTHON'):
                runtime.ble_python(self.base, '/unused')

    def test_scan_uses_ble_environment_and_unbuffered_output(self):
        console = console_module()
        job = console.Job('test', 'ble')
        with patch.object(console, 'ble_python', return_value='ble-python'), patch.object(console, '_stream_step', return_value=0) as stream:
            console._run_ble(job, 'scan', '', 30, [])
            self.assertEqual(stream.call_args.args[2][:2], ['ble-python', '-u'])
            self.assertEqual(stream.call_args.args[2][-1], '--scan')
            self.assertEqual(job.status, 'done')

    def test_failed_process_retains_actual_error(self):
        console = console_module()
        job = console.Job('test', 'ble')
        proc = SimpleNamespace(stdout=io.StringIO("Traceback (most recent call last):\nbleak.exc.BleakError: BLE is not authorized - check macOS privacy settings\n"), returncode=1, wait=Mock())
        with patch.object(console.subprocess, 'Popen', return_value=proc):
            self.assertEqual(console._stream_step(job, 'BLE 扫描', ['ble-python', '--scan'], str(self.base)), 1)
        self.assertEqual(job.status, 'failed')
        self.assertIn('未授权', job.message)
        self.assertIn('exit=1', job.message)
        self.assertIn('BLE is not authorized', '\n'.join(job.log))

    def test_unsupported_error_does_not_blame_card(self):
        detail = runtime.failure_detail(['bleak.exc.BleakError: BLE is unsupported'])
        self.assertIn('当前进程', detail)
        self.assertIn('不能证明录音卡不受支持', detail)


if __name__ == '__main__':
    unittest.main()

class CollectorDiagnosticTests(unittest.TestCase):
    def test_controller_error_is_not_reported_as_device_missing(self):
        spec = importlib.util.spec_from_file_location('diagnostic_collector', BASE / 'qs668-collector.py')
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        result = SimpleNamespace(returncode=1, stdout='', stderr='bleak.exc.BleakError: BLE is unsupported')
        with patch.object(collector, 'ble_python', return_value='fixture-python'), patch.object(collector.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, '系统扫描失败'):
                collector.ble_reachable()

class ConnectionResultTests(unittest.TestCase):
    def test_scan_returns_discovery_without_claiming_connection(self):
        from qs668_ble import parse_devices
        devices = parse_devices('  AA:BB:CC:DD:EE:FF  QS668 recorder\n  not-an-address wrong\n')
        self.assertEqual(len(devices), 1)
        console = console_module()
        job = console.Job('test-scan', 'ble')
        def scan(job, label, cmd, cwd):
            job.ble_devices.extend(devices)
            return 0
        with patch.object(console, 'ble_python', return_value='fixture'), patch.object(console, '_stream_step', side_effect=scan):
            console._run_ble(job, 'scan', '', 30, [])
        self.assertFalse(job.result['connection_verified'])
        self.assertEqual(job.result['devices'], devices)

    def test_connect_reads_files_from_explicit_device(self):
        console = console_module()
        job = console.Job('test-connect', 'ble')
        def listing(job, label, cmd, cwd):
            job.ble_files.append({'name': 'meeting.wav', 'size': 100})
            return 0
        with patch.object(console, 'ble_python', return_value='fixture'), patch.object(console, '_stream_step', side_effect=listing) as command:
            console._run_ble(job, 'connect', '', 30, [], 'AA:BB:CC:DD:EE:FF')
        self.assertTrue(job.result['connection_verified'])
        self.assertEqual(command.call_args.args[2][-2:], ['--address', 'AA:BB:CC:DD:EE:FF'])
        self.assertEqual(job.result['files'][0]['name'], 'meeting.wav')

    def test_failed_connection_cannot_show_file_manager_as_verified(self):
        console = console_module()
        job = console.Job('test-connect', 'ble')
        def failure(job, *args):
            job.status = 'failed'
            return 1
        with patch.object(console, 'ble_python', return_value='fixture'), patch.object(console, '_stream_step', side_effect=failure):
            console._run_ble(job, 'connect', '', 30, [])
        self.assertFalse(job.result['connection_verified'])
        self.assertFalse(job.result['list_ok'])
        self.assertEqual(job.status, 'failed')
