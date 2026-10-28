"""Pruebas del recorrido local. No usan cámara ni servicios externos."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from fuente_jpeg import Feed, make_handler, run_demo, run_source
from recibir_video import fetch, make_handler as receiver_handler


@contextmanager
def serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000
        self.feed = Feed("rgb", max_age_s=1, clock_ns=lambda: self.now)
        ok, jpeg = cv2.imencode(".jpg", np.full((48, 64, 3), 128, dtype=np.uint8))
        self.assertTrue(ok)
        self.jpeg = jpeg.tobytes()

    def test_source_receiver_exact_bytes_and_headers(self):
        self.feed.publish(self.jpeg, 64, 48)
        with serving(make_handler({"rgb": self.feed})) as origin:
            with serving(receiver_handler(origin)) as relay:
                data, headers, _ = fetch(relay, "/rgb.jpg")
                self.assertEqual(data, self.jpeg)
                self.assertEqual(headers["X-Frame-Id"], "1")
                self.assertEqual(headers["X-Frame-Session"], self.feed.session)
                self.assertEqual(cv2.imdecode(np.frombuffer(data, np.uint8), 1).shape, (48, 64, 3))
                with urlopen(relay + "/") as r:
                    self.assertIn(b"fetch('/rgb.jpg'", r.read())

    def test_absent_stale_and_recovery(self):
        with serving(make_handler({"rgb": self.feed})) as origin:
            with serving(receiver_handler(origin)) as relay:
                for stale in (False, True):
                    if stale:
                        self.feed.publish(self.jpeg, 64, 48)
                        self.now += 2_000_000_000
                    with self.assertRaises(HTTPError) as e:
                        fetch(relay, "/rgb.jpg")
                    self.assertEqual(e.exception.code, 503)
                    e.exception.close()
                self.feed.publish(self.jpeg, 64, 48)
                self.assertEqual(fetch(relay, "/rgb.jpg")[0], self.jpeg)
                status = json.loads(fetch(relay, "/status")[0])
                self.assertTrue(status["rgb"]["fresh"])

    def test_unknown_channel_and_writes(self):
        with serving(make_handler({"rgb": self.feed})) as origin:
            with serving(receiver_handler(origin)) as relay:
                for url, method, expected in [(origin + "/thermal.jpg", "GET", 404),
                                               (relay + "/rgb.jpg", "POST", 405)]:
                    with self.assertRaises(HTTPError) as e:
                        urlopen(Request(url, method=method), timeout=2)
                    self.assertEqual(e.exception.code, expected)
                    e.exception.close()

    def test_legacy_server_without_new_headers(self):
        jpeg = self.jpeg
        class Legacy(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            def log_message(self, *args):
                pass
        with serving(Legacy) as origin:
            data, headers, _ = fetch(origin, "/rgb.jpg")
            self.assertEqual(data, jpeg)
            self.assertEqual(headers, {})

    def test_demo_both_channels_real_jpegs(self):
        for channel, shape in [("rgb", (720, 1280, 3)), ("thermal", (120, 160, 3))]:
            feed, stop = Feed(channel), threading.Event()
            thread = threading.Thread(target=run_demo, args=(feed, stop))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while feed.sequence < 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                data, meta = feed.snapshot()
                self.assertGreaterEqual(meta["frame_id"], 2)
                self.assertEqual(cv2.imdecode(np.frombuffer(data, np.uint8), 1).shape, shape)
            finally:
                stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_video_file_ffmpeg_and_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "clip.avi")
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 8, (64, 48))
            self.assertTrue(writer.isOpened(), "Falta backend MJPEG")
            for i in range(4):
                writer.write(np.full((48, 64, 3), 40 + i * 40, dtype=np.uint8))
            writer.release()
            feed = Feed("rgb")
            run_source(feed, path, threading.Event())
            self.assertEqual(feed.sequence, 4)
            self.assertEqual(feed.error, "eof")
            self.assertIsNotNone(feed.snapshot()[0])


if __name__ == "__main__":
    cv2.setNumThreads(1)
    unittest.main()
