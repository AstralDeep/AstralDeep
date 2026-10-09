"""
Unit tests for the read‑only installation doctor (scripts/install_doctor.py).

The tests mock out socket connections to simulate various environment states
without requiring real services.
"""

import json
import types
import unittest
from unittest import mock

# Import the module under test using importlib to avoid executing the script's
# __main__ block.
import importlib.util
import pathlib

MODULE_PATH = pathlib.Path(__file__).parent.parent / "scripts" / "install_doctor.py"
spec = importlib.util.spec_from_file_location("install_doctor", MODULE_PATH)
install_doctor = importlib.util.module_from_spec(spec)  # type: ignore
assert spec and spec.loader
spec.loader.exec_module(install_doctor)  # type: ignore


class TestInstallDoctor(unittest.TestCase):
    def setUp(self):
        # Patch socket.create_connection for each test.
        patcher = mock.patch("socket.create_connection")
        self.addCleanup(patcher.stop)
        self.mock_create = patcher.start()

    def _mock_connection(self, succeed: bool):
        """
        Helper to configure the mock to either succeed (return a dummy context
        manager) or raise an OSError with a custom message.
        """
        if succeed:
            # Return a dummy object that can be used as a context manager.
            dummy = mock.MagicMock()
            dummy.__enter__.return_value = None
            dummy.__exit__.return_value = None
            self.mock_create.return_value = dummy
        else:
            self.mock_create.side_effect = OSError("connection refused")

    def test_all_services_ready(self):
        # All services succeed.
        self._mock_connection(succeed=True)
        report = install_doctor.diagnose()
        self.assertEqual(report.overall, "ready")
        for svc in report.services:
            self.assertTrue(svc.reachable)
            self.assertEqual(svc.error, "")

        # JSON rendering must be deterministic.
        json_output = json.dumps(report.to_dict(), sort_keys=True)
        parsed = json.loads(json_output)
        self.assertEqual(parsed["overall"], "ready")
        self.assertEqual(len(parsed["services"]), len(install_doctor.REQUIRED_SERVICES))

    def test_partial_services_reachable(self):
        # First service reachable, others fail.
        def side_effect(addr, timeout=1.0):
            host, port = addr
            if port == install_doctor.REQUIRED_SERVICES[0][2]:
                return mock.MagicMock()
            raise OSError("refused")

        self.mock_create.side_effect = side_effect
        report = install_doctor.diagnose()
        self.assertEqual(report.overall, "repairable")
        reachable = [svc for svc in report.services if svc.reachable]
        self.assertEqual(len(reachable), 1)

    def test_no_services_reachable(self):
        self._mock_connection(succeed=False)
        report = install_doctor.diagnose()
        self.assertEqual(report.overall, "unsupported")
        for svc in report.services:
            self.assertFalse(svc.reachable)
            self.assertIn("connection refused", svc.error)

    def test_human_rendering(self):
        self._mock_connection(succeed=True)
        report = install_doctor.diagnose()
        human = install_doctor.render_human(report)
        self.assertIn("Overall status: ready", human)
        for name, _, _ in install_doctor.REQUIRED_SERVICES:
            self.assertIn(name, human)

    def test_cli_exit_codes(self):
        # Ready -> 0
        self._mock_connection(succeed=True)
        with mock.patch.object(sys, "argv", ["install_doctor.py"]):
            exit_code = install_doctor.main([])
            self.assertEqual(exit_code, 0)

        # Repairable -> 1
        self._mock_connection(succeed=False)
        # make only the first service succeed
        def side_effect(addr, timeout=1.0):
            host, port = addr
            if port == install_doctor.REQUIRED_SERVICES[0][2]:
                return mock.MagicMock()
            raise OSError("refused")
        self.mock_create.side_effect = side_effect
        with mock.patch.object(sys, "argv", ["install_doctor.py", "--human"]):
            exit_code = install_doctor.main([])
            self.assertEqual(exit_code, 1)

        # Unsupported -> 2
        self._mock_connection(succeed=False)
        with mock.patch.object(sys, "argv", ["install_doctor.py", "--json"]):
            exit_code = install_doctor.main([])
            self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
