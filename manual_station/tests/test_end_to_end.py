"""Actual local HTTP + controller + mock backend integration, never device I/O."""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from manual_station.backends import MockBackend, RealBackend
from manual_station.config import load_config
from manual_station.controller import Controller
from manual_station.server import ROOT, StationServer


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.template.json")
        self.backend = MockBackend(self.cfg)
        self.controller = Controller(self.cfg, self.backend, db_path=Path(self.temp.name) / "test.sqlite")
        self.server = StationServer(("127.0.0.1", 0), self.controller, "smoke-test")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.controller.close()
        self.temp.cleanup()

    def request(self, action=None, body=None, lease=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Authorization": "Bearer smoke-test", "Content-Type": "application/json"}
        if lease:
            headers["X-Control-Lease"] = lease
        conn.request("POST" if action else "GET", "/api/" + action if action else "/api/state",
                     json.dumps(body or {}) if action else None, headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_claim_arm_hold_stop_estop_reset(self):
        self.assertEqual(self.backend.commands, [])
        code, state = self.request()
        self.assertEqual((code, state["state"]), (200, "DISARMED"))
        _, claim = self.request("claim", {"label": "mock operator"})
        lease = claim["lease"]
        code, _ = self.request("claim", {"label": "second operator"})
        self.assertEqual(code, 409)
        _, before_arm = self.request(lease=lease)
        code, armed = self.request("arm", {"lease": lease, "scope": "wheels", "ack": True,
                                          "lifecycle_permit": before_arm["lifecycle_permit"]})
        self.assertEqual((code, armed["state"]), (200, "ARMED"))
        self.assertFalse(any(item["event"] == "apply" for item in self.backend.commands))
        code, moving = self.request("command", {"lease": lease, "permit": armed["permit"], "seq": 1,
                                                "command": "left", "duty": 20})
        self.assertEqual((code, moving["state"]), (200, "MOVING"))
        self.assertEqual({abs(value) for value in self.backend.outputs.values()}, {20})
        self.assertNotIn("lift", self.backend.outputs)
        # Polling is not a movement heartbeat and cannot keep outputs alive.
        cutoff = time.monotonic() + 0.5
        while time.monotonic() < cutoff:
            time.sleep(0.06)
            _, state = self.request(lease=lease)
        self.assertEqual(state["state"], "DISARMED")
        self.assertFalse(self.backend.outputs)
        code, _ = self.request("command", {"lease": lease, "permit": moving["permit"], "seq": 2,
                                          "command": "forward", "duty": 20})
        self.assertEqual(code, 409)
        _, stopped = self.request("estop")
        self.assertEqual(stopped["state"], "ESTOP")
        code, _ = self.request("arm", {"lease": lease, "scope": "wheels", "ack": True,
                                      "lifecycle_permit": before_arm["lifecycle_permit"]})
        self.assertEqual(code, 409)
        _, before_reset = self.request(lease=lease)
        _, reset = self.request("reset", {"lease": lease, "lifecycle_permit": before_reset["lifecycle_permit"]})
        self.assertEqual(reset["state"], "DISARMED")
        self.assertFalse(self.backend.outputs)

    def test_real_template_rejected_before_gpio_import(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = RealBackend(self.cfg, lock_path=Path(directory) / "never-created.lock")
            controller = Controller(self.cfg, backend, mode="real", db_path=Path(directory) / "real-test.sqlite")
            try:
                lease = controller.request("claim", {"label": "offline config test"})["lease"]
                with patch.object(RealBackend, "_load_gpio", side_effect=AssertionError("Device import prohibited")) as device:
                    with self.assertRaises(ValueError):
                        controller.request("arm", {"lease": lease, "scope": "wheels", "ack": True,
                            "lifecycle_permit": controller.snapshot(lease)["lifecycle_permit"]})
                    device.assert_not_called()
                self.assertEqual(controller.snapshot(lease)["state"], "DISARMED")
                self.assertFalse((Path(directory) / "never-created.lock").exists())
            finally:
                controller.close()

    def test_default_import_and_config_checks_block_hardware_modules(self):
        script = r'''
import importlib.abc, sys
class NoHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'lgpio','gpiozero','gpiod','serial','picamera2','rplidar'}:
            raise AssertionError('hardware import: ' + fullname)
sys.meta_path.insert(0, NoHardware())
from manual_station.server import main
assert main(['--check-config']) == 0
assert not {'lgpio','gpiozero','gpiod','serial','picamera2','rplidar'} & sys.modules.keys()
print('NO_DEVICE_IMPORTS_OK')
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT.parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NO_DEVICE_IMPORTS_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
