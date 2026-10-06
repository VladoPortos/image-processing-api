"""Typed, ordered Pillow operations on already EXIF-oriented images.

Every ``coordinate_transform`` maps the operation's output pixel-edge
coordinates back to its input. Right and bottom crop edges are exclusive.
Matrices describe geometry, including coordinates outside the original image
where an operation adds a background; they do not claim those pixels existed
in the input. Padding logs also identify the placed content rectangle.
"""

from __future__ import annotations

import base64
import binascii
import math
from io import BytesIO
from typing import Annotated, Literal

from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


class OperationError(ValueError):
    """Invalid caller input, with machine-readable error information."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_operation",
        field: str | None = None,
        details: dict | list | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = {} if details is None else details


Anchor = Literal[
    "center",
    "top-left",
    "top",
    "top-right",
    "left",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
]
PositivePixel = Annotated[int, Field(gt=0, strict=True)]
NonnegativePixel = Annotated[int, Field(ge=0, strict=True)]


def _rgba(color: str) -> tuple[int, int, int, int]:
    return ImageColor.getcolor(color, "RGBA")


class OperationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, revalidate_instances="always"
    )

    @field_validator("background", "color", check_fields=False)
    @classmethod
    def valid_color(cls, value: str) -> str:
        try:
            _rgba(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Use a valid Pillow color name or hexadecimal RGB/RGBA color"
            ) from exc
        return value


class ResizeOperation(OperationModel):
    op: Literal["resize"] = "resize"
    width: PositivePixel | None = None
    height: PositivePixel | None = None
    percentage: Annotated[float, Field(gt=0)] | None = None
    fit: Literal["contain", "cover", "stretch", "pad"] = "contain"
    anchor: Anchor = "center"
    background: str = "#00000000"
    allow_upscale: bool = False

    @model_validator(mode="after")
    def one_sizing_strategy(self):
        dimensions = self.width is not None or self.height is not None
        if (self.percentage is not None) == dimensions:
            raise ValueError("Provide width/height or percentage, exclusively")
        if (
            self.fit in ("cover", "stretch", "pad")
            and self.percentage is None
            and (self.width is None or self.height is None)
        ):
            raise ValueError(f"The {self.fit} fit requires both width and height")
        return self


class CropOperation(OperationModel):
    op: Literal["crop"] = "crop"
    left: float
    top: float
    right: float
    bottom: float
    units: Literal["pixels", "normalized"] = "pixels"

    @model_validator(mode="after")
    def valid_box(self):
        if (
            self.left < 0
            or self.top < 0
            or self.right <= self.left
            or self.bottom <= self.top
        ):
            raise ValueError(
                "Crop must have nonnegative left/top and increasing right/bottom edges"
            )
        values = (self.left, self.top, self.right, self.bottom)
        if self.units == "normalized" and any(value > 1 for value in values):
            raise ValueError("Normalized crop coordinates must be between 0 and 1")
        if self.units == "pixels" and any(not value.is_integer() for value in values):
            raise ValueError("Pixel crop coordinates must be integer-valued")
        return self


class RotateOperation(OperationModel):
    op: Literal["rotate"] = "rotate"
    angle: float
    expand: bool = True
    background: str = "#00000000"


class FlipOperation(OperationModel):
    op: Literal["flip"] = "flip"
    direction: Literal["horizontal", "vertical"] = "horizontal"


class PadOperation(OperationModel):
    op: Literal["pad"] = "pad"
    border: NonnegativePixel | None = None
    left: NonnegativePixel = 0
    top: NonnegativePixel = 0
    right: NonnegativePixel = 0
    bottom: NonnegativePixel = 0
    background: str = "#00000000"

    @model_validator(mode="after")
    def one_padding_strategy(self):
        if self.border is not None and any(
            (self.left, self.top, self.right, self.bottom)
        ):
            raise ValueError("Provide a border or individual sides, exclusively")
        return self


class TrimOperation(OperationModel):
    op: Literal["trim"] = "trim"
    alpha_threshold: Annotated[int, Field(ge=0, le=254, strict=True)] = 0


class FlattenOperation(OperationModel):
    op: Literal["flatten"] = "flatten"
    background: str = "#ffffff"

    @field_validator("background")
    @classmethod
    def opaque_background(cls, value: str) -> str:
        if _rgba(value)[3] != 255:
            raise ValueError("Flatten requires an opaque background")
        return value


class WatermarkOperation(OperationModel):
    op: Literal["watermark"] = "watermark"
    text: str | None = None
    logo_base64: str | None = None
    color: str = "#ffffff"
    opacity: Annotated[float, Field(ge=0, le=1)] = 0.35
    font_size: PositivePixel = 20
    angle: float = 45
    position: Literal[
        "repeated",
        "corners",
        "center",
        "top-left",
        "top",
        "top-right",
        "left",
        "right",
        "bottom-left",
        "bottom",
        "bottom-right",
    ] = "repeated"
    margin: NonnegativePixel = 10
    spacing: NonnegativePixel = 40
    logo_width: PositivePixel | None = None
    density: Annotated[int, Field(ge=1, le=50, strict=True)] = 15

    @model_validator(mode="after")
    def has_content(self):
        if self.text is None and self.logo_base64 is None:
            raise ValueError("Provide text, logo_base64, or both")
        if self.text is not None and not self.text.strip():
            raise ValueError("Watermark text cannot be empty")
        if self.logo_base64 is not None and not self.logo_base64.strip():
            raise ValueError("Watermark logo cannot be empty")
        return self


Operation = Annotated[
    ResizeOperation
    | CropOperation
    | RotateOperation
    | FlipOperation
    | PadOperation
    | TrimOperation
    | FlattenOperation
    | WatermarkOperation,
    Field(discriminator="op"),
]
_OPERATION_ADAPTER = TypeAdapter(Operation)


def _matrix(sx=1.0, sy=1.0, tx=0.0, ty=0.0) -> list[list[float]]:
    return [[float(sx), 0.0, float(tx)], [0.0, float(sy), float(ty)], [0.0, 0.0, 1.0]]


def _anchor(anchor: str) -> tuple[float, float]:
    return {
        "center": (0.5, 0.5),
        "top-left": (0, 0),
        "top": (0.5, 0),
        "top-right": (1, 0),
        "left": (0, 0.5),
        "right": (1, 0.5),
        "bottom-left": (0, 1),
        "bottom": (0.5, 1),
        "bottom-right": (1, 1),
    }[anchor]


def _resizeable(image: Image.Image) -> Image.Image:
    if image.mode == "P":
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    if image.mode == "LA":
        return image.convert("RGBA")
    return image


def _canvas(
    image: Image.Image, size: tuple[int, int], offset: tuple[int, int], background: str
) -> Image.Image:
    canvas = Image.new("RGBA", size, _rgba(background))
    # A masked paste here would apply source alpha before compositing it again.
    canvas.alpha_composite(image.convert("RGBA"), dest=offset)
    return canvas


def _resize(image: Image.Image, operation: ResizeOperation):
    iw, ih = image.size
    warnings = []
    if operation.percentage is not None:
        scale = operation.percentage / 100
        tw, th = round(iw * scale), round(ih * scale)
        if tw < 1 or th < 1:
            raise OperationError(
                "Percentage rounds to a zero-sized image",
                code="invalid_dimensions",
                field="percentage",
                details={"input_size": [iw, ih], "percentage": operation.percentage},
            )
        fit = "stretch"
    else:
        tw = (
            operation.width
            if operation.width is not None
            else round(iw * operation.height / ih)
        )
        th = (
            operation.height
            if operation.height is not None
            else round(ih * operation.width / iw)
        )
        fit = operation.fit
    if tw < 1 or th < 1:
        raise OperationError(
            "Requested dimensions round to a zero-sized image",
            code="invalid_dimensions",
            details={"input_size": [iw, ih], "requested_size": [tw, th]},
        )

    if fit == "stretch":
        nw = tw if operation.allow_upscale else min(tw, iw)
        nh = th if operation.allow_upscale else min(th, ih)
        prevented = nw != tw or nh != th
    else:
        scale = (max if fit == "cover" else min)(tw / iw, th / ih)
        prevented = not operation.allow_upscale and scale > 1
        if prevented:
            scale = 1
        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    if prevented:
        warnings.append("Resize upscaling was prevented by allow_upscale=false")

    resized = _resizeable(image).resize((nw, nh), Image.Resampling.LANCZOS)
    ax, ay = _anchor(operation.anchor)
    details = {"scale": [nw / iw, nh / ih]}
    if fit == "cover":
        cw, ch = min(tw, nw), min(th, nh)
        left, top = int((nw - cw) * ax), int((nh - ch) * ay)
        resized = resized.crop((left, top, left + cw, top + ch))
        details["crop_box"] = [left, top, left + cw, top + ch]
        transform = _matrix(iw / nw, ih / nh, left * iw / nw, top * ih / nh)
    elif fit == "pad":
        left, top = int((tw - nw) * ax), int((th - nh) * ay)
        resized = _canvas(resized, (tw, th), (left, top), operation.background)
        details["content_box"] = [left, top, left + nw, top + nh]
        transform = _matrix(iw / nw, ih / nh, -left * iw / nw, -top * ih / nh)
    else:
        transform = _matrix(iw / nw, ih / nh)
    details["coordinate_transform"] = transform
    return resized, details, warnings


def _crop(image: Image.Image, operation: CropOperation):
    if operation.units == "normalized":
        # Outward rounding retains the complete normalized region.
        box = (
            math.floor(operation.left * image.width),
            math.floor(operation.top * image.height),
            math.ceil(operation.right * image.width),
            math.ceil(operation.bottom * image.height),
        )
    else:
        box = tuple(
            int(value)
            for value in (
                operation.left,
                operation.top,
                operation.right,
                operation.bottom,
            )
        )
    if box[2] > image.width or box[3] > image.height:
        raise OperationError(
            "Crop is outside the image bounds",
            code="invalid_crop",
            details={"input_size": list(image.size), "crop_box": list(box)},
        )
    return (
        image.crop(box),
        {"crop_box": list(box), "coordinate_transform": _matrix(tx=box[0], ty=box[1])},
        [],
    )


def _rotate(image: Image.Image, operation: RotateOperation):
    angle = operation.angle % 360
    background = _rgba(operation.background)
    quarter_turn = angle in (0, 90, 180, 270)
    if quarter_turn and (
        operation.expand or image.width == image.height or angle in (0, 180)
    ):
        source = _resizeable(image)
        fill = background if source.mode == "RGBA" else background[:3]
    else:
        source = image.convert("RGBA")
        fill = background
    result = source.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        expand=operation.expand,
        fillcolor=fill,
    )
    radians = -math.radians(angle)
    a, b = round(math.cos(radians), 15), round(math.sin(radians), 15)
    d, e = -b, a
    tx = image.width / 2 - a * result.width / 2 - b * result.height / 2
    ty = image.height / 2 - d * result.width / 2 - e * result.height / 2
    return (
        result,
        {"coordinate_transform": [[a, b, tx], [d, e, ty], [0.0, 0.0, 1.0]]},
        [],
    )


def _pad(image: Image.Image, operation: PadOperation):
    left, top, right, bottom = (
        (operation.border,) * 4
        if operation.border is not None
        else (operation.left, operation.top, operation.right, operation.bottom)
    )
    result = _canvas(
        image,
        (image.width + left + right, image.height + top + bottom),
        (left, top),
        operation.background,
    )
    return (
        result,
        {
            "content_box": [left, top, left + image.width, top + image.height],
            "coordinate_transform": _matrix(tx=-left, ty=-top),
        },
        [],
    )


def _trim(image: Image.Image, operation: TrimOperation):
    alpha = image.convert("RGBA").getchannel("A")
    box = alpha.point(
        lambda value: 255 if value > operation.alpha_threshold else 0
    ).getbbox()
    if box is None:
        return (
            image.copy(),
            {"coordinate_transform": _matrix()},
            [
                "Trim found no pixels above the alpha threshold; image dimensions were retained"
            ],
        )
    return (
        image.crop(box),
        {"crop_box": list(box), "coordinate_transform": _matrix(tx=box[0], ty=box[1])},
        [],
    )


def _font(size: int):
    for name in ("DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _logo(value: str) -> Image.Image:
    try:
        if value.startswith("data:"):
            header, value = value.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError("Expected a base64 data URI")
        data = base64.b64decode(value, validate=True)
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            return ImageOps.exif_transpose(opened).convert("RGBA")
    except (
        ValueError,
        binascii.Error,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise OperationError(
            "logo_base64 must encode a valid image",
            code="invalid_image",
            field="logo_base64",
        ) from exc


def _watermark(image: Image.Image, operation: WatermarkOperation):
    parts = []
    if operation.text is not None:
        font = _font(operation.font_size)
        bounds = ImageDraw.Draw(Image.new("RGBA", (1, 1))).multiline_textbbox(
            (0, 0), operation.text, font=font
        )
        text = Image.new(
            "RGBA", (max(1, bounds[2] - bounds[0]), max(1, bounds[3] - bounds[1]))
        )
        ImageDraw.Draw(text).multiline_text(
            (-bounds[0], -bounds[1]),
            operation.text,
            font=font,
            fill=_rgba(operation.color),
        )
        parts.append(text)
    if operation.logo_base64 is not None:
        logo = _logo(operation.logo_base64)
        if operation.logo_width is not None:
            height = max(1, round(logo.height * operation.logo_width / logo.width))
            logo = logo.resize((operation.logo_width, height), Image.Resampling.LANCZOS)
        parts.append(logo)
    gap = 4 if len(parts) > 1 else 0
    stamp = Image.new(
        "RGBA",
        (sum(part.width for part in parts) + gap, max(part.height for part in parts)),
    )
    x = 0
    for part in parts:
        stamp.alpha_composite(part, dest=(x, (stamp.height - part.height) // 2))
        x += part.width + gap
    stamp.putalpha(
        stamp.getchannel("A").point(lambda alpha: round(alpha * operation.opacity))
    )
    if operation.angle % 360:
        stamp = stamp.rotate(
            operation.angle, resample=Image.Resampling.BICUBIC, expand=True
        )
    overlay = Image.new("RGBA", image.size)
    if operation.position == "repeated":
        spacing = round(operation.spacing * 15 / operation.density)
        for y in range(0, image.height, stamp.height + spacing):
            for x in range(0, image.width, stamp.width + spacing):
                overlay.alpha_composite(stamp, dest=(x, y))
    else:
        positions = (
            ("top-left", "top-right", "bottom-left", "bottom-right")
            if operation.position == "corners"
            else (operation.position,)
        )
        for position in positions:
            ax, ay = _anchor(position)
            x = int((image.width - stamp.width) * ax + operation.margin * (1 - 2 * ax))
            y = int(
                (image.height - stamp.height) * ay + operation.margin * (1 - 2 * ay)
            )
            overlay.alpha_composite(stamp, dest=(x, y))
    result = Image.alpha_composite(image.convert("RGBA"), overlay)
    return result, {"coordinate_transform": _matrix()}, []


def apply_operations(
    image: Image.Image, operations: list
) -> tuple[Image.Image, list[dict], list[str]]:
    """Apply dicts or typed operations, without changing the supplied image."""
    if not isinstance(image, Image.Image):
        raise OperationError(
            "image must be a Pillow image", code="invalid_image", field="image"
        )
    if not isinstance(operations, list):
        raise OperationError("operations must be a list", field="operations")
    result = image.copy()
    logs, warnings = [], []
    for index, raw in enumerate(operations):
        try:
            operation = _OPERATION_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            errors = exc.errors(include_context=False, include_input=False)
            location = list(errors[0]["loc"])
            if location and location[0] in (
                "resize",
                "crop",
                "rotate",
                "flip",
                "pad",
                "trim",
                "flatten",
                "watermark",
            ):
                location.pop(0)
            field = ".".join(
                ["operations", str(index), *(str(item) for item in location)]
            )
            raise OperationError(
                "Invalid image operation", field=field, details=errors
            ) from exc
        before = result.size
        try:
            if isinstance(operation, ResizeOperation):
                result, details, step_warnings = _resize(result, operation)
            elif isinstance(operation, CropOperation):
                result, details, step_warnings = _crop(result, operation)
            elif isinstance(operation, RotateOperation):
                result, details, step_warnings = _rotate(result, operation)
            elif isinstance(operation, FlipOperation):
                horizontal = operation.direction == "horizontal"
                result = (
                    ImageOps.mirror(result) if horizontal else ImageOps.flip(result)
                )
                details = {
                    "coordinate_transform": (
                        _matrix(sx=-1, tx=result.width)
                        if horizontal
                        else _matrix(sy=-1, ty=result.height)
                    )
                }
                step_warnings = []
            elif isinstance(operation, PadOperation):
                result, details, step_warnings = _pad(result, operation)
            elif isinstance(operation, TrimOperation):
                result, details, step_warnings = _trim(result, operation)
            elif isinstance(operation, FlattenOperation):
                result = _canvas(
                    result, result.size, (0, 0), operation.background
                ).convert("RGB")
                details, step_warnings = {"coordinate_transform": _matrix()}, []
            else:
                result, details, step_warnings = _watermark(result, operation)
        except OperationError as exc:
            exc.field = f"operations.{index}" + (f".{exc.field}" if exc.field else "")
            raise
        logs.append(
            {
                "index": index,
                "op": operation.op,
                "input_size": list(before),
                "output_size": list(result.size),
                "coordinate_convention": "pixel_edges",
                **details,
            }
        )
        warnings.extend(step_warnings)
    return result, logs, warnings
