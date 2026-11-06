"""Langflow tiruan untuk uji otomatis (tanpa Gemini)."""
import json, threading, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLOW_ID = "35a26c4b-ebaf-4cac-a624-70c747e810cd"
FLOW = {"id": FLOW_ID, "name": "Omni", "endpoint_name": "app-medis", "data": {"nodes": [
    {"id": "File-Nn09e", "data": {"type": "File", "node": {"display_name": "File", "template": {"path": {}}}}},
    {"id": "TextInput-jE0Kv", "data": {"type": "TextInput", "node": {"display_name": "Anamnesis_pasien"}}},
    {"id": "Agent-xyz", "data": {"type": "Agent", "node": {"display_name": "Agent", "template": {
        "model": {"value": [{"name": "gemini-2.5-flash", "provider": "Google"}]},
        "max_tokens": {"value": 1000}, "temperature": {"value": 0.1}}}}},
]}}

APPMODE = {"id": FLOW_ID, "name": "Omni", "endpoint_name": "app-medis", "data": {
    "nodes": [
        {"id": "TextInput-AAA", "data": {"type": "TextInput", "node": {"display_name": "Text Input"}}},
        {"id": "TextInput-BBB", "data": {"type": "TextInput", "node": {"display_name": "Text Input"}}},
        {"id": "Prompt-1", "data": {"type": "Prompt", "node": {"display_name": "Prompt Template"}}},
        {"id": "Agent-xyz", "data": {"type": "Agent", "node": {"display_name": "Agent", "template": {
            "model": {"value": [{"name": "gemini-2.5-flash"}]}}}}},
    ],
    "edges": [
        # BBB -> Dokumen (format dict), AAA -> anamnesis_pasien (format string ber-'\u0153' ala Langflow lama)
        {"source": "TextInput-BBB", "target": "Prompt-1", "data": {"targetHandle": {"fieldName": "Dokumen"}}},
        {"source": "TextInput-AAA", "target": "Prompt-1",
         "targetHandle": "{\u0153fieldName\u0153:\u0153anamnesis_pasien\u0153,\u0153id\u0153:\u0153Prompt-1\u0153}"},
    ]}}


class State:
    variant = "classic"
    upload_status = 200
    run_script = []      # daftar (status, body) berurutan; kosong = sukses
    last_run = None
    require_key = "k"
    health_ok = True

def make_handler(S):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def _auth(self):
            if self.path.startswith("/health"): return True
            if S.require_key and self.headers.get("x-api-key") != S.require_key:
                self._send(403, {"detail": "invalid key"}); return False
            return True
        def do_GET(self):
            if not self._auth(): return
            if self.path.startswith("/health"): return self._send(200 if S.health_ok else 500, {"status": "ok"})
            if re.match(r"^/api/v1/flows/\?", self.path) or self.path == "/api/v1/flows/":
                return self._send(200, [{"id": FLOW_ID, "name": "Omni", "endpoint_name": "app-medis"}])
            if self.path.startswith(f"/api/v1/flows/{FLOW_ID}"): return self._send(200, APPMODE if S.variant == "appmode" else FLOW)
            self._send(404, {"detail": "nf"})
        def do_POST(self):
            if not self._auth(): return
            n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
            if self.path.startswith("/api/v1/files/upload/"):
                if S.upload_status != 200: return self._send(S.upload_status, {"detail": "upload gagal"})
                return self._send(200, {"flowId": FLOW_ID, "file_path": f"{FLOW_ID}/x.pdf"})
            if self.path.startswith("/api/v1/run/"):
                S.last_run = json.loads(body)
                if S.run_script:
                    code, txt = S.run_script.pop(0)
                    if code != 200: return self._send(code, {"detail": txt})
                ans = "## LAPISAN 1 - DOKTER\nHb rendah\n## LAPISAN 2 - PASIEN\nDarah kurang\n## LAPISAN 3 - EMR\nAnemia"
                return self._send(200, {"outputs": [{"outputs": [{"results": {"message": {"text": ans}}}]}]})
            self._send(404, {"detail": "nf"})
    return H

def start(S, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(S))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
