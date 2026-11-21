"""Read-only loopback viewer for simulation training and separate real snapshots."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent


def read_json(path: Path, fallback=None):
    try:
        if path.stat().st_size > 24_000_000:
            return fallback
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return fallback


def progress(path: Path):
    if not path.exists():
        return []
    # Training appends complete lines; ignore the last incomplete line.
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 1_000_000))
        raw = stream.read()
    lines = raw.splitlines()
    if size > 1_000_000:
        lines = lines[1:]
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except (ValueError, UnicodeError):
            continue
    stride = max(1, len(rows) // 400)
    sampled = rows[::stride]
    if rows and (not sampled or sampled[-1] != rows[-1]):
        sampled.append(rows[-1])
    return sampled


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, run_dir: Path, observations: Path, candidate_dir: Path | None = None):
        self.run_dir = run_dir.resolve()
        self.observations = observations.resolve()
        self.candidate_dir = candidate_dir.resolve() if candidate_dir else None
        super().__init__(("127.0.0.1", port), ViewerHandler)


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, data, mime="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self.reply(403, {"error": "loopback host required"})
        route = urlsplit(self.path).path
        if route == "/":
            return self.reply(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if route == "/api/state":
            return self.reply(200, {
                "mode": "simulation_only", "motor_output": False,
                "training": progress(self.server.run_dir / "training.jsonl"),
                "evaluation": read_json(self.server.run_dir / "evaluation.json"),
                "manifest": read_json(self.server.run_dir / "run_manifest.json"),
                "real_observation": read_json(self.server.observations / "real_observation.json"),
                "diagnostics": read_json(self.server.run_dir / "diagnostics" / "failure_summary.json"),
                "contracts": read_json(self.server.observations / "contract_validation.json"),
                "candidate_comparison": read_json(self.server.candidate_dir / "comparison.json") if self.server.candidate_dir else None,
            })
        if route == "/api/trace":
            if parse_qs(urlsplit(self.path).query).get("policy") == ["candidate"]:
                if self.server.candidate_dir is None:
                    return self.reply(404, {"error": "candidate run not configured"})
                return self.reply(200, read_json(self.server.candidate_dir / "representative_trace.json", {}))
            seed = parse_qs(urlsplit(self.path).query).get("seed", [""])[0]
            if seed:
                evaluation = read_json(self.server.run_dir / "evaluation.json", {})
                failed_seeds = {str(row["seed"]) for row in evaluation.get("learned", {}).get("episodes_detail", []) if not row["success"]}
                if seed not in failed_seeds:
                    return self.reply(404, {"error": "unknown failure trace"})
                return self.reply(200, read_json(self.server.run_dir / "diagnostics" / f"trace_{seed}.json", {}))
            return self.reply(200, read_json(self.server.run_dir / "representative_trace.json", {}))
        if route == "/api/real-lidar":
            return self.reply(200, read_json(self.server.observations / "real_lidar.json", {}))
        if route == "/real-camera.jpg":
            try:
                return self.reply(200, (self.server.observations / "real_camera.jpg").read_bytes(), "image/jpeg")
            except OSError:
                return self.reply(404, {"error": "no camera snapshot"})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        self.reply(405, {"error": "read-only viewer; no actuator or training endpoint"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--observations", type=Path, default=Path("outputs/rl_resume_20261121"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--candidate-dir", type=Path)
    args = parser.parse_args()
    with ViewerServer(args.port, args.run_dir, args.observations, args.candidate_dir) as server:
        print(f"Read-only learning viewer: http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
