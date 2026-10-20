"""Minimal HTTP server implementing Ollama's /api/tags and /api/chat, for testing the client code."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeOllama:
    def __init__(self, models=("llama3:latest",), reply=None, fail_chat=False):
        self.models, self.reply, self.fail_chat, self.requests = list(models), reply, fail_chat, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if outer.fail_chat:
                    return self._send(500, {"error": "model crashed"})
                if body["model"] not in outer.models:
                    return self._send(404, {"error": f"model '{body['model']}' not found"})
                self._send(200, {"message": {"role": "assistant", "content": outer.reply or "ok"}})

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
