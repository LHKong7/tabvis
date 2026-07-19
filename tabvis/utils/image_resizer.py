"""Image format detection, dimension checks, and resize helpers.

Image resize/downsample + compression for the API's image size/dimension limits.

The actual pixel processing depends on a native ``sharp``-style image processor, which is not
implemented in this build. :func:`_get_image_processor` always raises; the resize/compress entry
points then fall through to the pure fallback branches (magic-byte format detection + base64-size
gate), which are fully implemented here.

Pure, processor-free helpers (fully implemented): :func:`detect_image_format_from_buffer`,
:func:`detect_image_format_from_base64`, :func:`create_image_metadata_text`,
:func:`_classify_image_error`, :func:`_hash_string`, :class:`ImageResizeError`.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from tabvis.constants.api_limits import (
    API_IMAGE_MAX_BASE64_SIZE,
    IMAGE_MAX_HEIGHT,
    IMAGE_MAX_WIDTH,
    IMAGE_TARGET_RAW_SIZE,
)
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import get_error_message
from tabvis.utils.format import format_file_size
from tabvis.utils.log import log_error

# Error type constants for analytics (numeric to comply with logEvent restrictions).
_ERROR_TYPE_MODULE_LOAD = 1
_ERROR_TYPE_PROCESSING = 2
_ERROR_TYPE_UNKNOWN = 3
_ERROR_TYPE_PIXEL_LIMIT = 4
_ERROR_TYPE_MEMORY = 5
_ERROR_TYPE_TIMEOUT = 6
_ERROR_TYPE_VIPS = 7
_ERROR_TYPE_PERMISSION = 8

__all__ = [
    "ImageResizeError",
    "compress_image_block",
    "compress_image_buffer",
    "compress_image_buffer_with_token_limit",
    "create_image_metadata_text",
    "detect_image_format_from_base64",
    "detect_image_format_from_buffer",
    "maybe_resize_and_downsample_image_block",
    "maybe_resize_and_downsample_image_buffer",
]


class ImageResizeError(Exception):
    """Raised when image resizing fails and the image exceeds the API limit."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.name = "ImageResizeError"


def _get_image_processor() -> Any:
    """Accessor for the native image processor (``sharp``).

    The native ``sharp``/vips module is not available in this build, so this always raises. The
    resize/compress callers then hit their ``except`` fallback paths: classify as MODULE_LOAD and
    decide via the base64-size gate.
    """
    raise ModuleNotFoundError("Native image processor module not available")


def _classify_image_error(error: Any) -> int:
    """Classify image-processing errors for analytics (codes first, then message matching)."""
    code = getattr(error, "code", None) or getattr(error, "errno", None)
    if code in ("MODULE_NOT_FOUND", "ERR_MODULE_NOT_FOUND", "ERR_DLOPEN_FAILED"):
        return _ERROR_TYPE_MODULE_LOAD
    if code in ("EACCES", "EPERM"):
        return _ERROR_TYPE_PERMISSION
    if code == "ENOMEM":
        return _ERROR_TYPE_MEMORY
    if isinstance(error, ModuleNotFoundError):
        return _ERROR_TYPE_MODULE_LOAD

    message = get_error_message(error)

    if "Native image processor module not available" in message:
        return _ERROR_TYPE_MODULE_LOAD
    if any(
        s in message
        for s in (
            "unsupported image format",
            "Input buffer",
            "Input file is missing",
            "Input file has corrupt header",
            "corrupt header",
            "corrupt image",
            "premature end",
            "zlib: data error",
            "zero width",
            "zero height",
        )
    ):
        return _ERROR_TYPE_PROCESSING
    if any(
        s in message
        for s in ("pixel limit", "too many pixels", "exceeds pixel", "image dimensions")
    ):
        return _ERROR_TYPE_PIXEL_LIMIT
    if any(s in message for s in ("out of memory", "Cannot allocate", "memory allocation")):
        return _ERROR_TYPE_MEMORY
    if "timeout" in message or "timed out" in message:
        return _ERROR_TYPE_TIMEOUT
    if "Vips" in message:
        return _ERROR_TYPE_VIPS
    return _ERROR_TYPE_UNKNOWN


