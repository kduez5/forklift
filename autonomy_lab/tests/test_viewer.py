import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from autonomy_lab.server import ViewerServer, progress


class ViewerTests(unittest.TestCase):
    def test_incomplete_progress_is_ignored_and_last_episode_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "training.jsonl"
            path.write_bytes(b'{"episode":1}\n{"episode":2}\n{"episode":')
            self.assertEqual([r["episode"] for r in progress(path)], [1, 2])

    def test_loopback_viewer_serves_evidence_and_rejects_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "evaluation.json").write_text('{"mode":"simulation"}', encoding="utf-8")
            server = ViewerServer(0, path, path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(method, route, headers=None):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                    connection.request(method, route, headers=headers or {})
                    response = connection.getresponse()
                    status, body = response.status, response.read()
                    connection.close()
                    return status, body
                status, body = request("GET", "/api/state")
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["motor_output"])
                self.assertEqual(json.loads(body)["evaluation"]["mode"], "simulation")
                self.assertEqual(request("POST", "/api/drive")[0], 405)
                self.assertEqual(request("GET", "/../checkpoint.json")[0], 404)
                self.assertEqual(request("GET", "/api/trace?seed=../../checkpoint")[0], 404)
                self.assertEqual(request("GET", "/api/trace?policy=candidate")[0], 404)
                self.assertEqual(request("GET", "/api/state", {"Host": "external.example"})[0], 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
