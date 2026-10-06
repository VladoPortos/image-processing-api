"""Optional CPU provenance, registered-mark cleanup, and photo classification.

Original containers are inspected before decoding/re-encoding. No entry point in
this module downloads models, selects CUDA, or invokes the combined/diffusion API.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import io
import os
import threading
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image, PngImagePlugin, UnidentifiedImageError


CLASSIFIER_ENABLE_ENV = "IMAGE_API_ENABLE_CLASSIFIER"
CLASSIFIER_WEIGHTS_ENV = "RAIW_CLASSIFY_WEIGHTS"
# Verified against the required torch files in remove-ai-watermarks 0.45.0.
CLASSIFIER_WEIGHT_FILES = (
    "clip-l-ft.pt",
    "probe-weights-clip-l-ft.npz",
    "detector.pt",
    "provider.pt",
    "config.json",
    "preprocessor_config.json",
)
INVISIBLE_LIMITATION = "general_image_removal_unsupported_on_cpu"
_CLASSIFIER_LOCK = threading.Lock()


class AIServiceError(Exception):
    """Transport-independent error consumed by the HTTP layer."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _module_available(name: str) -> bool:
    try:
        spec = importlib.util.find_spec(name)
        return spec is not None and spec.loader is not None
    except (ImportError, ValueError):
        return False


def _import(name: str):
    try:
        return importlib.import_module(name)
    except (ImportError, OSError) as exc:
        raise AIServiceError(
            "ai_dependencies_unavailable",
            "CPU AI dependencies are unavailable; install requirements-ai.txt.",
            503,
            {"dependency": getattr(exc, "name", None) or name},
        ) from exc