def _hash_string(s: str) -> int:
    """djb2 hash, returning a 32-bit unsigned integer (for analytics grouping)."""
    h = 5381
    for ch in s:
        # ``((hash << 5) + hash + charCode) | 0`` — signed 32-bit truncation per char.
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return h & 0xFFFFFFFF


async def maybe_resize_and_downsample_image_buffer(
    image_buffer: bytes,
    original_size: int,
    ext: str,
) -> dict[str, Any]:
    """Resize image buffer to meet size and dimension constraints.

    Returns ``{"buffer": bytes, "mediaType": str, "dimensions"?: dict}``.
    """
    if len(image_buffer) == 0:
        raise ImageResizeError("Image file is empty (0 bytes)")
    try:
        sharp = _get_image_processor()
        image = sharp(image_buffer)
        metadata = await image.metadata()

        media_type = metadata.get("format") or ext
        normalized_media_type = "jpeg" if media_type == "jpg" else media_type

        width = metadata.get("width")
        height = metadata.get("height")
        if not width or not height:
            if original_size > IMAGE_TARGET_RAW_SIZE:
                compressed_buffer = await sharp(image_buffer).jpeg(quality=80).to_buffer()
                return {"buffer": compressed_buffer, "mediaType": "jpeg"}
            return {"buffer": image_buffer, "mediaType": normalized_media_type}

        original_width = width
        original_height = height

        if (
            original_size <= IMAGE_TARGET_RAW_SIZE
            and width <= IMAGE_MAX_WIDTH
            and height <= IMAGE_MAX_HEIGHT
        ):
            return {
                "buffer": image_buffer,
                "mediaType": normalized_media_type,
                "dimensions": {
                    "originalWidth": original_width,
                    "originalHeight": original_height,
                    "displayWidth": width,
                    "displayHeight": height,
                },
            }

        needs_dimension_resize = width > IMAGE_MAX_WIDTH or height > IMAGE_MAX_HEIGHT
        is_png = normalized_media_type == "png"

        if not needs_dimension_resize and original_size > IMAGE_TARGET_RAW_SIZE:
            if is_png:
                png_compressed = (
                    await sharp(image_buffer).png(compressionLevel=9, palette=True).to_buffer()
                )
                if len(png_compressed) <= IMAGE_TARGET_RAW_SIZE:
                    return _result(
                        png_compressed, "png", original_width, original_height, width, height
                    )
            for quality in (80, 60, 40, 20):
                compressed_buffer = await sharp(image_buffer).jpeg(quality=quality).to_buffer()
                if len(compressed_buffer) <= IMAGE_TARGET_RAW_SIZE:
                    return _result(
                        compressed_buffer, "jpeg", original_width, original_height, width, height
                    )

        if width > IMAGE_MAX_WIDTH:
            height = round((height * IMAGE_MAX_WIDTH) / width)
            width = IMAGE_MAX_WIDTH
        if height > IMAGE_MAX_HEIGHT:
            width = round((width * IMAGE_MAX_HEIGHT) / height)
            height = IMAGE_MAX_HEIGHT

        log_for_debugging(f"Resizing to {width}x{height}")
        resized_image_buffer = (
            await sharp(image_buffer)
            .resize(width, height, fit="inside", withoutEnlargement=True)
            .to_buffer()
        )

        if len(resized_image_buffer) > IMAGE_TARGET_RAW_SIZE:
            if is_png:
                png_compressed = (
                    await sharp(image_buffer)
                    .resize(width, height, fit="inside", withoutEnlargement=True)
                    .png(compressionLevel=9, palette=True)
                    .to_buffer()
                )
                if len(png_compressed) <= IMAGE_TARGET_RAW_SIZE:
                    return _result(
                        png_compressed, "png", original_width, original_height, width, height
                    )
            for quality in (80, 60, 40, 20):
                compressed_buffer = (
                    await sharp(image_buffer)
                    .resize(width, height, fit="inside", withoutEnlargement=True)
                    .jpeg(quality=quality)
                    .to_buffer()
                )
                if len(compressed_buffer) <= IMAGE_TARGET_RAW_SIZE:
                    return _result(
                        compressed_buffer, "jpeg", original_width, original_height, width, height
                    )

            smaller_width = min(width, 1000)
            smaller_height = round((height * smaller_width) / max(width, 1))
            log_for_debugging("Still too large, compressing with JPEG")
            compressed_buffer = (
                await sharp(image_buffer)
                .resize(smaller_width, smaller_height, fit="inside", withoutEnlargement=True)
                .jpeg(quality=20)
                .to_buffer()
            )
            log_for_debugging(f"JPEG compressed buffer size: {len(compressed_buffer)}")
            return _result(
                compressed_buffer,
                "jpeg",
                original_width,
                original_height,
                smaller_width,
                smaller_height,
            )

        return _result(
            resized_image_buffer, normalized_media_type, original_width, original_height, width, height
        )
    except ImageResizeError:
        raise
    except Exception as error:  # noqa: BLE001 — faithful to the TS catch-all fallback
        log_error(error)
        error_type = _classify_image_error(error)
        error_msg = get_error_message(error)

        # Detect actual format from magic bytes instead of trusting the extension.
        detected = detect_image_format_from_buffer(image_buffer)
        normalized_ext = detected[6:]  # strip 'image/'

        base64_size = math.ceil((original_size * 4) / 3)

        over_dim = (
            len(image_buffer) >= 24
            and image_buffer[0] == 0x89
            and image_buffer[1] == 0x50
            and image_buffer[2] == 0x4E
            and image_buffer[3] == 0x47
            and (
                struct.unpack(">I", image_buffer[16:20])[0] > IMAGE_MAX_WIDTH
                or struct.unpack(">I", image_buffer[20:24])[0] > IMAGE_MAX_HEIGHT
            )
        )

        if base64_size <= API_IMAGE_MAX_BASE64_SIZE and not over_dim:
            return {"buffer": image_buffer, "mediaType": normalized_ext}

        raise ImageResizeError(
            (
                f"Unable to resize image — dimensions exceed the {IMAGE_MAX_WIDTH}x{IMAGE_MAX_HEIGHT}px"
                " limit and image processing failed. Please resize the image to reduce its pixel "
                "dimensions."
            )
            if over_dim
            else (
                f"Unable to resize image ({format_file_size(original_size)} raw, "
                f"{format_file_size(base64_size)} base64). The image exceeds the 5MB API limit and "
                "compression failed. Please resize the image manually or use a smaller image."
            )
        ) from error


