"""HTTP interface for local CPU image workflows."""

import asyncio
import json
import logging
import os
import platform
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from PIL import Image

from .artifacts import ArtifactStore
from .core import (
    MIME,
    content_disposition,
    decode_base64,
    decode_image,
    encode_image,
    metadata_result,
    output_filename,
)
from .errors import ImageError
from .models import (
    AICleanRequest,
    AICleanResult,
    AIClassifyRequest,
    AIInspectRequest,
    AIClassification,
    AIInspection,
    Capabilities,
    ComparisonResult,
    MetadataResult,
    Artifact,
    Base64ConvertRequest,
    BatchRequest,
    BatchResult,
    CompareRequest,
    ContactSheetRequest,
    ContactSheetResult,
    ErrorResponse,
    Format,
    InputReference,
    InspectRequest,
    Inspection,
    OutputOptions,
    ProcessRequest,
    ProcessResult,
    VariantsRequest,
    VisionRequest,
    VisionResult,
)
from .operations import OperationError, apply_operations
from .workflows import compare, contact_sheet, prepare_vision, process

VERSION = "0.2.0"
FORM_QUALITY = Annotated[int, Form(ge=1, le=100)]
BINARY_RESPONSES = {
    200: {
        "description": "Encoded image bytes",
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}}
            for mime in set(MIME.values())
        },
    }
}
ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}
logger = logging.getLogger(__name__)


def error_response(error):
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.message, "error": error.as_dict()},
    )


def validation_error(error):
    details = [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": item["msg"],
            "type": item["type"],
        }
        for item in error.errors()
    ]
    return ImageError(
        "validation_error",
        "Request parameters are invalid",
        details=details,
        status_code=422,
    )


def image_response(data, filename, format, warnings=None):
    headers = {"Content-Disposition": content_disposition(filename)}
    if warnings:
        headers["X-Image-Warnings"] = ",".join(warnings)
    return Response(data, media_type=MIME[format], headers=headers)


