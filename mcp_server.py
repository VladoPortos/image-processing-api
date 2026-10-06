"""Optional stdio MCP adapter for the local Image Processing HTTP API.

Image processing remains in the API. This module only validates tool arguments,
forwards HTTP requests, and presents structured results and bounded previews.
"""

from __future__ import annotations

import base64
import os
import re
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import (
    CallToolResult,
    ImageContent,
    ResourceLink,
    TextContent,
    ToolAnnotations,
)
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from app.models import (
    AICleanRequest,
    AICleanResult,
    AIClassification,
    AIClassifyRequest,
    AIInspection,
    AIInspectRequest,
    Artifact,
    BatchRequest,
    BatchResult,
    Capabilities,
    ContactSheetItem,
    ContactSheetRequest,
    ContactSheetResult,
    ErrorDetail,
    ErrorResponse,
    InputReference,
    Inspection,
    InspectRequest,
    OutputOptions,
    ProcessRequest,
    ProcessResult,
    VariantSpec,
    VariantsRequest,
    VisionRequest,
    VisionResult,
)
from app.operations import Operation

DEFAULT_API_URL = "http://127.0.0.1:8078"
PREVIEW_MAX_EDGE = 512
PREVIEW_MAX_BYTES = 128 * 1024
API_TIMEOUT_SECONDS = 120.0