def _result(
    buffer: bytes,
    media_type: str,
    original_width: int,
    original_height: int,
    display_width: int,
    display_height: int,
) -> dict[str, Any]:
    return {
        "buffer": buffer,
        "mediaType": media_type,
        "dimensions": {
            "originalWidth": original_width,
            "originalHeight": original_height,
            "displayWidth": display_width,
            "displayHeight": display_height,
        },
    }


async def maybe_resize_and_downsample_image_block(image_block: dict[str, Any]) -> dict[str, Any]:
    """Resize an image content block if needed; also returns dimension info.

    Returns ``{"block": ImageBlockParam, "dimensions"?: dict}``.
    """
    import base64

    source = image_block["source"]
    if source.get("type") != "base64":
        return {"block": image_block}

    image_buffer = base64.b64decode(source["data"])
    original_size = len(image_buffer)

    media_type = source.get("media_type")
    ext = (media_type.split("/")[1] if media_type else None) or "png"

    resized = await maybe_resize_and_downsample_image_buffer(image_buffer, original_size, ext)

    return {
        "block": {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{resized['mediaType']}",
                "data": base64.b64encode(resized["buffer"]).decode("ascii"),
            },
        },
        "dimensions": resized.get("dimensions"),
    }


async def compress_image_buffer(
    image_buffer: bytes,
    max_bytes: int = IMAGE_TARGET_RAW_SIZE,
    original_media_type: str | None = None,
) -> dict[str, Any]:
    """Compress an image buffer to fit within ``max_bytes`` via a multi-strategy fallback.

    Returns ``{"base64": str, "mediaType": str, "originalSize": int}``.
    """
    import base64

    fallback_format = (original_media_type.split("/")[1] if original_media_type else None) or "jpeg"
    normalized_fallback = "jpeg" if fallback_format == "jpg" else fallback_format

    try:
        sharp = _get_image_processor()
        metadata = await sharp(image_buffer).metadata()
        fmt = metadata.get("format") or normalized_fallback
        original_size = len(image_buffer)

        context = {
            "imageBuffer": image_buffer,
            "metadata": metadata,
            "format": fmt,
            "maxBytes": max_bytes,
            "originalSize": original_size,
        }

        if original_size <= max_bytes:
            return _create_compressed_image_result(image_buffer, fmt, original_size)

        resized_result = await _try_progressive_resizing(context, sharp)
        if resized_result:
            return resized_result
        if fmt == "png":
            palettized_result = await _try_palette_png(context, sharp)
            if palettized_result:
                return palettized_result
        jpeg_result = await _try_jpeg_conversion(context, 50, sharp)
        if jpeg_result:
            return jpeg_result
        return await _create_ultra_compressed_jpeg(context, sharp)
    except Exception as error:  # noqa: BLE001 — faithful fallback
        log_error(error)
        error_type = _classify_image_error(error)
        error_msg = get_error_message(error)

        if len(image_buffer) <= max_bytes:
            detected = detect_image_format_from_buffer(image_buffer)
            return {
                "base64": base64.b64encode(image_buffer).decode("ascii"),
                "mediaType": detected,
                "originalSize": len(image_buffer),
            }

        raise ImageResizeError(
            f"Unable to compress image ({format_file_size(len(image_buffer))}) to fit within "
            f"{format_file_size(max_bytes)}. Please use a smaller image."
        ) from error


