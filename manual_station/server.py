"""Loopback-only manual station; default mock, no hardware access at startup."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import tempfile
from urllib.parse import urlsplit

from .backends import MockBackend, RealBackend
from .config import load_config, validate_real
from .controller import Controller

ROOT = Path(__file__).resolve().parent
MAX_BODY = 4096
ACTIONS = frozenset(("claim", "release", "arm", "command", "stop", "estop", "reset", "note"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_body(raw):
    def bad_constant(_value):
        raise ValueError("Non-finite JSON number")
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=bad_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


class StationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, controller, token):
        if address[0] != "127.0.0.1":
            raise ValueError("Only 127.0.0.1 is allowed; use SSH port forwarding")
        self.controller = controller
        self.token = token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ManualStation/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(2.0)

    def log_message(self, *_args):
        # Do not write launch tokens, lease tokens, headers, or note text to stdout.
        pass

    def _reply(self, status, payload, content_type="application/json; charset=utf-8"):
        # On Windows closing with an unread POST body can reset the socket and
        # discard even a completed rejection response. Drain only a tiny,
        # declared body, with a short timeout; never wait for an unbounded peer.
        if status >= 400 and self.command == "POST" and not getattr(self, "_body_read", False):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 < size <= MAX_BODY and not self.headers.get("Transfer-Encoding"):
                    self.connection.settimeout(0.1)
                    self.rfile.read(size)
                    self._body_read = True
            except (ValueError, OSError):
                pass
            finally:
                self.connection.settimeout(2.0)
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        self.close_connection = True

    def _host_ok(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        try:
            parsed = urlsplit("http://" + hosts[0])
            good = parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == self.server.server_port
        except ValueError:
            return False
        if not good or parsed.username or parsed.password or parsed.path:
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            return False
        origin = origins[0] if origins else None
        return origin is None or origin in (f"http://localhost:{parsed.port}", f"http://127.0.0.1:{parsed.port}")

    def _authorized(self):
        auth = self.headers.get_all("Authorization", [])
        return len(auth) == 1 and hmac.compare_digest(auth[0].encode("utf-8"), ("Bearer " + self.server.token).encode("utf-8"))

    def _boundary(self, api=True):
        if not self._host_ok():
            self._reply(403, {"error": "로컬 주소와 같은 출처로 접속하세요. SSH 포워딩의 양쪽 포트 번호를 맞추세요."})
            return False
        if api and not self._authorized():
            self._reply(401, {"error": "이 실행 세션의 접속 코드가 필요합니다. 서버 터미널의 주소를 사용하세요."})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._boundary(api=path != "/"):
            return
        if path == "/":
            self._reply(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                result = self.server.controller.snapshot(self.headers.get("X-Control-Lease"))
                result["linux_user"] = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
                result["hardware_verified"] = False
                self._reply(200, result)
            except Exception as exc:
                self._reply(503, {"error": str(exc)})
        else:
            self._reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self._boundary():
            return
        path = urlsplit(self.path).path
        action = path.removeprefix("/api/")
        if not path.startswith("/api/") or action not in ACTIONS:
            self._reply(404, {"error": "Unknown action"})
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            self._reply(415, {"error": "application/json with Content-Length required"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError("One Content-Length required")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError("Request body must be 1..4096 bytes")
            raw = self.rfile.read(length)
            self._body_read = True
            if len(raw) != length:
                raise ValueError("Incomplete request")
            data = decode_body(raw)
        except (ValueError, UnicodeError, TimeoutError) as exc:
            self._reply(400, {"error": str(exc)})
            return
        try:
            result = self.server.controller.request(action, data)
            self._reply(200, result if result is not None else {"ok": True})
        except (ValueError, PermissionError) as exc:
            self._reply(409, {"error": str(exc)})
        except Exception as exc:
            # The controller owns fail-closed output cleanup for execution failures.
            self._reply(503, {"error": str(exc)})

    def do_OPTIONS(self):
        self._reply(403, {"error": "Cross-origin access is disabled"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("mock", "real"), default="mock")
    parser.add_argument("--config", type=Path, default=ROOT / "config.template.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check-config", action="store_true", help="Report readiness without device imports or outputs")
    parser.add_argument("--db", type=Path, default=Path.home() / ".local" / "state" / "forklift-manual" / "events.sqlite3")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    try:
        args.config = args.config.resolve()
        args.db = args.db.resolve()
        cfg = load_config(args.config)
        if args.check_config:
            readiness = {scope: validate_real(cfg, scope) for scope in ("wheels", "lift")}
            print(json.dumps({"hardware_accessed": False, "readiness_errors": readiness}, ensure_ascii=False, indent=2))
            return 0 if args.backend == "mock" or any(not errors for errors in readiness.values()) else 2
        args.db.parent.mkdir(parents=True, exist_ok=True)
        if args.backend == "real":
            if sys.platform != "linux":
                raise RuntimeError("Real mode requires Raspberry Pi Linux")
            cache = Path.home() / ".cache" / "forklift-manual"
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            # lgpio may create notification FIFOs in cwd on import. Isolate these
            # before any server/controller thread exists; never use the team folder.
            private_runtime = Path(tempfile.mkdtemp(prefix="runtime-", dir=cache))
            os.chmod(private_runtime, 0o700)
            os.chdir(private_runtime)
        backend = MockBackend(cfg) if args.backend == "mock" else RealBackend(cfg, lock_path=ROOT / "runtime" / "controller.lock")
        controller = Controller(cfg, backend, mode=args.backend, db_path=args.db)
        try:
            token = secrets.token_urlsafe(32)
            server = StationServer(("127.0.0.1", args.port), controller, token)
        except BaseException:
            controller.close()
            raise
        ending = threading.Event()
        def terminate(*_args):
            if not ending.is_set():
                ending.set()
                # shutdown must run outside the serve_forever thread.
                threading.Thread(target=server.shutdown, daemon=True).start()
        previous = {}
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                previous[sig] = signal.signal(sig, terminate)
        print(f"MODE={args.backend.upper()} | DISARMED | hardware not opened", flush=True)
        print(f"Open http://127.0.0.1:{args.port}/#token={token}", flush=True)
        print("Use the same local/remote SSH port. Do not share this session URL beyond the intended operators.", flush=True)
        print("Ctrl+C stops the station. ARM and a fresh hold input are required for any output.", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
            controller.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Station could not continue: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
