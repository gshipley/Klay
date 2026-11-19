"""Bound untrusted artwork before handing it to image renderers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
from time import monotonic
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

try:
    from PIL import Image
except ModuleNotFoundError:
    Image = None


MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 30
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_ANIMATION_FRAMES = 240
MAX_ANIMATION_PIXELS = 180_000_000
RASTER_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "TIFF", "BMP", "ICO")
FORMAT_SUFFIXES = {
    "PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp",
    "TIFF": ".tiff", "BMP": ".bmp", "ICO": ".ico",
}


class ArtworkError(OSError):
    """An image is unsupported, invalid, or exceeds artwork limits."""


def download_artwork(client, url: str, *, timeout: int, directory: Path | None = None,
                     headers: dict[str, str] | None = None) -> tuple[Path, str]:
    """Bound decoded bytes and check elapsed time between chunks; clean failures.

    The elapsed-time check supplements the socket timeout, but is not a strict
    wall-clock deadline while an individual network read is in progress.
    """
    temporary = None
    response = None
    started = monotonic()

    def discard_redirect_body(response, **_kwargs):
        if response.status_code in {301, 302, 303, 307, 308}:
            response.close()
            # Even allow_redirects=False computes Response.next by consuming the
            # body. The hook runs first: discard it before Requests can buffer it.
            response._content = b""
            response._content_consumed = True
        return response

    try:
        for redirect in range(6):
            if urlsplit(url).scheme.lower() not in {"http", "https"}:
                raise ArtworkError("Artwork must use an HTTP or HTTPS URL.")
            # Requests consumes redirect bodies itself, bypassing iter_content's
            # byte limit. Follow redirects without reading those bodies.
            response = client.get(
                url, timeout=timeout, headers=headers, stream=True, allow_redirects=False,
                hooks={"response": discard_redirect_body},
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location")
            if not location or redirect == 5:
                raise ArtworkError("Artwork has invalid or excessive redirects.")
            url = urljoin(url, location)
            response.close()
            response = None
        response.raise_for_status()
        try:
            advertised_size = int(response.headers.get("Content-Length", "0"))
        except (ValueError, TypeError):
            advertised_size = 0
        if advertised_size > MAX_DOWNLOAD_BYTES:
            raise ArtworkError("Artwork exceeds the 32 MiB download limit.")
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".img", delete=False) as output:
            temporary = Path(output.name)
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if monotonic() - started > MAX_DOWNLOAD_SECONDS:
                    raise ArtworkError("Artwork download took too long.")
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ArtworkError("Artwork exceeds the 32 MiB download limit.")
                output.write(chunk)
            if not size:
                raise ArtworkError("The artwork download was empty.")
        return temporary, str(response.headers.get("Content-Type") or "")
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()


def check_dimensions(image) -> None:
    width, height = image.size
    if (width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS):
        raise ArtworkError("Artwork exceeds the 8192-pixel dimension or 16-megapixel limit.")


@contextmanager
def open_artwork(path: Path):
    if Image is None:
        raise ArtworkError("Pillow is required for artwork processing.")
    if path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ArtworkError("Artwork exceeds the 32 MiB file limit.")
    # Sniff only these formats: a misleading filename cannot enable PSD/FITS/etc.
    with Image.open(path, formats=RASTER_FORMATS) as image:
        check_dimensions(image)
        yield image


def artwork_frames(image):
    """Check each frame before decoding; never ask n_frames to scan the whole file."""
    pixels = 0
    for index in range(MAX_ANIMATION_FRAMES + 1):
        try:
            image.seek(index)
        except EOFError:
            return
        if index == MAX_ANIMATION_FRAMES:
            raise ArtworkError("Animated artwork exceeds the 240-frame limit.")
        check_dimensions(image)
        pixels += image.width * image.height
        if pixels > MAX_ANIMATION_PIXELS:
            raise ArtworkError("Animated artwork exceeds the total decoded-pixel limit.")
        yield image


def validate_artwork(path: Path) -> str:
    with open_artwork(path) as image:
        image_format = image.format
        for frame in artwork_frames(image):
            frame.load()
        return str(image_format)


def svg_icon_png(path: Path) -> bytes:
    """Render bounded, self-contained launcher SVGs, never arbitrary fallback input."""
    if path.suffix.lower() != ".svg" or path.stat().st_size > 2 * 1024 * 1024:
        raise ArtworkError("Unsupported launcher icon.")
    with path.open("rb") as source:
        payload = source.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ArtworkError("SVG launcher icon is too large.")
    text = payload.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ArtworkError("SVG declarations are not supported.")
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    depth = count = 0
    root = None
    for offset in range(0, len(text), 4096):
        parser.feed(text[offset:offset + 4096])
        for event, element in parser.read_events():
            if event == "end":
                depth -= 1
                continue
            root = element if root is None else root
            depth += 1
            count += 1
            if depth > 64 or count > 10_000:
                raise ArtworkError("SVG launcher icon is too complex.")
            if element.tag in {"{http://www.w3.org/2000/svg}script", "{http://www.w3.org/2000/svg}foreignObject"}:
                raise ArtworkError("Active SVG content is not supported.")
    parser.close()
    if root is None or root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ArtworkError("Unsupported launcher icon.")
    # Editors commonly include RDF metadata and Inkscape/Sodipodi named views.
    # Remove these inert subtrees instead of rejecting otherwise ordinary icons.
    for parent in root.iter():
        for child in list(parent):
            if (not child.tag.startswith("{http://www.w3.org/2000/svg}")
                    or child.tag == "{http://www.w3.org/2000/svg}metadata"):
                parent.remove(child)
    for element in root.iter():
        for name in list(element.attrib):
            if name.startswith("{") and not name.startswith((
                "{http://www.w3.org/2000/svg}", "{http://www.w3.org/1999/xlink}",
                "{http://www.w3.org/XML/1998/namespace}",
            )):
                del element.attrib[name]
        for name, value in element.attrib.items():
            if name == "{http://www.w3.org/XML/1998/namespace}base":
                raise ArtworkError("External SVG bases are not supported.")
            if name.rsplit("}", 1)[-1] in {"href", "src"} and not value.startswith("#"):
                raise ArtworkError("External SVG resources are not supported.")
        values = [*element.attrib.values(), element.text or ""]
        for value in values:
            if "\\" in value or "@import" in value.lower():
                raise ArtworkError("External SVG styles are not supported.")
            for reference in re.findall(r"url\s*\(([^)]*)\)", value, re.IGNORECASE):
                if not reference.strip().strip("\"'").startswith("#"):
                    raise ArtworkError("External SVG resources are not supported.")

    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")

    def prepare_size(loader, width, height):
        scale = 256 / max(width, height, 1)
        loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))

    loader.connect("size-prepared", prepare_size)
    # GdkPixbuf's format detection expects an unprefixed <svg> root.
    ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
    try:
        loader.write(ElementTree.tostring(root, encoding="utf-8"))
    finally:
        loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ArtworkError("Could not render launcher icon.")
    success, png = pixbuf.save_to_bufferv("png", [], [])
    if not success:
        raise ArtworkError("Could not render launcher icon.")
    return bytes(png)
