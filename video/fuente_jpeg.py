"""Publica JPEG por HTTP desde RTSP, un archivo o --demo. Python >= 3.10.

Adaptada de la fuente del proyecto (23/10). Sin control, inferencia ni cifrado.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Feed:
    def __init__(self, channel, max_age_s=3.0, clock_ns=time.monotonic_ns):
        self.channel = channel
        self.max_age_s = max_age_s
        self.clock_ns = clock_ns
        self.session = str(uuid.uuid4())
        self.lock = threading.Lock()
        self.frame = None
        self.sequence = 0
        self.error = "waiting_for_frame"

    def publish(self, jpeg, width, height, *, monotonic_ns=None, unix_ns=None):
        with self.lock:
            self.sequence += 1
            self.frame = (bytes(jpeg), {
                "channel": self.channel, "session": self.session,
                "frame_id": self.sequence, "width": int(width), "height": int(height),
                "received_monotonic_ns": self.clock_ns() if monotonic_ns is None else monotonic_ns,
                "received_unix_ns": time.time_ns() if unix_ns is None else unix_ns,
                "time_basis": "source_receipt_after_decode",
            })
            self.error = ""

    def set_error(self, error):
        with self.lock:
            self.error = error

    def snapshot(self):
        with self.lock:
            if self.frame is None:
                return None, {"channel": self.channel, "fresh": False, "error": self.error}
            jpeg, metadata = self.frame
            metadata = dict(metadata)
            age_s = max(0, (self.clock_ns() - metadata["received_monotonic_ns"]) / 1e9)
            metadata.update(age_ms=age_s * 1000, fresh=age_s < self.max_age_s, error=self.error)
            return (jpeg if metadata["fresh"] else None), metadata


def pump_capture(cap, *, local, fps, stop, publish, clock=time.monotonic):
    """Consume una apertura. El callback recibe frame y tiempos después de read()."""
    if local and (not math.isfinite(fps) or fps <= 0):
        raise ValueError("El archivo no declara FPS válidos: indicar --fps")
    deadline = clock()
    while not stop.is_set():
        if local and stop.wait(max(0.0, deadline - clock())):
            return "stopped"
        ok, frame = cap.read()
        if not ok:
            return "eof" if local else "read_failed"
        received_mono, received_unix = time.monotonic_ns(), time.time_ns()
        publish(frame, received_mono, received_unix)
        if local:
            # Si decodificar tarda más que un periodo, no recuperar con ráfagas.
            deadline = max(deadline + 1.0 / fps, clock())
    return "stopped"


def run_source(feed, source, stop, *, loop=False, fps=None, quality=82):
    import cv2

    local = not source.lower().startswith(("rtsp://", "rtsps://"))
    while not stop.is_set():
        cap = None
        try:
            if local:
                cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            else:
                cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000,
                ])
            if not cap.isOpened():
                raise RuntimeError("open_failed")
            effective_fps = fps if fps is not None else cap.get(cv2.CAP_PROP_FPS)

            def publish(frame, mono, unix):
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if not ok:
                    raise RuntimeError("jpeg_encode_failed")
                feed.publish(encoded.tobytes(), frame.shape[1], frame.shape[0],
                             monotonic_ns=mono, unix_ns=unix)

            reason = pump_capture(cap, local=local, fps=effective_fps, stop=stop, publish=publish)
            feed.set_error(reason)
            if local and not loop or reason == "stopped":
                return
            if local and reason == "eof":
                continue  # El último wait ya mantiene el periodo entre vueltas.
        except Exception as exc:
            # No publicar URL, credenciales o rutas privadas en /status.
            feed.set_error(type(exc).__name__)
            if local:
                return
        finally:
            if cap is not None:
                cap.release()
        stop.wait(2.0)


def run_demo(feed, stop, *, fps=8.0, quality=82):
    """Mismo JPEG/HTTP que la cámara, con contenido inequívocamente simulado."""
    import cv2
    import numpy as np

    width, height = (1280, 720) if feed.channel == "rgb" else (160, 120)
    index = 0
    while not stop.is_set():
        frame = np.full((height, width, 3), 35, dtype=np.uint8)
        x = (index * max(1, width // 100)) % max(1, width - width // 8)
        cv2.rectangle(frame, (x, height // 3), (x + width // 8, height * 2 // 3),
                      (50, 180, 240) if feed.channel == "rgb" else (200, 200, 200), -1)
        scale = .8 if width > 200 else .35
        cv2.putText(frame, "SIMULACION", (8, 25), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(frame, str(index), (8, height - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (255, 255, 255), 1, cv2.LINE_AA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            feed.set_error("jpeg_encode_failed")
            return
        feed.publish(encoded.tobytes(), width, height)
        index += 1
        stop.wait(1.0 / fps)


def make_handler(feeds):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5.0)

        def respond(self, code, body, content_type, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/status":
                body = json.dumps({name: feed.snapshot()[1] for name, feed in feeds.items()}).encode()
                return self.respond(200, body, "application/json")
            channel = {"/rgb.jpg": "rgb", "/thermal.jpg": "thermal"}.get(path)
            if channel not in feeds:
                return self.respond(404, b"Unknown channel or path\n", "text/plain")
            jpeg, meta = feeds[channel].snapshot()
            if jpeg is None:
                return self.respond(503, b"No recent frame\n", "text/plain")
            headers = {
                "X-Frame-Channel": meta["channel"], "X-Frame-Session": meta["session"],
                "X-Frame-Id": meta["frame_id"], "X-Frame-Width": meta["width"],
                "X-Frame-Height": meta["height"], "X-Frame-Age-Ms": f'{meta["age_ms"]:.3f}',
                "X-Frame-Received-Monotonic-Ns": meta["received_monotonic_ns"],
                "X-Frame-Received-Unix-Ns": meta["received_unix_ns"],
                "X-Frame-Time-Basis": meta["time_basis"],
            }
            return self.respond(200, jpeg, "image/jpeg", headers)

        def reject_write(self):
            self.respond(405, b"Only GET is supported\n", "text/plain", {"Allow": "GET"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = reject_write

        def log_message(self, *args):
            pass

    return Handler


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rgb", help="URL RTSP o archivo local")
    source.add_argument("--demo", action="store_true", help="RGB y térmica ficticios, sin cámara")
    parser.add_argument("--thermal", help="Segundo RTSP o archivo local, opcional")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089, help="8089 evita ocupar el 8088 del servicio del dron")
    parser.add_argument("--loop", action="store_true", help="Repetir archivos al llegar a EOF")
    parser.add_argument("--fps", type=float, help="FPS de reproducción local; por defecto los del archivo")
    parser.add_argument("--max-age", type=float, default=3.0, help="Caducidad del frame en segundos")
    parser.add_argument("--quality", type=int, default=82, help="Calidad JPEG 1–100")
    parser.add_argument("--duration", type=float, help="Cerrar tras N segundos; sin opción, Ctrl+C")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.quality <= 100:
        parser.error("Puerto o calidad fuera de rango")
    if not math.isfinite(args.max_age) or args.max_age <= 0:
        parser.error("--max-age debe ser positivo y finito")
    if args.fps is not None and (not math.isfinite(args.fps) or args.fps <= 0):
        parser.error("--fps debe ser positivo y finito")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration debe ser positivo y finito")
    if args.demo and args.thermal:
        parser.error("--demo ya genera ambos canales; no combinar con --thermal")
    for source in (args.rgb, args.thermal):
        if source and not source.lower().startswith(("rtsp://", "rtsps://")) and not Path(source).is_file():
            parser.error("Cada fuente debe ser RTSP o un archivo local existente")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        import cv2  # Comprobar dependencia antes de publicar un servidor vacío.
    except ImportError:
        raise SystemExit("Falta OpenCV en este Python; consultar requirements.txt y README.md")
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cv2.setNumThreads(1)
    sources = {"rgb": args.rgb}
    if args.thermal:
        sources["thermal"] = args.thermal
    if args.demo:
        sources = {"rgb": None, "thermal": None}
    feeds = {name: Feed(name, args.max_age) for name in sources}
    stop = threading.Event()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(feeds))
    server.daemon_threads = True
    server.timeout = .2
    workers = []
    for name, source in sources.items():
        target = run_demo if args.demo else run_source
        worker_args = (feeds[name], stop) if args.demo else (feeds[name], source, stop)
        options = dict(fps=args.fps or 8.0, quality=args.quality) if args.demo else dict(
            loop=args.loop, fps=args.fps, quality=args.quality)
        workers.append(threading.Thread(target=target, args=worker_args, kwargs=options, daemon=True))
    for worker in workers:
        worker.start()
    print(f"JPEG solo lectura: http://{args.host}:{args.port}; OpenCV {cv2.__version__}", flush=True)
    deadline = time.monotonic() + args.duration if args.duration else float("inf")
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        for worker in workers:
            worker.join(timeout=6)


if __name__ == "__main__":
    main()
