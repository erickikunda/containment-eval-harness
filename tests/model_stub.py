"""Test-only Ollama wire fixture. No model or real inference."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def model_server(transform=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload))
            if self.path == "/api/tags":
                reply = {
                    "models": [
                        {
                            "name": "LiquidAI/lfm2.5-1.2b-instruct:latest",
                            "digest": (
                                "e0b3029914daaf4236d5040ed72cef10c8d283946d20ac87abf51bdd07660228"
                            ),
                            "details": {"format": "gguf"},
                        }
                    ]
                }
            elif self.path == "/api/generate":
                prompt = payload["prompt"]
                context = json.loads(prompt.split("\n", 1)[1].rsplit("\nNext JSON", 1)[0])
                history = context["history"]
                if not history:
                    message = {"kind": "tool", "name": "lookup", "arguments": {"key": "greeting"}}
                elif len(history) == 2:
                    message = {
                        "kind": "tool",
                        "name": "echo",
                        "arguments": {"text": history[-1]["result"]},
                    }
                else:
                    message = {"kind": "finish", "text": history[-1]["result"]}
                content = json.dumps(message)
                reply = {
                    "response": content,
                    "eval_count": len(content.encode()),
                    "prompt_eval_count": len(payload["prompt"]),
                    "done": True,
                    "done_reason": "stop",
                    "model": payload["model"],
                }
            else:
                self.send_error(404)
                return
            if transform:
                reply = transform(self.path, reply)
            data = json.dumps(reply).encode()
            try:
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
