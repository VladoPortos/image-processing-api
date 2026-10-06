"""Canonical request and response contracts for HTTP and MCP callers."""

from typing import Annotated, Any, Literal

from PIL import ImageColor
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .operations import Operation

Format = Annotated[
    Literal["png", "webp", "jpeg", "jpg", "avif"],
    BeforeValidator(lambda value: value.lower() if isinstance(value, str) else value),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputReference(StrictModel):
    artifact_id: str | None = None
    path: str | None = Field(
        None,
        description="Absolute or relative path inside IMAGE_DATA_DIR. Docker paths use /data.",
    )
    image_base64: str | None = Field(
        None,
        description="Raw base64 or a base64 image data URI. Prefer an artifact ID for reuse.",
    )
    filename: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (
            sum(
                value is not None
                for value in (self.artifact_id, self.path, self.image_base64)
            )
            != 1
        ):
            raise ValueError("Provide exactly one of artifact_id, path, image_base64")
        if any(
            value == "" for value in (self.artifact_id, self.path, self.image_base64)
        ):
            raise ValueError("Input source cannot be empty")
        return self


class OutputOptions(StrictModel):
    format: Format = "webp"
    quality: int = Field(
        85,
        ge=1,
        le=100,
        description="Lossy JPEG/WebP/AVIF quality. PNG uses compression_level.",
    )
    background: str = "#ffffff"
    metadata: Literal["preserve", "strip"] = "preserve"
    lossless: bool = Field(False, description="WebP only.")
    method: int = Field(4, ge=0, le=6, description="WebP encoding effort.")
    speed: int = Field(6, ge=0, le=10, description="AVIF encoding speed.")
    compression_level: int = Field(
        6, ge=0, le=9, description="PNG compression, lossless at every level."
    )
    progressive: bool = Field(False, description="JPEG only.")

    @field_validator("background")
    @classmethod
    def valid_background(cls, value):
        try:
            ImageColor.getcolor(value, "RGBA")
        except (ValueError, TypeError) as error:
            raise ValueError("Use a Pillow color name or hex color") from error
        return value

    @model_validator(mode="after")
    def jpeg_opaque_background(self):
        if (
            self.format in ("jpeg", "jpg")
            and ImageColor.getcolor(self.background, "RGBA")[3] != 255
        ):
            raise ValueError("JPEG requires an opaque background")
        return self


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    details: Any = None


class ErrorResponse(BaseModel):
    detail: str
    error: ErrorDetail


class Artifact(BaseModel):
    id: str
    filename: str
    path: str
    download_url: str
    mime_type: str
    format: str
    width: int
    height: int
    mode: str
    has_alpha: bool
    size_bytes: int
    warnings: list[str] = Field(default_factory=list)


class ProcessRequest(StrictModel):
    input: InputReference
    operations: list[Operation] = Field(
        default_factory=list,
        description="Ordered operations on the oriented image; crop edges are exclusive.",
    )
    output: OutputOptions = Field(default_factory=OutputOptions)


class ProcessResult(BaseModel):
    artifact: Artifact
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    coordinate_transform: list[list[float]] | None = Field(
        None,
        description="Maps output pixel-edge coordinates to the oriented source, composed in operation order.",
    )


class InspectRequest(StrictModel):
    input: InputReference


class Inspection(BaseModel):
    filename: str
    format: str
    width: int
    height: int
    raw_width: int
    raw_height: int
    mode: str
    has_alpha: bool
    orientation: int | None
    frames: int
    animated: bool
    size_bytes: int
    has_icc: bool
    has_exif: bool
    dpi: list[float] | None = None
    warnings: list[str] = Field(default_factory=list)


class VariantSpec(StrictModel):
    name: str
    operations: list[Operation] = Field(default_factory=list)
    output: OutputOptions = Field(default_factory=OutputOptions)


class VariantsRequest(StrictModel):
    input: InputReference
    variants: list[VariantSpec] = Field(min_length=1)


class BatchRequest(StrictModel):
    jobs: list[ProcessRequest] = Field(min_length=1)


class ItemResult(BaseModel):
    index: int
    name: str | None = None
    status: Literal["success", "error"]
    result: ProcessResult | None = None
    error: ErrorDetail | None = None


class BatchResult(BaseModel):
    results: list[ItemResult]


class ContactSheetItem(StrictModel):
    input: InputReference
    label: str | None = None


class ContactSheetRequest(StrictModel):
    images: list[ContactSheetItem] = Field(min_length=1)
    columns: int = Field(3, ge=1)
    cell_width: int = Field(256, ge=32)
    cell_height: int = Field(256, ge=32)
    background: str = "#ffffff"
    output: OutputOptions = Field(
        default_factory=lambda: OutputOptions(format="png", metadata="strip")
    )


class SheetItem(BaseModel):
    index: int
    label: str
    input: InputReference
    box: list[int]


class ContactSheetResult(BaseModel):
    artifact: Artifact
    items: list[SheetItem]
    warnings: list[str] = Field(default_factory=list)


class VisionRequest(StrictModel):
    input: InputReference
    max_edge: int | None = Field(None, ge=1)
    max_pixels: int | None = Field(None, ge=1)
    max_bytes: int | None = Field(None, ge=1)
    allow_upscale: bool = False
    output: OutputOptions = Field(default_factory=OutputOptions)
    min_quality: int = Field(35, ge=1, le=100)

    @model_validator(mode="after")
    def quality_range(self):
        if self.min_quality > self.output.quality:
            raise ValueError("min_quality cannot exceed output.quality")
        return self


class VisionResult(ProcessResult):
    quality: int
    budget_met: bool


class CompareRequest(StrictModel):
    input: InputReference
    formats: list[Format] = Field(
        default_factory=lambda: ["avif", "webp", "png", "jpg"], min_length=1
    )
    quality: int = Field(85, ge=1, le=100)


class Base64ConvertRequest(StrictModel):
    image_base64: str
    format: Format
    quality: int = Field(85, ge=1, le=100)


class AIInspectRequest(InspectRequest):
    check_visible: bool = True
    check_invisible: bool = True


class AIClassifyRequest(InspectRequest):
    domain: Literal["photo"] = "photo"


class AICleanRequest(InspectRequest):
    visible: bool = True
    metadata: bool = True


class AICleanResult(BaseModel):
    artifact: Artifact
    visible: dict[str, Any]
    metadata: dict[str, Any]
    invisible: dict[str, Any]
    original_inspection: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class EvidenceSignal(BaseModel):
    name: str
    detail: str
    confidence: str


class AIInspection(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: int
    is_ai_generated: bool | None
    platform: str | None
    confidence: str
    ai_source_kind: str | None
    ai_from_metadata: bool
    watermarks: list[str]
    signals: list[EvidenceSignal]
    caveats: list[str]
    integrity_clashes: list[str]
    c2pa_validation: dict[str, Any] | None = None
    visible: dict[str, Any]
    invisible: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class AIClassification(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: Literal["ai", "human", "unknown"]
    domain: Literal["photo"]
    detector: str
    provider: str | None
    device: Literal["cpu"]
    backend: str
    warnings: list[str] = Field(default_factory=list)


class LibraryCapability(BaseModel):
    installed: bool
    version: str | None


class MetadataCapability(BaseModel):
    inspection: bool
    removal: bool
    c2pa_validation: bool = False


class VisibleMarkCapability(BaseModel):
    key: str
    label: str
    location: str
    automatic: bool


class VisibleCapability(BaseModel):
    available: bool
    backend: str
    marks: list[VisibleMarkCapability]


class InvisibleCapability(BaseModel):
    dwt_dct_detection: bool
    synthid_detection: bool
    general_removal: bool


class ClassificationCapability(BaseModel):
    enabled: bool
    dependencies_ready: bool
    weights_ready: bool
    ready: bool
    domain: Literal["photo"]
    device: Literal["cpu"]
    backend: str
    missing_weights: list[str]


class AICapabilities(BaseModel):
    library: LibraryCapability
    metadata: MetadataCapability
    visible: VisibleCapability
    invisible: InvisibleCapability
    classification: ClassificationCapability


class Capabilities(BaseModel):
    version: str
    formats: list[str]
    operations: list[str]
    data_root: str
    worker_limit: int
    animation: str
    ai: AICapabilities


class MetadataResult(BaseModel):
    filename: str
    format: str
    mode: str
    size: dict[str, int]
    info: dict[str, Any]
    exif: dict[str, Any]
    frames: int
    warnings: list[str]


class ComparisonOriginal(Inspection):
    size_human: str


class Savings(BaseModel):
    bytes: int
    percentage: str
    percentage_value: float


class ComparedFormat(BaseModel):
    quality: int
    size_bytes: int
    size_human: str
    savings: Savings


class ComparisonResult(BaseModel):
    original: ComparisonOriginal
    formats: dict[str, ComparedFormat]
    warnings: list[str]
