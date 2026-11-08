"""Profile photos: shrink what someone uploads, and read back what the API stores."""
import base64
import io

PHOTO_SIZE = 256  # pixels on each side, enough for a sharp avatar on any screen
MAX_PIXELS = 40_000_000  # a huge picture is refused rather than filling the memory


def square_photo(data: bytes) -> bytes | None:
    """Crop to a square from the middle and shrink, so a large photo from a phone fits the 300 KB limit.

    Saving it again also drops what a camera stores inside a photo, such as where it was taken.
    """
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture = ImageOps.exif_transpose(picture).convert("RGB")
            picture = ImageOps.fit(picture, (PHOTO_SIZE, PHOTO_SIZE))
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=85)
            return output.getvalue()
    except Exception:  # not a picture, one Pillow can't read, or far too big
        return None


def photo_bytes(profile: dict) -> bytes | None:
    """The photo from a profile, which the API sends as a data URL. None if there isn't one."""
    photo = profile.get("photo") or ""
    if not photo.startswith("data:image/"):
        return None
    try:
        return base64.b64decode(photo.split(",", 1)[1], validate=True)
    except (ValueError, IndexError):
        return None