async def compress_image_buffer_with_token_limit(
    image_buffer: bytes,
    max_tokens: int,
    original_media_type: str | None = None,
) -> dict[str, Any]:
    """Compress an image buffer to fit within a token limit (tokens → bytes via 0.125 / 0.75)."""
    max_base64_chars = math.floor(max_tokens / 0.125)
    max_bytes = math.floor(max_base64_chars * 0.75)
    return await compress_image_buffer(image_buffer, max_bytes, original_media_type)


async def compress_image_block(
    image_block: dict[str, Any],
    max_bytes: int = IMAGE_TARGET_RAW_SIZE,
) -> dict[str, Any]:
    """Compress an image block to fit within ``max_bytes`` (wrapper around compress_image_buffer)."""
    import base64

    source = image_block["source"]
    if source.get("type") != "base64":
        return image_block

    image_buffer = base64.b64decode(source["data"])
    if len(image_buffer) <= max_bytes:
        return image_block

    compressed = await compress_image_buffer(image_buffer, max_bytes)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": compressed["mediaType"],
            "data": compressed["base64"],
        },
    }


def _create_compressed_image_result(
    buffer: bytes, media_type: str, original_size: int
) -> dict[str, Any]:
    import base64

    normalized_media_type = "jpeg" if media_type == "jpg" else media_type
    return {
        "base64": base64.b64encode(buffer).decode("ascii"),
        "mediaType": f"image/{normalized_media_type}",
        "originalSize": original_size,
    }


async def _try_progressive_resizing(context: dict[str, Any], sharp: Any) -> dict[str, Any] | None:
    for scaling_factor in (1.0, 0.75, 0.5, 0.25):
        new_width = round((context["metadata"].get("width") or 2000) * scaling_factor)
        new_height = round((context["metadata"].get("height") or 2000) * scaling_factor)

        resized_image = sharp(context["imageBuffer"]).resize(
            new_width, new_height, fit="inside", withoutEnlargement=True
        )
        resized_image = _apply_format_optimizations(resized_image, context["format"])
        resized_buffer = await resized_image.to_buffer()

        if len(resized_buffer) <= context["maxBytes"]:
            return _create_compressed_image_result(
                resized_buffer, context["format"], context["originalSize"]
            )
    return None


