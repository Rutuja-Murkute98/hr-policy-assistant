"""Shrinking profile photos before they're sent. No API is called."""
import base64
import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ui"))

from photos import PHOTO_SIZE, photo_bytes, square_photo  # noqa: E402


def picture(width: int, height: int, fmt: str = "PNG", **options) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 40)).save(output, format=fmt, **options)
    return output.getvalue()


def test_a_large_photo_becomes_a_small_square_jpeg():
    shrunk = square_photo(picture(3000, 2000))
    with Image.open(io.BytesIO(shrunk)) as result:
        assert result.format == "JPEG"
        assert result.size == (PHOTO_SIZE, PHOTO_SIZE)
    assert len(shrunk) < 300_000


def test_where_a_photo_was_taken_is_dropped():
    exif = Image.Exif()
    exif[0x8825] = {2: (18.0, 31.0, 0.0)}  # GPS information
    shrunk = square_photo(picture(400, 400, "JPEG", exif=exif.tobytes()))
    with Image.open(io.BytesIO(shrunk)) as result:
        assert not result.getexif()


def test_something_that_is_not_a_photo_is_refused():
    assert square_photo(b"<svg><script>alert(1)</script></svg>") is None
    assert square_photo(b"") is None


def test_a_stored_photo_is_read_back_from_the_api():
    data = picture(10, 10)
    assert photo_bytes({"photo": "data:image/png;base64," + base64.b64encode(data).decode()}) == data
    assert photo_bytes({"photo": ""}) is None
    assert photo_bytes({"photo": "javascript:alert(1)"}) is None
    assert photo_bytes({"photo": "data:image/png;base64,not base64!"}) is None
