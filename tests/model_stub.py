"""Test-only llama.cpp wire fixture. No model, tokenizer, or real inference."""

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

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload))
            if self.path == "/tokenize":
                reply = {"tokens": list(payload["content"].encode())}
            elif self.path == "/completion":
                prompt = bytes(payload["prompt"]).decode()
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
                    "content": content,
                    "tokens_predicted": len(content.encode()),
                    "tokens_evaluated": len(payload["prompt"]),
                    "stop": True,
                    "truncated": False,
                }
            else:
                self.send_error(404)
                return
            if transform:
                reply = transform(self.path, reply)
            data = json.dumps(reply).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
