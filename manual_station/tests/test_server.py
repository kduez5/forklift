import http.client
import json
import threading
import unittest

from manual_station.server import StationServer, decode_body


class DummyController:
    def __init__(self):
        self.calls = []

    def request(self, action, data):
        self.calls.append((action, data))
        return {"ok": True}

    def snapshot(self, lease=None):
        return {"state": "DISARMED", "mode": "mock", "outputs": {}, "lease_seen": bool(lease)}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.controller = DummyController()
        self.server = StationServer(("127.0.0.1", 0), self.controller, "test-session")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def call(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_no_authorization_cannot_operate(self):
        status, _ = self.call("POST", "/api/arm", "{}", **{"Content-Type": "application/json"})
        self.assertEqual(status, 401)
        self.assertEqual(self.controller.calls, [])

    def test_cross_origin_cannot_operate(self):
        status, _ = self.call("POST", "/api/arm", "{}", **{"Content-Type": "application/json", "Authorization": "Bearer test-session", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.controller.calls, [])

    def test_host_rebinding_rejected(self):
        status, _ = self.call("GET", "/api/state", **{"Authorization": "Bearer test-session", "Host": f"other.example:{self.server.server_port}"})
        self.assertEqual(status, 403)

    def test_state_does_not_issue_any_motion(self):
        status, raw = self.call("GET", "/api/state", **{"Authorization": "Bearer test-session"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["state"], "DISARMED")
        self.assertEqual(self.controller.calls, [])

    def test_oversized_body_and_duplicate_keys_rejected(self):
        for body in ('{"duty":20,"duty":40}', '{"duty":NaN}', ' ' * 4097):
            status, _ = self.call("POST", "/api/command", body, **{"Authorization": "Bearer test-session", "Content-Type": "application/json"})
            self.assertEqual(status, 400)
        self.assertEqual(self.controller.calls, [])

    def test_valid_stop_reaches_controller(self):
        status, _ = self.call("POST", "/api/stop", "{}", **{"Authorization": "Bearer test-session", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls, [("stop", {})])

    def test_bind_all_interfaces_rejected(self):
        with self.assertRaises(ValueError):
            StationServer(("0.0.0.0", 0), self.controller, "test")

    def test_decoder_rejects_arrays(self):
        with self.assertRaises(ValueError):
            decode_body(b"[]")


if __name__ == "__main__":
    unittest.main()
