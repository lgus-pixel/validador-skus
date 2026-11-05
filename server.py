#!/usr/bin/env python3
"""
Validador web de SKUs (Plaza Vea / Makro).

Sirve la página (static/index.html) y la API que usa monitor.check().
Sin dependencias externas: solo la biblioteca estándar de Python 3.9+.

    python3 server.py               # http://0.0.0.0:8080
    PORT=9000 python3 server.py

API:
    POST /api/validar   {"rows": [{...columnas libres...}, ...]}  (máx. 100 filas por llamada)
      -> {"results": [...]}
    GET  /health        -> ok
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import monitor

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "static" / "index.html"
MAX_ROWS = 100          # por petición; el navegador envía la lista en lotes
MAX_BODY = 2_000_000    # 2 MB
WORKERS = int(os.environ.get("WORKERS", "8"))
# Límite global de lotes simultáneos para no saturar el sitio (rate limit / WAF).
GATE = threading.BoundedSemaphore(int(os.environ.get("MAX_BATCHES", "3")))


class Handler(BaseHTTPRequestHandler):
    server_version = "ValidadorSKU/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")  # que el navegador tome siempre la última versión
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/validar":
            return self._send(404, "not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._json(413, {"error": "Cuerpo vacío o demasiado grande"})
        try:
            rows = json.loads(self.rfile.read(length)).get("rows")
            assert isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
        except Exception:
            return self._json(400, {"error": "JSON inválido: se espera {\"rows\": [...]}"})
        if len(rows) > MAX_ROWS:
            return self._json(413, {"error": f"Máximo {MAX_ROWS} filas por petición"})
        with GATE:
            results = monitor.check_many([monitor.norm_row(r) for r in rows], WORKERS)
        self._json(200, {"results": results})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"Validador de SKUs en http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
