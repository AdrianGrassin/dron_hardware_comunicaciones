"""Recibe JPEG del dron: --check N comprueba; sin --check ofrece visor local.

Solo biblioteca estándar. No cifra ni interpreta MAVLink; no manda órdenes.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PATHS = {"/rgb.jpg": "image/jpeg", "/thermal.jpg": "image/jpeg", "/status": "application/json"}
PAGE = """<!doctype html><html lang="es"><meta charset="utf-8">
<title>Vídeo Jetson → PC</title><style>
body{font:18px system-ui;background:#101d2b;color:#eef4fb;margin:32px}
img{max-width:100%;max-height:75vh}small{color:#b9cbdc}
</style><h1>Vídeo Jetson → PC</h1><p id="state">Conectando…</p>
<img id="frame" alt="Fotograma recibido" hidden>
<p><small>JPEG por HTTP · sin cifrado adicional · cierre: Ctrl+C en la terminal</small></p>
<script>
const picture=document.querySelector('#frame'), state=document.querySelector('#state');
let previous=null;
async function update(){
  try {
    const response=await fetch('/__CHANNEL__.jpg',{cache:'no-store'});
    if(!response.ok) throw Error('HTTP '+response.status+' · no hay imagen disponible');
    const blob=await response.blob(), url=URL.createObjectURL(blob);
    await new Promise((resolve,reject)=>{picture.onload=resolve;picture.onerror=reject;picture.src=url;});
    if(previous) URL.revokeObjectURL(previous); previous=url;
    picture.hidden=false; state.textContent='Canal __CHANNEL__ · '+blob.size+' bytes';
  } catch(error){picture.hidden=true;state.textContent=String(error);}
  setTimeout(update,125);
} update();
</script></html>"""


def fetch(base_url, path, timeout=3.0):
    """Recibe una respuesta completa, limitada; conserva bytes y metadatos."""
    if path not in PATHS:
        raise ValueError("Ruta no permitida")
    start = time.perf_counter()
    request = Request(base_url.rstrip("/") + path, headers={"Cache-Control": "no-cache"})
    limit = 1024 * 1024 if path == "/status" else 16 * 1024 * 1024
    with urlopen(request, timeout=timeout) as response:
        if response.headers.get_content_type() != PATHS[path]:
            raise ValueError("Tipo de respuesta inesperado")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Respuesta demasiado grande")
        headers = {k: v for k, v in response.headers.items() if k.lower().startswith("x-frame-")}
    if path == "/status":
        json.loads(data)
    elif not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        raise ValueError("La respuesta no contiene un JPEG completo")
    return data, headers, (time.perf_counter() - start) * 1000


def check(base_url, channel, count, interval, timeout):
    timings, identities, failures = [], set(), 0
    identified = 0
    for i in range(count):
        try:
            data, headers, elapsed = fetch(base_url, f"/{channel}.jpg", timeout)
            h = {k.lower(): v for k, v in headers.items()}
            identity = (h.get("x-frame-session"), h.get("x-frame-id"))
            if all(identity):
                identities.add(identity)
                identified += 1
            timings.append(elapsed)
            print(json.dumps({"sample": i + 1, "bytes": len(data),
                              "request_ms": round(elapsed, 3), "frame_id": identity[1]}), flush=True)
        except (OSError, ValueError, URLError, HTTPException) as exc:
            failures += 1
            # No imprimir URLs que pudieran incluir credenciales.
            print(json.dumps({"sample": i + 1, "error": type(exc).__name__,
                              "http_status": getattr(exc, "code", None)}), flush=True)
        if i + 1 < count:
            time.sleep(interval)
    print(json.dumps({"successful_requests": len(timings), "failures": failures,
                      "identified_requests": identified,
                      "unique_frames": len(identities) if identified else None,
                      "median_request_ms": statistics.median(timings) if timings else None,
                      "measurement": "HTTP request, not camera-to-screen latency"}), flush=True)
    return 1 if failures else 0


def make_handler(base_url, channel="rgb", timeout=3.0):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5.0)

        def reply(self, code, body, content_type, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                return self.reply(200, PAGE.replace("__CHANNEL__", channel).encode(), "text/html; charset=utf-8")
            if path not in PATHS:
                return self.reply(404, b"Unknown path", "text/plain")
            try:
                data, headers, _ = fetch(base_url, path, timeout)
                # Reenviar los bytes; nunca decodificar y volver a comprimir.
                self.reply(200, data, PATHS[path], headers)
            except HTTPError as exc:
                self.reply(exc.code if exc.code in (404, 503) else 502,
                           b"Source has no available frame", "text/plain")
            except (OSError, ValueError, URLError, HTTPException):
                self.reply(502, b"Cannot receive source", "text/plain")

        def do_POST(self):
            self.reply(405, b"Only GET supported", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, *args):
            pass
    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8089", help="Servidor de imágenes de la Jetson")
    p.add_argument("--channel", choices=("rgb", "thermal"), default="rgb")
    p.add_argument("--port", type=int, default=8090, help="Visor local en el PC")
    p.add_argument("--check", type=int, help="Comprobar N peticiones y salir, sin visor")
    p.add_argument("--interval", type=float, default=.15)
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--duration", type=float, help="Cerrar el visor tras N segundos")
    args = p.parse_args(argv)
    url = urlsplit(args.url)
    if url.scheme not in ("http", "https") or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
        p.error("--url debe ser http(s)://IP:PUERTO, sin ruta, query ni fragmento")
    if not 1 <= args.port <= 65535 or (args.check is not None and args.check <= 0):
        p.error("Puerto o número de peticiones inválido")
    for value in (args.timeout, args.duration):
        if value is not None and (not math.isfinite(value) or value <= 0):
            p.error("Tiempo inválido")
    if not math.isfinite(args.interval) or args.interval < 0:
        p.error("Intervalo inválido")
    if args.check is not None:
        return check(args.url, args.channel, args.check, args.interval, args.timeout)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.url, args.channel, args.timeout))
    server.daemon_threads = True
    server.timeout = .2
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    print(f"Abre http://127.0.0.1:{args.port} en el navegador de este PC. Ctrl+C para cerrar.", flush=True)
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
