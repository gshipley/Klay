"""Artwork limits apply to imported, manually selected, and cached covers."""
from __future__ import annotations

import hashlib
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from klay.qt import artwork, import_worker
from klay.qt.library import GameEntry, GameLibrary


class ArtworkDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.response = Mock()
        self.response.status_code = 200
        self.response.headers = {}
        self.response.iter_content.return_value = iter([b"1234", b"5678"])
        self.client = Mock()
        self.client.get.return_value = self.response

    def download(self):
        return artwork.download_artwork(
            self.client, "https://example.test/art.png?large=1", timeout=8,
            directory=self.root,
        )

    def test_download_streams_decoded_bytes_and_closes_response(self):
        path, _ = self.download()
        self.assertEqual(path.read_bytes(), b"12345678")
        self.assertTrue(self.client.get.call_args.kwargs["stream"])
        self.response.close.assert_called_once()

    def test_missing_or_false_content_length_cannot_bypass_byte_limit(self):
        for content_length in (None, "1", "not-a-number"):
            with self.subTest(content_length=content_length):
                self.response.headers = {"Content-Length": content_length}
                self.response.iter_content.return_value = iter([b"1234", b"5678"])
                with patch.object(artwork, "MAX_DOWNLOAD_BYTES", 7):
                    with self.assertRaises(artwork.ArtworkError):
                        self.download()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_advertised_oversize_is_rejected_before_reading_body(self):
        self.response.headers = {"Content-Length": str(artwork.MAX_DOWNLOAD_BYTES + 1)}
        with self.assertRaises(artwork.ArtworkError):
            self.download()
        self.response.iter_content.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_network_error_removes_partial_file_and_closes_response(self):
        def chunks():
            yield b"partial"
            raise OSError("connection reset")
        self.response.iter_content.return_value = chunks()
        with self.assertRaises(OSError):
            self.download()
        self.assertEqual(list(self.root.iterdir()), [])
        self.response.close.assert_called_once()

    def test_elapsed_time_is_checked_between_chunks(self):
        with patch.object(artwork, "monotonic", side_effect=[0, 31]):
            with self.assertRaises(artwork.ArtworkError):
                self.download()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_redirect_bodies_are_never_downloaded(self):
        redirect = Mock(status_code=302, headers={"Location": "/cover.png"})
        self.client.get.side_effect = [redirect, self.response]
        path, _ = self.download()
        self.assertEqual(path.read_bytes(), b"12345678")
        self.assertEqual(self.client.get.call_args.args[0], "https://example.test/cover.png")
        for call in self.client.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
        redirect.iter_content.assert_not_called()
        redirect.close.assert_called_once()

    def test_redirects_are_bounded_and_cannot_access_local_files(self):
        self.response.status_code = 302
        for location, calls in (("/again", 6), ("file:///etc/passwd", 1)):
            with self.subTest(location=location):
                self.response.headers = {"Location": location}
                self.client.reset_mock()
                with self.assertRaises(artwork.ArtworkError):
                    self.download()
                self.assertEqual(self.client.get.call_count, calls)
                self.response.iter_content.assert_not_called()

    @unittest.skipIf(import_worker.requests is None, "Requests is not installed")
    def test_real_requests_does_not_consume_a_slow_redirect_body(self):
        release = Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/cover")
                    self.send_header("Content-Length", "1000000")
                    self.end_headers()
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    release.wait(5)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"done")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with import_worker.requests.Session() as client:
                client.trust_env = False
                path, _ = artwork.download_artwork(
                    client, f"http://127.0.0.1:{server.server_port}/redirect", timeout=1,
                    directory=self.root,
                )
                self.assertEqual(path.read_bytes(), b"done")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@unittest.skipIf(artwork.Image is None, "Pillow is not installed")
class ArtworkImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.covers = self.root / "covers"
        self.covers.mkdir()

    def image(self, name="source.png", *, size=(32, 48), format="PNG"):
        path = self.root / name
        artwork.Image.new("RGB", size, "red").save(path, format=format)
        return path

    def animation(self):
        path = self.root / "source.gif"
        frames = [artwork.Image.new("RGB", (32, 48), color) for color in ("red", "blue", "green")]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=[80, 90, 100], loop=0)
        for frame in frames:
            frame.close()
        return path

    def save(self, path):
        return import_worker._save_cover_from_path(
            cover_path=path, covers_dir=self.covers, game_id="game",
        )

    def test_normal_static_and_animated_covers_are_preserved(self):
        self.assertTrue(self.save(self.image()))
        with artwork.Image.open(self.covers / "game.png") as image:
            self.assertEqual(image.size, (200, 300))
        self.assertTrue(self.save(self.animation()))
        self.assertFalse((self.covers / "game.png").exists())
        with artwork.Image.open(self.covers / "game.gif") as image:
            self.assertEqual(image.n_frames, 3)
            self.assertEqual(image.info["duration"], 80)

    def test_disallowed_format_with_png_suffix_does_not_reach_fallback(self):
        path = self.image("disguised.png", format="PPM")
        with patch.object(import_worker, "svg_icon_png") as fallback:
            self.assertFalse(self.save(path))
        fallback.assert_not_called()
        self.assertEqual(list(self.covers.iterdir()), [])

    def test_dimension_and_pixel_limits_preserve_existing_cover(self):
        old = self.covers / "game.png"
        old.write_bytes(b"previous cover")
        path = self.image(size=(32, 48))
        for limit, value in (("MAX_IMAGE_DIMENSION", 40), ("MAX_IMAGE_PIXELS", 1000)):
            with self.subTest(limit=limit), patch.object(artwork, limit, value):
                self.assertFalse(self.save(path))
                self.assertEqual(old.read_bytes(), b"previous cover")
                self.assertEqual(list(self.covers.iterdir()), [old])

    def test_animation_frame_and_total_pixel_limits(self):
        path = self.animation()
        for limit, value in (("MAX_ANIMATION_FRAMES", 2), ("MAX_ANIMATION_PIXELS", 32 * 48 * 2)):
            with self.subTest(limit=limit), patch.object(artwork, limit, value):
                self.assertFalse(self.save(path))
                self.assertEqual(list(self.covers.iterdir()), [])

    def test_manual_cover_rejection_preserves_previous_cover(self):
        library = GameLibrary.__new__(GameLibrary)
        library.covers_dir = self.covers
        game = GameEntry(self.root / "game.json", {"game_id": "game"})
        old = self.covers / "game.png"
        old.write_bytes(b"previous cover")
        with self.assertRaises(OSError):
            library.set_cover(game, self.image("disguised.png", format="PPM"))
        self.assertEqual(old.read_bytes(), b"previous cover")
        self.assertEqual(list(self.covers.iterdir()), [old])

    def test_manual_cover_uses_detected_format(self):
        library = GameLibrary.__new__(GameLibrary)
        library.covers_dir = self.covers
        game = GameEntry(self.root / "game.json", {"game_id": "game"})
        library.set_cover(game, self.image("disguised.gif", format="JPEG"))
        self.assertTrue((self.covers / "game.jpg").is_file())
        self.assertFalse((self.covers / "game.gif").exists())

    def test_bad_cached_artwork_is_not_returned_to_qt(self):
        library = GameLibrary.__new__(GameLibrary)
        library.cover_cache_dir = self.covers
        url = "https://example.test/art.png"
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.covers / f"{key}.png"
        cached.write_bytes(b"not an image")
        with patch("klay.qt.library.download_artwork", side_effect=OSError("offline")):
            self.assertIsNone(library.cached_remote_cover_path(url))
        self.assertFalse(cached.exists())

    def test_remote_cache_validates_actual_bytes_and_cleans_rejected_download(self):
        library = GameLibrary.__new__(GameLibrary)
        library.cover_cache_dir = self.covers
        source = self.image("disguised.png", format="PPM")
        with patch("klay.qt.library.download_artwork", return_value=(source, "image/png")):
            self.assertIsNone(library.cached_remote_cover_path("https://example.test/art.png"))
        self.assertFalse(source.exists())
        self.assertEqual(list(self.covers.iterdir()), [])


class SvgIconTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "icon.svg"

    def test_external_entities_resources_and_styles_are_rejected(self):
        for body in (
            '<image href="file:///etc/passwd"/>',
            '<image href="https://example.test/image.png"/>',
            '<rect style="fill: url(https://example.test/paint.svg)"/>',
            '<style>@import "https://example.test/style.css";</style>',
            '<g xml:base="https://example.test/"><use href="#icon"/></g>',
            '<script>alert(1)</script>',
        ):
            with self.subTest(body=body):
                self.path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>')
                with self.assertRaises(artwork.ArtworkError):
                    artwork.svg_icon_png(self.path)
        self.path.write_text('<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>')
        with self.assertRaises(artwork.ArtworkError):
            artwork.svg_icon_png(self.path)

    def test_deep_svg_is_rejected_before_native_renderer(self):
        self.path.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + '<g>' * 65 + '</g>' * 65 + '</svg>')
        with self.assertRaises(artwork.ArtworkError):
            artwork.svg_icon_png(self.path)

    def test_normal_svg_icon_renders_at_bounded_size(self):
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
        except (ImportError, ValueError):
            self.skipTest("GdkPixbuf is not installed")
        if artwork.Image is None:
            self.skipTest("Pillow is not installed")
        self.path.write_text('''<svg xmlns="http://www.w3.org/2000/svg"
            xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
            xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
            inkscape:version="1.4" width="1000" height="500">
            <metadata><rdf:RDF><rdf:Description>Example</rdf:Description></rdf:RDF></metadata>
            <sodipodi:namedview inkscape:zoom="2"/>
            <rect width="1000" height="500" fill="green"/></svg>''')
        with artwork.Image.open(BytesIO(artwork.svg_icon_png(self.path))) as image:
            self.assertEqual(image.size, (256, 128))


if __name__ == "__main__":
    unittest.main()
