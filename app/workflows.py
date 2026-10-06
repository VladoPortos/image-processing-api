"""Composable local workflows, independent of HTTP request parsing."""

import math

from PIL import Image, ImageDraw, ImageFont

from .core import color, decode_image, encode_image, output_filename
from .errors import ImageError
from .models import InputReference, ProcessResult
from .operations import OperationError, apply_operations


def compose_transforms(logs):
    total = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for log in logs:
        matrix = log.get("coordinate_transform")
        if matrix is None:
            return None
        total = [
            [sum(total[i][k] * matrix[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]
    return total


def process(store, request, decoded=None, filename=None):
    if decoded is None:
        data, filename = store.resolve(request.input)
        decoded = decode_image(data, filename)
    try:
        image, logs, operation_warnings = apply_operations(
            decoded.image.copy(), request.operations
        )
    except OperationError as error:
        raise ImageError(
            error.code, str(error), field=error.field, details=error.details
        ) from error
    warnings = list(dict.fromkeys(decoded.warnings + operation_warnings))
    data = encode_image(image, request.output, decoded.metadata)
    name = output_filename(filename, request.output.format, "_processed")
    artifact = store.save_encoded(data, name, image, request.output.format, warnings)
    return ProcessResult(
        artifact=artifact,
        operations=logs,
        warnings=warnings,
        coordinate_transform=compose_transforms(logs),
    )


def compare(decoded, formats, quality):
    from .models import OutputOptions

    original = decoded.inspection
    results = {}
    for format in formats:
        data = encode_image(
            decoded.image,
            OutputOptions(format=format, quality=quality),
            decoded.metadata,
        )
        saved_bytes = original["size_bytes"] - len(data)
        percentage = saved_bytes / original["size_bytes"] * 100
        results[format] = {
            "quality": quality,
            "size_bytes": len(data),
            "size_human": f"{len(data) / 1024:.2f} KB",
            "savings": {
                "bytes": saved_bytes,
                "percentage": f"{percentage:.2f}%",
                "percentage_value": percentage,
            },
        }
    return {
        "original": {
            **original,
            "size_human": f'{original["size_bytes"] / 1024:.2f} KB',
        },
        "formats": results,
        "warnings": decoded.warnings,
    }


def contact_sheet(store, request):
    rows = math.ceil(len(request.images) / request.columns)
    sheet = Image.new(
        "RGBA",
        (request.columns * request.cell_width, rows * request.cell_height),
        color(request.background),
    )
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("LiberationSans-Regular.ttf", 14)
    except OSError:
        font = ImageFont.load_default(size=14)
    label_height = min(28, request.cell_height // 3)
    items, warnings = [], []
    for index, item in enumerate(request.images):
        data, filename = store.resolve(item.input)
        decoded = decode_image(data, filename)
        warnings.extend(decoded.warnings)
        reference = item.input
        if reference.image_base64 is not None:
            original = store.save(data, filename, decoded=decoded)
            reference = InputReference(artifact_id=original.id, filename=filename)
        preview = decoded.image.convert("RGBA")
        preview.thumbnail(
            (request.cell_width - 8, request.cell_height - label_height - 8),
            Image.Resampling.LANCZOS,
        )
        left = (index % request.columns) * request.cell_width
        top = (index // request.columns) * request.cell_height
        x = left + (request.cell_width - preview.width) // 2
        y = top + (request.cell_height - label_height - preview.height) // 2
        sheet.alpha_composite(preview, (x, y))
        label = item.label or filename
        caption = f"{index + 1}: {label}"
        while (
            caption
            and draw.textlength(caption + "…", font=font) > request.cell_width - 8
        ):
            caption = caption[:-1]
        if caption != f"{index + 1}: {label}":
            caption += "…"
        draw.text(
            (left + 4, top + request.cell_height - label_height + 3),
            caption,
            fill="#111111",
            font=font,
        )
        items.append(
            {
                "index": index,
                "label": label,
                "input": reference,
                "box": [
                    left,
                    top,
                    left + request.cell_width,
                    top + request.cell_height,
                ],
            }
        )
    warnings = list(dict.fromkeys(warnings))
    data = encode_image(sheet, request.output)
    artifact = store.save_encoded(
        data,
        output_filename("contact-sheet", request.output.format),
        sheet,
        request.output.format,
        warnings,
    )
    return {"artifact": artifact, "items": items, "warnings": warnings}


def prepare_vision(store, request):
    data, filename = store.resolve(request.input)
    decoded = decode_image(data, filename)
    original = decoded.image
    factors = []
    if request.max_edge:
        factors.append(request.max_edge / max(original.size))
    if request.max_pixels:
        factors.append(
            math.sqrt(request.max_pixels / (original.width * original.height))
        )
    scale = min(factors) if factors else 1.0
    if not request.allow_upscale:
        scale = min(1.0, scale)
    width, height = max(1, math.floor(original.width * scale)), max(
        1, math.floor(original.height * scale)
    )
    if request.max_pixels and width * height > request.max_pixels:
        if width >= height:
            width = max(1, request.max_pixels // height)
        else:
            height = max(1, request.max_pixels // width)
    warnings = list(decoded.warnings)
    lossy = request.output.format != "png" and not (
        request.output.format == "webp" and request.output.lossless
    )
    budget_met, chosen_quality = True, request.output.quality
    while True:
        image = (
            original.resize((width, height), Image.Resampling.LANCZOS)
            if original.size != (width, height)
            else original.copy()
        )
        options = request.output.model_copy(update={"quality": request.output.quality})
        encoded = encode_image(image, options, decoded.metadata)
        chosen_quality = options.quality
        if not request.max_bytes or len(encoded) <= request.max_bytes:
            break
        if lossy:
            low, high, best = request.min_quality, request.output.quality - 1, None
            while low <= high:
                mid = (low + high) // 2
                candidate = encode_image(
                    image, options.model_copy(update={"quality": mid}), decoded.metadata
                )
                if len(candidate) <= request.max_bytes:
                    best = (candidate, mid)
                    low = mid + 1
                else:
                    high = mid - 1
            if best:
                encoded, chosen_quality = best
                break
        if width == 1 and height == 1:
            budget_met = False
            warnings.append("byte_budget_unmet")
            break
        ratio = min(0.85, math.sqrt(request.max_bytes / len(encoded)) * 0.95)
        width, height = max(1, math.floor(width * ratio)), max(
            1, math.floor(height * ratio)
        )
    if request.max_bytes and len(encoded) > request.max_bytes:
        budget_met = False
    transform = [
        [original.width / width, 0.0, 0.0],
        [0.0, original.height / height, 0.0],
        [0.0, 0.0, 1.0],
    ]
    logs = [
        {
            "index": 0,
            "op": "prepare_vision",
            "input_size": list(original.size),
            "output_size": [width, height],
            "coordinate_transform": transform,
            "coordinate_convention": "pixel_edges",
        }
    ]
    artifact = store.save_encoded(
        encoded,
        output_filename(filename, request.output.format, "_vision"),
        image,
        request.output.format,
        warnings,
    )
    return {
        "artifact": artifact,
        "operations": logs,
        "warnings": warnings,
        "coordinate_transform": transform,
        "quality": chosen_quality,
        "budget_met": budget_met,
    }
