"""Decode displayed pixels consistently and encode with explicit policies."""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from PIL import ExifTags, Image, ImageColor, ImageOps, UnidentifiedImageError
from PIL.PngImagePlugin import PngInfo

try:
    import pillow_avif  # noqa: F401 -- Pillow < 11.3 needs the plugin.
except ImportError:
    pass

from .errors import ImageError
from .models import OutputOptions

MIME = {
    "png": "image/png",
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "avif": "image/avif",
}
PIL_FORMAT = {
    "png": "PNG",
    "webp": "WEBP",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "avif": "AVIF",
}


@dataclass
class DecodedImage:
    image: Image.Image
    metadata: dict
    inspection: dict
    warnings: list[str]


def decode_base64(value: str) -> bytes:
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or ";base64" not in header:
            raise ImageError(
                "invalid_base64", "Expected a base64 data URI", field="image_base64"
            )
    try:
        result = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ImageError(
            "invalid_base64", "Invalid base64 image data", field="image_base64"
        ) from error
    if not result:
        raise ImageError("invalid_base64", "Image data is empty", field="image_base64")
    return result


def has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def decode_image(data: bytes, filename: str = "image") -> DecodedImage:
    try:
        with Image.open(BytesIO(data)) as source:
            frames = getattr(source, "n_frames", 1)
            original_info = dict(source.info)
            original_exif = source.getexif()
            orientation = original_exif.get(274)
            raw_size = source.size
            input_format = source.format or "unknown"
            original_mode = source.mode
            alpha = has_alpha(source)
            source.seek(0)
            oriented = ImageOps.exif_transpose(source)
            oriented.load()
            exif = oriented.getexif()
            metadata = {
                "info": dict(oriented.info),
                "exif": exif,
                "original_mode": original_mode,
            }
            warnings = ["first_frame_only"] if frames > 1 else []
            if alpha and "A" not in oriented.getbands():
                oriented = oriented.convert("RGBA")
            elif oriented.mode not in ("RGB", "RGBA", "L", "LA"):
                oriented = oriented.convert("RGBA" if alpha else "RGB")
            else:
                oriented = oriented.copy()
            if original_mode == "CMYK" and original_info.get("icc_profile"):
                metadata["info"].pop("icc_profile", None)
                warnings.append("cmyk_profile_dropped_after_rgb_conversion")
            dpi = original_info.get("dpi")
            inspection = {
                "filename": filename,
                "format": input_format.lower(),
                "width": oriented.width,
                "height": oriented.height,
                "raw_width": raw_size[0],
                "raw_height": raw_size[1],
                "mode": original_mode,
                "has_alpha": alpha,
                "orientation": orientation,
                "frames": frames,
                "animated": frames > 1,
                "size_bytes": len(data),
                "has_icc": bool(original_info.get("icc_profile")),
                "has_exif": bool(original_exif),
                "dpi": list(dpi) if isinstance(dpi, (list, tuple)) else None,
                "warnings": warnings,
            }
            return DecodedImage(oriented, metadata, inspection, warnings)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise ImageError(
            "invalid_image", "Input is not a readable image", field="input"
        ) from error
    except Image.DecompressionBombError as error:
        raise ImageError(
            "image_too_large", str(error), field="input", status_code=413
        ) from error


def color(value: str, field="background"):
    try:
        return ImageColor.getcolor(value, "RGBA")
    except (ValueError, TypeError) as error:
        raise ImageError(
            "invalid_color", "Use a Pillow color name or hex color", field=field
        ) from error


def flatten(image: Image.Image, background: str) -> Image.Image:
    rgba = image.convert("RGBA")
    base = Image.new("RGBA", rgba.size, color(background))
    return Image.alpha_composite(base, rgba).convert("RGB")


def encode_image(
    image: Image.Image, options: OutputOptions, metadata: dict | None = None
) -> bytes:
    image = image.copy()
    image.info.clear()  # No implicit metadata leaking into the strip policy.
    if options.format in ("jpg", "jpeg"):
        image = (
            flatten(image, options.background)
            if has_alpha(image)
            else image.convert("RGB")
        )
    elif image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA" if has_alpha(image) else "RGB")
    kwargs = {}
    if options.metadata == "preserve" and metadata:
        info = metadata.get("info", {})
        for key in ("icc_profile", "dpi"):
            if info.get(key) is not None:
                kwargs[key] = info[key]
        exif = metadata.get("exif")
        if exif:
            exif = Image.Exif()
            exif.load(metadata["exif"].tobytes())
            for tag, value in (
                (256, image.width),
                (257, image.height),
                (40962, image.width),
                (40963, image.height),
            ):
                if tag in exif:
                    exif[tag] = value
            if 34665 in exif:
                nested = dict(exif.get_ifd(34665))
                for tag, value in ((40962, image.width), (40963, image.height)):
                    if tag in nested:
                        nested[tag] = value
                exif[34665] = nested
            if options.format in ("jpg", "jpeg", "png", "webp", "avif"):
                kwargs["exif"] = exif.tobytes()
        if options.format == "png":
            text = PngInfo()
            for key, value in info.items():
                if isinstance(value, str):
                    text.add_itxt(key, value)
            kwargs["pnginfo"] = text
    if options.format in ("jpg", "jpeg"):
        kwargs.update(
            quality=options.quality, optimize=True, progressive=options.progressive
        )
    elif options.format == "png":
        kwargs.update(compress_level=options.compression_level)
    elif options.format == "webp":
        kwargs.update(
            quality=options.quality, lossless=options.lossless, method=options.method
        )
    elif options.format == "avif":
        kwargs.update(quality=options.quality, speed=options.speed)
    stream = BytesIO()
    try:
        image.save(stream, format=PIL_FORMAT[options.format], **kwargs)
    except KeyError as error:
        raise ImageError(
            "codec_unavailable",
            f"{options.format} encoder is unavailable",
            status_code=503,
        ) from error
    return stream.getvalue()


def json_value(value):
    if isinstance(value, bytes):
        return {"type": "binary", "size_bytes": len(value)}
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return float(value)
    except (ValueError, TypeError):
        return str(value)


def metadata_result(decoded: DecodedImage, filename):
    exif = decoded.metadata["exif"]
    fields = {str(ExifTags.TAGS.get(k, k)): json_value(v) for k, v in exif.items()}
    if 34665 in exif:
        fields.update(
            {
                str(ExifTags.TAGS.get(k, k)): json_value(v)
                for k, v in exif.get_ifd(34665).items()
            }
        )
    if 34853 in exif:
        fields["GPSInfo"] = {
            str(ExifTags.GPSTAGS.get(k, k)): json_value(v)
            for k, v in exif.get_ifd(34853).items()
        }
    result = {
        "filename": filename,
        "format": decoded.inspection["format"].upper(),
        "mode": decoded.inspection["mode"],
        "size": {"width": decoded.image.width, "height": decoded.image.height},
        "info": {
            k: json_value(v) for k, v in decoded.metadata["info"].items() if k != "exif"
        },
        "exif": fields,
        "frames": decoded.inspection["frames"],
        "warnings": decoded.warnings,
    }
    return result


def output_filename(filename, format, suffix=""):
    return f'{Path(filename or "image").stem}{suffix}.{format}'


def content_disposition(filename):
    filename = Path(filename.replace("\\", "/")).name
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    )
    ascii_name = re.sub(r"[^a-zA-Z0-9._ -]", "_", ascii_name) or "image"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
