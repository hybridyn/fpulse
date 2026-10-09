"""Loopback demo API: GET fixtures, POST results, GET stored results."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health"):
            return self.respond(200, {"service": "fpulse-multi-system-demo"})
        if self.path == "/orders":
            return self.respond(200, json.loads((ROOT / "fixtures.json").read_text()))
        if self.path == "/results/shipments":
            target = ROOT / "runtime" / "api-shipments.json"
            return self.respond(200, json.loads(target.read_text()) if target.exists() else [])
        self.respond(404, {"error": "Unknown demo endpoint"})

    def do_POST(self):
        if self.path != "/results/shipments":
            return self.respond(404, {"error": "Unknown demo endpoint"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self.respond(413, {"error": "Expected a JSON body under 1 MB"})
            rows = json.loads(self.rfile.read(length))
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Expected an array of objects")
        except (ValueError, UnicodeDecodeError):
            return self.respond(400, {"error": "Expected an array of JSON objects"})
        target = ROOT / "runtime" / "api-shipments.json"
        target.parent.mkdir(exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)
        self.respond(200, {"stored": len(rows)})


if __name__ == "__main__":
    print("Demo API listening on http://127.0.0.1:28080", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 28080), Handler).serve_forever()
