"""Lifecycle regressions without a card, browser, or HTTP listener."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "qs668_console", Path(__file__).resolve().parents[1] / "qs668-console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class WatcherTests(unittest.TestCase):
    def run_sequence(self, mounts, enabled=None, open_results=None, import_fail=False):
        tick = [0]
        stop = {"stop": False}
        opened = []
        results = iter(open_results or [0] * len(mounts))
        collector = SimpleNamespace(
            wired_volumes=lambda: [Path("/Volumes") / n for n in mounts[tick[0]]],
            card_files=lambda _: [])

        def sleep(_):
            tick[0] += 1
            stop["stop"] = tick[0] >= len(mounts)

        def run(cmd, **kwargs):
            rc = 0
            if cmd[0] == "open":
                opened.append(cmd)
                rc = next(results)
            return SimpleNamespace(returncode=rc, stderr="simulated failure" if rc else "")

        with patch.object(console, "_collector_mod", side_effect=(
                [RuntimeError("temporary import failure"), collector] if import_fail else None),
                return_value=collector), patch.object(
                console, "load_state", side_effect=lambda: {
                    "auto_open": enabled[tick[0]] if enabled else True}), patch.object(
                console.time, "sleep", side_effect=sleep), patch.object(
                console.subprocess, "run", side_effect=run):
            console.start_watcher(8765, stop)
        return len(opened)

    def test_existing_mount_opens_once(self):
        self.assertEqual(self.run_sequence([["NO NAME"]] * 3), 1)

    def test_reinsert_opens_again(self):
        self.assertEqual(self.run_sequence([["NO NAME"], [], ["NO NAME"]]), 2)

    def test_enable_without_restart(self):
        self.assertEqual(self.run_sequence([["NO NAME"]] * 2, [False, True]), 1)

    def test_disabled_does_not_open(self):
        self.assertEqual(self.run_sequence([["NO NAME"]], [False]), 0)

    def test_open_failure_retries(self):
        self.assertEqual(self.run_sequence([["NO NAME"]] * 3, open_results=[1, 0]), 2)

    def test_import_failure_recovers(self):
        self.assertEqual(self.run_sequence([["NO NAME"]] * 2, import_fail=True), 1)

    def test_shutdown_closes_server_and_releases_lock(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                console, "BASE", Path(tmp)), patch("sys.argv", ["qs668-console.py"]), patch.object(
                console, "pick_port", return_value=8765), patch.object(
                console, "load_state", return_value={"auto_open": True}), patch.object(
                console, "save_state"), patch.object(console, "ThreadingHTTPServer", return_value=server), patch.object(
                console.threading, "Thread") as thread:
            self.assertEqual(console.main(), 0)
            server.server_close.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["args"][1]["stop"])
            with (Path(tmp) / ".qs668-console.lock").open("a+") as lock:
                console.fcntl.flock(lock, console.fcntl.LOCK_EX | console.fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