def _apply_format_optimizations(image: Any, fmt: str) -> Any:
    if fmt == "png":
        return image.png(compressionLevel=9, palette=True)
    if fmt in ("jpeg", "jpg"):
        return image.jpeg(quality=80)
    if fmt == "webp":
        return image.webp(quality=80)
    return image


async def _try_palette_png(context: dict[str, Any], sharp: Any) -> dict[str, Any] | None:
    palette_png = (
        await sharp(context["imageBuffer"])
        .resize(800, 800, fit="inside", withoutEnlargement=True)
        .png(compressionLevel=9, palette=True, colors=64)
        .to_buffer()
    )
    if len(palette_png) <= context["maxBytes"]:
        return _create_compressed_image_result(palette_png, "png", context["originalSize"])
    return None


async def _try_jpeg_conversion(
    context: dict[str, Any], quality: int, sharp: Any
) -> dict[str, Any] | None:
    jpeg_buffer = (
        await sharp(context["imageBuffer"])
        .resize(600, 600, fit="inside", withoutEnlargement=True)
        .jpeg(quality=quality)
        .to_buffer()
    )
    if len(jpeg_buffer) <= context["maxBytes"]:
        return _create_compressed_image_result(jpeg_buffer, "jpeg", context["originalSize"])
    return None


async def _create_ultra_compressed_jpeg(context: dict[str, Any], sharp: Any) -> dict[str, Any]:
    ultra_compressed_buffer = (
        await sharp(context["imageBuffer"])
        .resize(400, 400, fit="inside", withoutEnlargement=True)
        .jpeg(quality=20)
        .to_buffer()
    )
    return _create_compressed_image_result(
        ultra_compressed_buffer, "jpeg", context["originalSize"]
    )


def detect_image_format_from_buffer(buffer: bytes) -> str:
    """Detect image format from a buffer using magic bytes. Defaults to 'image/png'."""
    if len(buffer) < 4:
        return "image/png"

    if buffer[0] == 0x89 and buffer[1] == 0x50 and buffer[2] == 0x4E and buffer[3] == 0x47:
        return "image/png"
    if buffer[0] == 0xFF and buffer[1] == 0xD8 and buffer[2] == 0xFF:
        return "image/jpeg"
    if buffer[0] == 0x47 and buffer[1] == 0x49 and buffer[2] == 0x46:
        return "image/gif"
    if buffer[0] == 0x52 and buffer[1] == 0x49 and buffer[2] == 0x46 and buffer[3] == 0x46:
        if (
            len(buffer) >= 12
            and buffer[8] == 0x57
            and buffer[9] == 0x45
            and buffer[10] == 0x42
            and buffer[11] == 0x50
        ):
            return "image/webp"

    return "image/png"


def detect_image_format_from_base64(base64_data: str) -> str:
    """Detect image format from base64 data using magic bytes. Defaults to 'image/png' on error."""
    import base64

    try:
        buffer = base64.b64decode(base64_data)
        return detect_image_format_from_buffer(buffer)
    except Exception:  # noqa: BLE001 — any decode error → default
        return "image/png"


def create_image_metadata_text(dims: dict[str, Any], source_path: str | None = None) -> str | None:
    """Create a text description of image metadata (dimensions + source). ``None`` if not useful."""
    original_width = dims.get("originalWidth")
    original_height = dims.get("originalHeight")
    display_width = dims.get("displayWidth")
    display_height = dims.get("displayHeight")

    if (
        not original_width
        or not original_height
        or not display_width
        or not display_height
        or display_width <= 0
        or display_height <= 0
    ):
        if source_path:
            return f"[Image source: {source_path}]"
        return None

    was_resized = original_width != display_width or original_height != display_height

    if not was_resized and not source_path:
        return None

    parts: list[str] = []
    if source_path:
        parts.append(f"source: {source_path}")
    if was_resized:
        scale_factor = original_width / display_width
        parts.append(
            f"original {original_width}x{original_height}, displayed at "
            f"{display_width}x{display_height}. Multiply coordinates by {scale_factor:.2f} to map "
            "to original image."
        )

    return f"[Image: {', '.join(parts)}]"