class ProcessOutput(RootModel[ProcessResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class InspectionOutput(RootModel[Inspection | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class BatchOutput(RootModel[BatchResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class ContactSheetOutput(RootModel[ContactSheetResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class VisionOutput(RootModel[VisionResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class AIInspectionOutput(RootModel[AIInspection | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class AIClassificationOutput(RootModel[AIClassification | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class AICleanOutput(RootModel[AICleanResult | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


class CapabilitiesOutput(RootModel[Capabilities | ErrorResponse]):
    model_config = ConfigDict(json_schema_extra={"type": "object"})


def _without_image_data(value: Any) -> Any:
    """Never repeat image bytes in textual or structured output."""
    if isinstance(value, dict):
        return {
            key: _without_image_data(item)
            for key, item in value.items()
            if key != "image_bytes"
            and not key.endswith("_base64")
            and not isinstance(item, (bytes, bytearray))
        }
    if isinstance(value, list):
        return [
            _without_image_data(item)
            for item in value
            if not isinstance(item, (bytes, bytearray))
        ]
    if isinstance(value, str) and (
        value.startswith("data:image/")
        or (len(value) >= 1024 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", value))
    ):
        return "[image data omitted]"
    return value


def _artifacts(value: Any) -> list[Artifact]:
    """Collect ordered, unique artifact descriptors from single and item results."""
    found: list[Artifact] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if isinstance(item.get("artifact"), dict):
                candidate = Artifact.model_validate(item["artifact"])
                if candidate.id not in seen:
                    seen.add(candidate.id)
                    found.append(candidate)
            for key, child in item.items():
                if key != "artifact":
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _error(
    code: str, message: str, *, field: str | None = None, details: Any = None
) -> CallToolResult:
    result = ErrorResponse(
        detail=message,
        error=ErrorDetail(code=code, message=message, field=field, details=details),
    )
    structured = _without_image_data(result.model_dump(mode="json"))
    return CallToolResult(
        isError=True,
        structuredContent=structured,
        content=[
            TextContent(
                type="text",
                text=f"{structured['error']['code']}: {structured['error']['message']}"
                + (f" (field: {structured['error']['field']})" if field else ""),
            )
        ],
    )


class ImageAPI:
    """A short-lived HTTP client per tool call; no independent processing core."""

    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")
        parsed = httpx.URL(self.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("IMAGE_API_URL must be an absolute HTTP or HTTPS URL")

    def artifact_url(self, artifact: Artifact) -> str:
        return f"{self.api_url}/artifacts/{quote(artifact.id, safe='')}"

    async def preview(
        self, client: httpx.AsyncClient, artifact: Artifact
    ) -> ImageContent | None:
        url = f"{self.artifact_url(artifact)}/preview"
        try:
            async with client.stream(
                "GET", url, params={"max_edge": PREVIEW_MAX_EDGE}
            ) as response:
                mime = (
                    response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if response.status_code != 200 or mime not in {
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                }:
                    return None
                if int(response.headers.get("Content-Length", "0")) > PREVIEW_MAX_BYTES:
                    return None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > PREVIEW_MAX_BYTES:
                        return None
            with Image.open(BytesIO(data)) as picture:
                if max(picture.size) > PREVIEW_MAX_EDGE or picture.format not in {
                    "PNG",
                    "JPEG",
                    "WEBP",
                }:
                    return None
                if (
                    mime
                    != {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[
                        picture.format
                    ]
                ):
                    return None
                picture.verify()
            return ImageContent(
                type="image", mimeType=mime, data=base64.b64encode(data).decode("ascii")
            )
        except (
            httpx.HTTPError,
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            return None

    async def request(
        self, route: str, request: BaseModel | None, response_model: type[BaseModel]
    ) -> CallToolResult:
        try:
            async with httpx.AsyncClient(
                timeout=API_TIMEOUT_SECONDS, trust_env=False
            ) as client:
                response = await client.request(
                    "GET" if request is None else "POST",
                    f"{self.api_url}{route}",
                    json=(
                        None
                        if request is None
                        else request.model_dump(mode="json", exclude_none=True)
                    ),
                )
                try:
                    payload = response.json()
                except ValueError:
                    return _error(
                        "api_error" if response.is_error else "invalid_api_response",
                        "The image API returned a non-JSON response",
                        details={"status_code": response.status_code},
                    )
                if response.is_error:
                    detail = payload.get("error") if isinstance(payload, dict) else None
                    if (
                        isinstance(detail, dict)
                        and isinstance(detail.get("code"), str)
                        and isinstance(detail.get("message"), str)
                    ):
                        return _error(
                            detail["code"],
                            detail["message"],
                            field=detail.get("field"),
                            details=detail.get("details"),
                        )
                    message = (
                        payload.get("detail", "The image API request failed")
                        if isinstance(payload, dict)
                        else "The image API request failed"
                    )
                    return _error(
                        "api_error",
                        (
                            message
                            if isinstance(message, str)
                            else "The image API request failed"
                        ),
                        details={"status_code": response.status_code},
                    )
                if not isinstance(payload, dict):
                    return _error(
                        "invalid_api_response",
                        "The image API returned a non-object JSON response",
                    )
                payload = _without_image_data(payload)
                response_model.model_validate(payload)
                artifacts = _artifacts(payload)
                content: list[TextContent | ResourceLink | ImageContent] = [
                    TextContent(
                        type="text",
                        text=(
                            f"Completed successfully. Generated {len(artifacts)} artifact(s)."
                            if artifacts
                            else "Completed successfully; the result is available as structured content."
                        ),
                    )
                ]
                for artifact in artifacts:
                    content.append(
                        ResourceLink(
                            type="resource_link",
                            uri=self.artifact_url(artifact),
                            name=artifact.filename,
                            mimeType=artifact.mime_type,
                            size=artifact.size_bytes,
                        )
                    )
                if artifacts:
                    preview = await self.preview(client, artifacts[0])
                    if preview is not None:
                        content.append(preview)
                return CallToolResult(content=content, structuredContent=payload)
        except httpx.HTTPError as exc:
            return _error(
                "api_unavailable",
                "Could not reach the image API",
                details={"api_url": self.api_url, "reason": str(exc)},
            )
        except ValidationError:
            return _error(
                "invalid_api_response",
                "The image API response did not match its documented schema",
            )


def create_server(api_url: str | None = None) -> FastMCP:
    """Create the adapter without connecting until a tool is called."""
    api = ImageAPI(api_url or os.environ.get("IMAGE_API_URL", DEFAULT_API_URL))
    server = FastMCP(
        "Image Processing API",
        instructions="Local CPU image tools. Reuse artifact_id values for subsequent calls. Paths are resolved by the API inside IMAGE_DATA_DIR, not by this MCP process. Use capabilities to discover optional AI features.",
        log_level="WARNING",
    )
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False
    )
    processing = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False
    )

    @server.tool(annotations=read_only)
    async def capabilities() -> Annotated[CallToolResult, CapabilitiesOutput]:
        """Discover supported formats, ordered operations, and optional CPU AI capabilities."""
        return await api.request("/capabilities", None, Capabilities)

    @server.tool(annotations=read_only)
    async def inspect_image(
        input: InputReference,
    ) -> Annotated[CallToolResult, InspectionOutput]:
        """Inspect format, oriented dimensions, alpha, frames and metadata without encoding variants."""
        return await api.request("/inspect", InspectRequest(input=input), Inspection)

    @server.tool(annotations=processing)
    async def process_image(
        input: InputReference,
        operations: list[Operation],
        output: OutputOptions = OutputOptions(),
    ) -> Annotated[CallToolResult, ProcessOutput]:
        """Apply operations in order using displayed, EXIF-oriented coordinates; return a reusable artifact."""
        return await api.request(
            "/process",
            ProcessRequest(input=input, operations=operations, output=output),
            ProcessResult,
        )

    @server.tool(annotations=processing)
    async def create_variants(
        input: InputReference,
        variants: Annotated[list[VariantSpec], Field(min_length=1)],
    ) -> Annotated[CallToolResult, BatchOutput]:
        """Create named outputs from one input; preserve ordered per-variant successes and errors."""
        return await api.request(
            "/variants", VariantsRequest(input=input, variants=variants), BatchResult
        )

    @server.tool(annotations=processing)
    async def process_batch(
        jobs: Annotated[list[ProcessRequest], Field(min_length=1)],
    ) -> Annotated[CallToolResult, BatchOutput]:
        """Process ordered jobs independently, preserving each success or actionable error."""
        return await api.request("/batch", BatchRequest(jobs=jobs), BatchResult)

    @server.tool(annotations=processing)
    async def contact_sheet(
        images: Annotated[list[ContactSheetItem], Field(min_length=1)],
        columns: Annotated[int, Field(ge=1)] = 3,
        cell_width: Annotated[int, Field(ge=32)] = 256,
        cell_height: Annotated[int, Field(ge=32)] = 256,
        background: str = "#ffffff",
        output: OutputOptions = OutputOptions(format="png", metadata="strip"),
    ) -> Annotated[CallToolResult, ContactSheetOutput]:
        """Make a labeled contact sheet; returned item boxes map cells to the original inputs."""
        request = ContactSheetRequest(
            images=images,
            columns=columns,
            cell_width=cell_width,
            cell_height=cell_height,
            background=background,
            output=output,
        )
        return await api.request("/contact-sheet", request, ContactSheetResult)

    @server.tool(annotations=processing)
    async def prepare_vision(
        input: InputReference,
        max_edge: Annotated[int | None, Field(ge=1)] = None,
        max_pixels: Annotated[int | None, Field(ge=1)] = None,
        max_bytes: Annotated[int | None, Field(ge=1)] = None,
        allow_upscale: bool = False,
        min_quality: Annotated[int, Field(ge=1, le=100)] = 35,
        output: OutputOptions = OutputOptions(),
    ) -> Annotated[CallToolResult, VisionOutput]:
        """Prepare an image for edge, pixel and byte budgets; min_quality must not exceed output.quality."""
        request = VisionRequest(
            input=input,
            max_edge=max_edge,
            max_pixels=max_pixels,
            max_bytes=max_bytes,
            allow_upscale=allow_upscale,
            min_quality=min_quality,
            output=output,
        )
        return await api.request("/prepare-vision", request, VisionResult)

    @server.tool(annotations=read_only)
    async def ai_inspect(
        input: InputReference, check_visible: bool = True, check_invisible: bool = True
    ) -> Annotated[CallToolResult, AIInspectionOutput]:
        """Inspect provenance and watermark evidence; absent evidence leaves the origin unknown."""
        return await api.request(
            "/ai/inspect",
            AIInspectRequest(
                input=input,
                check_visible=check_visible,
                check_invisible=check_invisible,
            ),
            AIInspection,
        )

    @server.tool(annotations=read_only)
    async def ai_classify(
        input: InputReference, domain: Literal["photo"] = "photo"
    ) -> Annotated[CallToolResult, AIClassificationOutput]:
        """Run the optional local photo classifier on CPU; requires configured dependencies and weights."""
        return await api.request(
            "/ai/classify",
            AIClassifyRequest(input=input, domain=domain),
            AIClassification,
        )

    @server.tool(annotations=processing)
    async def ai_clean(
        input: InputReference, visible: bool = True, metadata: bool = True
    ) -> Annotated[CallToolResult, AICleanOutput]:
        """Create a cleaned artifact using supported CPU visible-mark and metadata cleanup stages."""
        return await api.request(
            "/ai/clean",
            AICleanRequest(input=input, visible=visible, metadata=metadata),
            AICleanResult,
        )

    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
