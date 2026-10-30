"""Boo-king Desk demo server. Stdlib only: python3 server.py, then open http://localhost:8765"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import Desk  # noqa: E402

DESK = Desk()
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _tts(self, text, voice):
        from llm import tts_stream
        if not os.environ.get("LLM_BASE_URL"):
            return self._json({"error": "Voice needs the gateway (LLM_BASE_URL)."}, 400)
        try:
            chunks = tts_stream(text, voice)
            first = next(chunks)  # fail before sending headers if the gateway errors
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)
        self.send_response(200)
        self.send_header("content-type", "audio/mpeg")
        self.end_headers()  # HTTP/1.0: no length, the body ends when we close
        try:
            self.wfile.write(first)
            for c in chunks:
                self.wfile.write(c)
        except (BrokenPipeError, ConnectionResetError):
            pass  # caller started speaking and we stopped playback

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("cache-control", "no-store")  # always load the current page after an update
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/api/tts?"):
            # GET so an <audio> element can start playing while the mp3 is still streaming in.
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            self._tts(q.get("text", [""])[0], q.get("voice", [None])[0])
        elif self.path.startswith("/api/state"):
            since = int(self.path.split("since=")[1]) if "since=" in self.path else 0
            self._json(DESK.state(since))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        b = self._body()
        p = self.path
        if p == "/api/chat":
            self._json(DESK.chat(b["session"], b["text"]))
        elif p == "/api/config":
            # Set the model backend from the page instead of env vars. Empty values clear.
            for k in ("LLM_BASE_URL", "MODEL", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
                if k in b:
                    if b[k]:
                        os.environ[k] = b[k].strip()
                    else:
                        os.environ.pop(k, None)
            from llm import backend_name
            self._json({"backend": backend_name()})
        elif p == "/api/models":
            from llm import list_models
            try:
                self._json({"models": list_models()})
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}. On VNet/Teleport?"}, 502)
        elif p == "/api/tts":
            self._tts(b.get("text", ""), b.get("voice"))
        elif p == "/api/reset":
            DESK.reset()
            self._json({"ok": True})
        elif p == "/api/flip":
            self._json(DESK.flip_slot(b["slot_id"]))
        elif p == "/api/dry_run":
            DESK.dry_run = bool(b["on"])
            self._json({"dry_run": DESK.dry_run})
        elif p == "/api/review":
            DESK.review(b["request"], b["verdict"], b.get("correction", ""))
            self._json({"ok": True})
        elif p == "/api/propose_rules":
            try:
                self._json(DESK.propose_rules())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif p == "/api/approve_rule":
            DESK.approve_rule(int(b["index"]))
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    from llm import backend_name, list_models
    if "--models" in sys.argv:
        print("\n".join(list_models()))
        sys.exit()
    print(f"Boo-king Desk on http://localhost:{PORT}  (model: {backend_name() or 'NOT CONFIGURED'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