def _validate_image(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = image.format
            image.load()
            if fmt not in {"PNG", "JPEG", "WEBP", "AVIF", "GIF", "TIFF", "BMP"}:
                raise AIServiceError(
                    "unsupported_image_format", "Unsupported AI image format.", 415
                )
            return ".jpg" if fmt == "JPEG" else "." + fmt.lower()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise AIServiceError(
            "invalid_image", "The input is not a decodable image.", 400
        ) from exc


def _weights_status() -> tuple[bool, list[str]]:
    configured = os.environ.get(CLASSIFIER_WEIGHTS_ENV)
    if not configured:
        return False, list(CLASSIFIER_WEIGHT_FILES)
    folder = Path(configured)
    missing = []
    for name in CLASSIFIER_WEIGHT_FILES:
        candidates = (folder / name, folder / "run1" / name)
        if not any(path.is_file() and path.stat().st_size > 0 for path in candidates):
            missing.append(name)
    return folder.is_dir() and not missing, missing


def _classifier_enabled() -> bool:
    return os.environ.get(CLASSIFIER_ENABLE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def capabilities() -> dict[str, Any]:
    """Report installation and local file readiness without importing ML runtimes."""
    installed = _module_available("remove_ai_watermarks")
    try:
        version = (
            importlib.metadata.version("remove-ai-watermarks") if installed else None
        )
    except importlib.metadata.PackageNotFoundError:
        version = None
    metadata_ready = installed and _module_available("piexif")
    pixels_ready = installed and all(
        _module_available(name) for name in ("numpy", "cv2")
    )
    detect_ready = pixels_ready and _module_available("pywt")
    classifier_deps = pixels_ready and all(
        _module_available(name) for name in ("torch", "transformers", "huggingface_hub")
    )
    weights_ready, missing_weights = _weights_status()
    marks = []
    if installed:
        # The registry contains lazy engine callbacks; this imports neither cv2 nor ML.
        try:
            registry = importlib.import_module(
                "remove_ai_watermarks.watermark_registry"
            )
            marks = [
                {
                    "key": mark.key,
                    "label": mark.label,
                    "location": mark.location,
                    "automatic": mark.in_auto,
                }
                for mark in registry.known_marks()
            ]
        except (ImportError, OSError):
            pixels_ready = False
    enabled = _classifier_enabled()
    return {
        "library": {"installed": installed, "version": version},
        "metadata": {
            "inspection": metadata_ready,
            "removal": metadata_ready,
            "c2pa_validation": installed and _module_available("c2pa"),
        },
        "visible": {"available": pixels_ready, "backend": "cv2", "marks": marks},
        "invisible": {
            "dwt_dct_detection": detect_ready,
            "synthid_detection": False,
            "general_removal": False,
        },
        "classification": {
            "enabled": enabled,
            "dependencies_ready": classifier_deps,
            "weights_ready": weights_ready,
            "ready": enabled and classifier_deps and weights_ready,
            "domain": "photo",
            "device": "cpu",
            "backend": "torch",
            "missing_weights": missing_weights,
        },
    }


def _inspect_path(
    path: Path, check_visible: bool, check_invisible: bool
) -> dict[str, Any]:
    identify = _import("remove_ai_watermarks.identify")
    available = capabilities()
    visible_ready = available["visible"]["available"]
    invisible_ready = available["invisible"]["dwt_dct_detection"]
    # identify's invisible arm can invoke TrustMark and download its model. Select
    # only the keyless, model-free DWT-DCT decoder explicitly instead.
    report = identify.identify(
        path,
        check_visible=check_visible and visible_ready,
        check_invisible=False,
    )
    result = report.to_dict(schema_version=1)
    result["visible"] = {
        "status": (
            "skipped"
            if not check_visible
            else "checked" if visible_ready else "unavailable"
        ),
        "backend": "cv2",
    }
    decoded = None
    warnings = []
    if check_visible and not visible_ready:
        warnings.append(
            "Visible-mark detection requires the optional CPU pixel dependencies."
        )
    if check_invisible and invisible_ready:
        decoder = _import("remove_ai_watermarks.invisible_watermark")
        decoded = decoder.detect_invisible_watermark(path)
        if decoded:
            result["signals"].append(
                {"name": "invisible_watermark", "detail": decoded, "confidence": "high"}
            )
            result["watermarks"].append(f"Open invisible watermark: {decoded}")
            result["is_ai_generated"] = True
            result["ai_source_kind"] = result["ai_source_kind"] or "pixel_watermark"
            result["confidence"] = "high"
            result["platform"] = (
                result["platform"] or f"{decoded} (open DWT-DCT watermark)"
            )
    elif check_invisible:
        warnings.append(
            "Open invisible-watermark detection requires the optional detect dependencies."
        )
    result["invisible"] = {
        "status": (
            "skipped"
            if not check_invisible
            else "checked" if invisible_ready else "unavailable"
        ),
        "detected": decoded,
        "synthid": "unsupported",
        "trustmark": "not_processed",
    }
    if check_invisible:
        warnings.append(
            "No general local SynthID pixel decoder is available; absent signals leave origin unknown."
        )
    result["warnings"] = warnings
    return result


def inspect_ai(
    data: bytes,
    filename: str,
    check_visible: bool = True,
    check_invisible: bool = True,
) -> dict[str, Any]:
    """Inspect original bytes and retain unknown provenance when evidence is absent."""
    suffix = _validate_image(data)
    with TemporaryDirectory(prefix="image-ai-") as directory:
        path = Path(directory) / ("original" + suffix)
        path.write_bytes(data)
        try:
            return _inspect_path(path, check_visible, check_invisible)
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError(
                "ai_processing_failed", "CPU AI inspection failed."
            ) from exc


def _carry_png_metadata(source: Path, output: Path) -> None:
    """Retain readable text, EXIF and display tags during a lossless pixel write."""
    with Image.open(source) as original, Image.open(output) as cleaned:
        info = PngImagePlugin.PngInfo()
        for key, value in original.info.items():
            if isinstance(value, str):
                info.add_itxt(key, value)
        if isinstance(original.info.get("xmp"), bytes):
            info.add_itxt(
                "XML:com.adobe.xmp",
                original.info["xmp"].decode("utf-8", errors="replace"),
            )
        options = {"pnginfo": info}
        for key in ("exif", "icc_profile", "dpi"):
            if original.info.get(key):
                options[key] = original.info[key]
        encoded = io.BytesIO()
        cleaned.save(encoded, "PNG", **options)
    output.write_bytes(encoded.getvalue())


def _metadata_evidence(path: Path, metadata_api) -> dict[str, str]:
    # v0.45.0 get_ai_metadata omits ordinary EXIF generator tags even though
    # identify and the scrubber recognize them. Verify that signal explicitly.
    evidence = dict(metadata_api.get_ai_metadata(path))
    generator = metadata_api.exif_generator(path)
    if generator:
        evidence["exif_generator"] = generator
    return evidence


def clean_ai(
    data: bytes, filename: str, visible: bool = True, metadata: bool = True
) -> dict[str, Any]:
    """Fill registered visible marks on CPU and verify selected AI metadata removal."""
    suffix = _validate_image(data)
    with TemporaryDirectory(prefix="image-ai-") as directory:
        source = Path(directory) / ("original" + suffix)
        source.write_bytes(data)
        try:
            original = _inspect_path(source, visible, True)
            warnings = list(original["warnings"])
            metadata_api = _import("remove_ai_watermarks.metadata")
            original_metadata = _metadata_evidence(source, metadata_api)
            current = source
            visible_summary = {"status": "skipped", "backend": "cv2", "marks": []}
            if visible:
                if not capabilities()["visible"]["available"]:
                    raise AIServiceError(
                        "ai_dependencies_unavailable",
                        "Visible cleanup requires requirements-ai.txt.",
                        503,
                    )
                with Image.open(source) as image:
                    if getattr(image, "n_frames", 1) > 1:
                        raise AIServiceError(
                            "unsupported_animated_image",
                            "Visible cleanup requires a static image.",
                            422,
                        )
                api = _import("remove_ai_watermarks.api")
                output = Path(directory) / "visible.png"
                report = api.remove_visible_detailed(
                    source,
                    output,
                    backend="cv2",
                    strip_metadata=False,
                    write_noop=False,
                )
                visible_summary = {
                    "status": report.status,
                    "backend": "cv2",
                    "marks": [asdict(mark) for mark in report.marks],
                }
                if report.marks:
                    _carry_png_metadata(source, output)
                    current = output
                    warnings.append(
                        "Visible edits are saved as lossless PNG and can invalidate signed Content Credentials."
                    )
                    if not metadata:
                        warnings.append(
                            "Readable text/EXIF metadata is retained; embedded container credentials may not survive pixel re-encoding."
                        )
            metadata_summary = {
                "status": "skipped",
                "removed_fields": [],
                "remaining": {},
            }
            if metadata:
                output = Path(directory) / ("metadata" + current.suffix)
                current, remaining = metadata_api.strip_and_verify(
                    current, output, keep_standard=True
                )
                remaining = {**remaining, **_metadata_evidence(current, metadata_api)}
                metadata_summary = {
                    "status": (
                        "partial"
                        if remaining
                        else "removed" if original_metadata else "no_metadata"
                    ),
                    "removed_fields": sorted(set(original_metadata) - set(remaining)),
                    "remaining": remaining,
                }
                if remaining:
                    warnings.append(
                        "AI metadata remains after the verified cleanup attempt."
                    )
            return {
                "data": current.read_bytes(),
                "visible": visible_summary,
                "metadata": metadata_summary,
                "invisible": {
                    "status": "not_processed",
                    "reason": INVISIBLE_LIMITATION,
                },
                "original_inspection": original,
                "warnings": warnings,
            }
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError(
                "ai_processing_failed", "CPU AI cleanup failed."
            ) from exc


def classify_ai(data: bytes, filename: str, domain: str = "photo") -> dict[str, Any]:
    """Opt-in photographic classifier with local weights and explicit CPU torch."""
    if domain != "photo":
        raise AIServiceError(
            "unsupported_domain", "Classification supports photographs only.", 422
        )
    if not _classifier_enabled():
        raise AIServiceError(
            "classifier_disabled",
            f"Enable optional photo classification with {CLASSIFIER_ENABLE_ENV}=true.",
            503,
        )
    weights_ready, missing = _weights_status()
    if not weights_ready:
        raise AIServiceError(
            "classifier_weights_unavailable",
            f"Set {CLASSIFIER_WEIGHTS_ENV} to complete locally cached classifier weights.",
            503,
            {"missing_weights": missing},
        )
    if not capabilities()["classification"]["dependencies_ready"]:
        raise AIServiceError(
            "ai_dependencies_unavailable",
            "Install requirements-classify.txt and CPU PyTorch to classify photographs.",
            503,
        )
    suffix = _validate_image(data)
    classifier = _import("remove_ai_watermarks.classify")
    with TemporaryDirectory(prefix="image-ai-") as directory:
        path = Path(directory) / ("original" + suffix)
        path.write_bytes(data)
        try:
            # v0.45.0 loads small CLIP config/processor files via from_pretrained
            # even when model weights are local. A local BACKBONE directory makes
            # those reads local as well; the lock keeps configuration and runtime
            # loading together for simultaneous requests.
            with _CLASSIFIER_LOCK:
                clip = _import("remove_ai_watermarks._internal.clip_l_ft")
                folder = Path(os.environ[CLASSIFIER_WEIGHTS_ENV]).resolve()
                if not (folder / "config.json").is_file():
                    folder /= "run1"
                clip.BACKBONE = str(folder)
                result = classifier.classify_pixels(
                    path, device="cpu", backend="torch"
                ).to_dict()
        except AIServiceError:
            raise
        except Exception as exc:
            raise AIServiceError(
                "ai_processing_failed", "CPU photo classification failed."
            ) from exc
    result.update(
        {
            "device": "cpu",
            "backend": "torch",
            "warnings": [
                "Photo classification is an unbenchmarked model opinion, not proof of origin or watermark presence."
            ],
        }
    )
    return result
