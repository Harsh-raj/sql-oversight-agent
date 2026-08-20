"""
A minimal HTTP server that speaks just enough of Ollama's real API
(/api/chat, /api/embeddings) to drive the full agent graph end to end.

This is deliberately different from the `unittest.mock.patch("ollama.chat", ...)`
approach used in test_graph_integration.py — that patches out the Python
function entirely, never exercising the real `ollama` client library's
HTTP request building or response parsing. This stub runs a real HTTP
server and points a real `ollama.Client` at it, so response-schema
mismatches would be caught here too, not just assumed away by a mock.

Response shapes verified directly against the installed `ollama` package's
`_types.py` (ChatResponse requires `message: {role, content}`;
EmbeddingsResponse requires `embedding: [floats]`) rather than assumed.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _decide_chat_content(system_prompt: str) -> str:
    lowered = system_prompt.lower()
    if "break a business question" in lowered:
        return "- How many courses are in the Design category?"
    if "check whether a business question" in lowered:
        return "CLEAR"
    if "actually answers the business question" in lowered:
        return "VALID"
    return "SELECT COUNT(*) FROM course_listings WHERE category = 'Design'"


class FakeOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence default request logging in test output

    def _send_json(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        request_data = json.loads(raw_body or b"{}")

        if self.path == "/api/chat":
            messages = request_data.get("messages", [])
            system_prompt = messages[0]["content"] if messages else ""
            content = _decide_chat_content(system_prompt)
            self._send_json(
                {
                    "model": request_data.get("model", "stub-model"),
                    "created_at": "2026-01-01T00:00:00Z",
                    "message": {"role": "assistant", "content": content},
                    "done": True,
                }
            )
        elif self.path == "/api/embeddings":
            self._send_json({"embedding": [0.1] * 384})
        else:
            self.send_response(404)
            self.end_headers()


def start_fake_ollama_server():
    """Binds to an OS-assigned free port (port=0) and serves in a
    background thread. Returns (server, port) — caller must call
    server.shutdown() when done."""
    server = HTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port