def create_app(data_root: str | Path | None = None) -> FastAPI:
    store = ArtifactStore(
        data_root
        or os.getenv(
            "IMAGE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")
        )
    )
    worker_limit = max(1, int(os.getenv("IMAGE_WORKERS", "2")))
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(application):
        application.state.image_workers = asyncio.Semaphore(worker_limit)
        yield

    application = FastAPI(
        title="Image Processing API",
        version=VERSION,
        lifespan=lifespan,
        description="Local CPU image processing, reusable artifacts and agent workflows.",
        responses=ERROR_RESPONSES,
    )
    application.state.store = store

    async def work(function, *args, **kwargs):
        async with application.state.image_workers:
            return await run_in_threadpool(function, *args, **kwargs)

    @application.exception_handler(ImageError)
    async def handle_image_error(request, error):
        return error_response(error)

    @application.exception_handler(RequestValidationError)
    @application.exception_handler(ValidationError)
    async def handle_validation_error(request, error):
        return error_response(validation_error(error))

    @application.exception_handler(OperationError)
    async def handle_operation_error(request, error):
        return error_response(
            ImageError(error.code, str(error), field=error.field, details=error.details)
        )

    @application.exception_handler(HTTPException)
    async def handle_http_error(request, error):
        return error_response(
            ImageError("http_error", str(error.detail), status_code=error.status_code)
        )

    @application.exception_handler(Exception)
    async def handle_internal_error(request, error):
        logger.exception("Image request failed", exc_info=error)
        return error_response(
            ImageError("internal_error", "Image processing failed", status_code=500)
        )

    @application.get("/", operation_id="service_info")
    async def root():
        return {
            "message": "Image Processing API is running",
            "version": VERSION,
            "docs": "/docs",
        }

    @application.get("/health", operation_id="health")
    async def health():
        uptime = int(time.monotonic() - started)
        return {
            "status": "healthy",
            "version": VERSION,
            "uptime": {
                "days": uptime // 86400,
                "hours": uptime // 3600 % 24,
                "minutes": uptime // 60 % 60,
                "seconds": uptime % 60,
                "total_seconds": uptime,
            },
            "system_info": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "pillow_version": Image.__version__,
                "supported_formats": list(MIME),
            },
        }

    @application.get(
        "/capabilities", response_model=Capabilities, operation_id="capabilities"
    )
    async def capabilities():
        from .ai_service import capabilities as ai_capabilities

        Image.init()
        return {
            "version": VERSION,
            "formats": [
                name
                for name in MIME
                if ("JPEG" if name in ("jpg", "jpeg") else name.upper()) in Image.SAVE
            ],
            "operations": [
                "crop",
                "resize",
                "rotate",
                "flip",
                "pad",
                "trim",
                "flatten",
                "watermark",
            ],
            "data_root": str(store.root),
            "worker_limit": worker_limit,
            "animation": "first_frame_only",
            "ai": ai_capabilities(),
        }

    def binary_convert(data, filename, options, operations=(), suffix=""):
        decoded = decode_image(data, filename)
        for operation in operations:
            if (
                isinstance(operation, dict)
                and operation.get("op") == "resize"
                and operation.get("fit") == "stretch"
                and operation.get("percentage") is None
                and (
                    operation.get("width") is not None
                    or operation.get("height") is not None
                )
            ):
                operation["width"] = operation.get("width") or decoded.image.width
                operation["height"] = operation.get("height") or decoded.image.height
        image, logs, warnings = apply_operations(decoded.image, list(operations))
        encoded = encode_image(image, options, decoded.metadata)
        return image_response(
            encoded,
            output_filename(filename, options.format, suffix),
            options.format,
            list(dict.fromkeys(decoded.warnings + warnings)),
        )

    @application.post(
        "/convert",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="convert_image",
    )
    async def convert(
        image: UploadFile = File(...),
        format: str = Form(..., json_schema_extra={"enum": list(MIME)}),
        quality: FORM_QUALITY = 85,
    ):
        return await work(
            binary_convert,
            await image.read(),
            image.filename,
            OutputOptions(format=format, quality=quality),
        )

    @application.post(
        "/convert/base64",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="convert_base64",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": Base64ConvertRequest.model_json_schema()
                    }
                },
            }
        },
    )
    async def convert_base64(request: Request):
        content_type = request.headers.get("content-type", "")
        try:
            if "form" in content_type:
                payload = dict(await request.form())
            else:
                body = await request.body()
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    payload = {
                        "image_base64": body.decode("utf-8"),
                        "format": request.query_params.get("format"),
                        "quality": request.query_params.get("quality", 85),
                    }
            parsed = Base64ConvertRequest.model_validate(payload)
        except (UnicodeDecodeError, ValueError) as error:
            if isinstance(error, ValidationError):
                raise validation_error(error) from error
            raise ImageError(
                "invalid_request", "Invalid base64 conversion payload"
            ) from error
        return await work(
            binary_convert,
            decode_base64(parsed.image_base64),
            "image",
            OutputOptions(format=parsed.format, quality=parsed.quality),
        )

    @application.post(
        "/resize",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="resize_image",
    )
    async def resize(
        image: UploadFile = File(...),
        format: str = Form(..., json_schema_extra={"enum": list(MIME)}),
        quality: FORM_QUALITY = 85,
        width: Annotated[int | None, Form(gt=0)] = None,
        height: Annotated[int | None, Form(gt=0)] = None,
        percentage: Annotated[float | None, Form(gt=0)] = None,
        maintain_aspect_ratio: bool = Form(True),
    ):
        operation = {
            "op": "resize",
            "width": width,
            "height": height,
            "percentage": percentage,
            "fit": "contain" if maintain_aspect_ratio else "stretch",
            "allow_upscale": True,
        }
        return await work(
            binary_convert,
            await image.read(),
            image.filename,
            OutputOptions(format=format, quality=quality),
            [operation],
            "_resized",
        )

    @application.post(
        "/crop",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="crop_image",
    )
    async def crop(
        image: UploadFile = File(...),
        format: str = Form(..., json_schema_extra={"enum": list(MIME)}),
        quality: FORM_QUALITY = 85,
        left: int = Form(...),
        top: int = Form(...),
        right: int = Form(...),
        bottom: int = Form(...),
    ):
        return await work(
            binary_convert,
            await image.read(),
            image.filename,
            OutputOptions(format=format, quality=quality),
            [
                {
                    "op": "crop",
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                }
            ],
            "_cropped",
        )

    @application.post(
        "/watermark",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="watermark_image",
    )
    async def watermark(
        image: UploadFile = File(...),
        text: str = Form(...),
        format: str = Form(..., json_schema_extra={"enum": list(MIME)}),
        quality: FORM_QUALITY = 85,
        opacity: Annotated[float, Form(ge=0, le=1)] = 0.5,
        density: Annotated[int, Form(ge=1, le=50)] = 15,
        font_size: Annotated[int | None, Form(gt=0)] = None,
        color: str = Form("#ffffff"),
        position: str = Form("repeated"),
    ):
        data = await image.read()

        def apply():
            decoded = decode_image(data, image.filename)
            operation = {
                "op": "watermark",
                "text": text,
                "opacity": opacity,
                "density": density,
                "font_size": font_size or max(1, max(decoded.image.size) // 20),
                "color": color,
                "position": position,
            }
            result, logs, warnings = apply_operations(decoded.image, [operation])
            options = OutputOptions(format=format, quality=quality)
            return image_response(
                encode_image(result, options, decoded.metadata),
                output_filename(image.filename, format, "_watermarked"),
                format,
                decoded.warnings + warnings,
            )

        return await work(apply)

    @application.post(
        "/metadata", response_model=MetadataResult, operation_id="image_metadata"
    )
    async def metadata(image: UploadFile = File(...)):
        data = await image.read()
        return await work(
            lambda: metadata_result(decode_image(data, image.filename), image.filename)
        )

    @application.post(
        "/info", response_model=ComparisonResult, operation_id="compare_uploaded_image"
    )
    async def info(
        image: UploadFile = File(...),
        quality: FORM_QUALITY = 85,
        formats: list[Format] = Form(["avif", "webp", "png", "jpg"]),
    ):
        data = await image.read()
        return await work(
            lambda: compare(decode_image(data, image.filename), formats, quality)
        )

    @application.post("/images", response_model=Artifact, operation_id="upload_image")
    async def upload_image(image: UploadFile = File(...)):
        return await work(store.save, await image.read(), image.filename or "image")

    @application.post(
        "/images/base64", response_model=Artifact, operation_id="store_image"
    )
    async def store_image(request: InspectRequest):
        def apply():
            data, filename = store.resolve(request.input)
            return store.save(data, filename)

        return await work(apply)

    @application.get(
        "/artifacts/{artifact_id}",
        response_class=Response,
        responses=BINARY_RESPONSES,
        operation_id="download_artifact",
    )
    async def download_artifact(artifact_id: str):
        def apply():
            descriptor = store.descriptor(artifact_id)
            data, filename = store.resolve(InputReference(artifact_id=artifact_id))
            return Response(
                data,
                media_type=descriptor.mime_type,
                headers={"Content-Disposition": content_disposition(filename)},
            )

        return await work(apply)

    @application.get(
        "/artifacts/{artifact_id}/preview",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "image/png": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
        operation_id="preview_artifact",
    )
    async def preview_artifact(
        artifact_id: str, max_edge: Annotated[int, Query(ge=1, le=2048)] = 512
    ):
        def apply():
            data, filename = store.resolve(InputReference(artifact_id=artifact_id))
            decoded = decode_image(data, filename)
            decoded.image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            return Response(
                encode_image(
                    decoded.image, OutputOptions(format="png", metadata="strip")
                ),
                media_type="image/png",
            )

        return await work(apply)

    @application.post(
        "/inspect", response_model=Inspection, operation_id="inspect_image"
    )
    async def inspect_image(request: InspectRequest):
        def apply():
            data, filename = store.resolve(request.input)
            return decode_image(data, filename).inspection

        return await work(apply)

    @application.post(
        "/compare", response_model=ComparisonResult, operation_id="compare_formats"
    )
    async def compare_formats(request: CompareRequest):
        def apply():
            data, filename = store.resolve(request.input)
            return compare(
                decode_image(data, filename), request.formats, request.quality
            )

        return await work(apply)

    @application.post(
        "/process", response_model=ProcessResult, operation_id="process_image"
    )
    async def process_image(request: ProcessRequest):
        return await work(process, store, request)

    @application.post(
        "/variants", response_model=BatchResult, operation_id="create_variants"
    )
    async def variants(request: VariantsRequest):
        def apply():
            data, filename = store.resolve(request.input)
            decoded = decode_image(data, filename)
            results = []
            for index, variant in enumerate(request.variants):
                try:
                    result = process(
                        store,
                        ProcessRequest(
                            input=request.input,
                            operations=variant.operations,
                            output=variant.output,
                        ),
                        decoded,
                        filename,
                    )
                    results.append(
                        {
                            "index": index,
                            "name": variant.name,
                            "status": "success",
                            "result": result,
                        }
                    )
                except ImageError as error:
                    results.append(
                        {
                            "index": index,
                            "name": variant.name,
                            "status": "error",
                            "error": error.as_dict(),
                        }
                    )
            return {"results": results}

        return await work(apply)

    @application.post(
        "/batch", response_model=BatchResult, operation_id="process_batch"
    )
    async def batch(request: BatchRequest):
        def apply():
            results = []
            for index, job in enumerate(request.jobs):
                try:
                    results.append(
                        {
                            "index": index,
                            "status": "success",
                            "result": process(store, job),
                        }
                    )
                except ImageError as error:
                    results.append(
                        {"index": index, "status": "error", "error": error.as_dict()}
                    )
            return {"results": results}

        return await work(apply)

    @application.post(
        "/contact-sheet",
        response_model=ContactSheetResult,
        operation_id="contact_sheet",
    )
    async def create_contact_sheet(request: ContactSheetRequest):
        return await work(contact_sheet, store, request)

    @application.post(
        "/prepare-vision", response_model=VisionResult, operation_id="prepare_vision"
    )
    async def vision(request: VisionRequest):
        return await work(prepare_vision, store, request)

    def ai_call(function_name, source, **kwargs):
        from . import ai_service

        data, filename = store.resolve(source)
        try:
            return getattr(ai_service, function_name)(data, filename, **kwargs)
        except ai_service.AIServiceError as error:
            raise ImageError(
                error.code,
                error.message,
                details=getattr(error, "details", None),
                status_code=error.status_code,
            ) from error

    @application.post(
        "/ai/inspect", response_model=AIInspection, operation_id="inspect_ai_provenance"
    )
    async def ai_inspect(request: AIInspectRequest):
        return await work(
            ai_call,
            "inspect_ai",
            request.input,
            check_visible=request.check_visible,
            check_invisible=request.check_invisible,
        )

    @application.post(
        "/ai/classify",
        response_model=AIClassification,
        operation_id="classify_ai_photo",
    )
    async def ai_classify(request: AIClassifyRequest):
        return await work(ai_call, "classify_ai", request.input, domain=request.domain)

    @application.post(
        "/ai/clean", response_model=AICleanResult, operation_id="clean_ai_marks"
    )
    async def ai_clean(request: AICleanRequest):
        def apply():
            result = ai_call(
                "clean_ai",
                request.input,
                visible=request.visible,
                metadata=request.metadata,
            )
            data = result.pop("data")
            decoded = decode_image(data)
            original_filename = store.resolve(request.input)[1]
            filename = output_filename(
                original_filename, decoded.inspection["format"], "_cleaned"
            )
            result["artifact"] = store.save(
                data, filename, warnings=result.get("warnings"), decoded=decoded
            )
            return result

        return await work(apply)

    return application


app = create_app()
