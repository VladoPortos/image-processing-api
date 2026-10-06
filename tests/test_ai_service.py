"""CPU AI integration contracts, including real library image fixtures."""

import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import types

import pytest
from PIL import Image, PngImagePlugin


@pytest.fixture
def service():
    assert (
        importlib.util.find_spec("app.ai_service") is not None
    ), "CPU AI service is not implemented"
    return importlib.import_module("app.ai_service")


@pytest.fixture
def cpu_dependencies():
    for name in ("remove_ai_watermarks", "cv2", "numpy", "pywt", "piexif"):
        pytest.importorskip(name)


def image_bytes(fmt="PNG", *, ai_metadata=False, mode="RGBA"):
    image = Image.new(
        mode, (160, 120), (53, 84, 121, 127) if mode == "RGBA" else (53, 84, 121)
    )
    kwargs = {}
    if fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Original author")
        if ai_metadata:
            info.add_text(
                "parameters",
                "a forest, Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 123",
            )
        kwargs["pnginfo"] = info
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def test_invalid_image_is_a_client_error(service):
    with pytest.raises(service.AIServiceError) as caught:
        service.inspect_ai(b"not an image", "invalid.png")
    assert caught.value.code == "invalid_image"
    assert caught.value.status_code == 400


def test_unmarked_original_stays_unknown(service, cpu_dependencies):
    result = service.inspect_ai(
        image_bytes(), "original.png", check_visible=False, check_invisible=False
    )
    assert result["is_ai_generated"] is None
    assert result["visible"]["status"] == "skipped"
    assert result["invisible"]["status"] == "skipped"
    json.dumps(result, allow_nan=False)
    assert "path" not in result


def test_provenance_is_inspected_before_reencoding(service, cpu_dependencies):
    result = service.inspect_ai(
        image_bytes(ai_metadata=True), "misleading.jpg", False, False
    )
    assert result["is_ai_generated"] is True
    assert result["ai_from_metadata"] is True
    assert any("parameters" in signal["detail"].lower() for signal in result["signals"])


def test_metadata_cleaning_is_verified_and_preserves_alpha(service, cpu_dependencies):
    original = image_bytes(ai_metadata=True)
    result = service.clean_ai(original, "metadata.png", visible=False)
    assert result["metadata"]["status"] == "removed"
    assert result["metadata"]["remaining"] == {}
    assert result["original_inspection"]["is_ai_generated"] is True
    assert result["invisible"]["status"] == "not_processed"
    with Image.open(io.BytesIO(original)) as before, Image.open(
        io.BytesIO(result["data"])
    ) as after:
        assert after.size == before.size
        assert after.mode == before.mode
        assert after.tobytes() == before.tobytes()
        assert after.info["Author"] == "Original author"
        assert "parameters" not in after.info
    assert (
        service.inspect_ai(result["data"], "clean.png", False, False)["is_ai_generated"]
        is None
    )
    json.dumps(
        {key: value for key, value in result.items() if key != "data"}, allow_nan=False
    )


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "AVIF"])
def test_container_metadata_cleanup_preserves_decoded_pixels(
    service, cpu_dependencies, fmt
):
    Image.init()
    if fmt not in Image.SAVE:
        pytest.skip(f"Pillow {fmt} encoder unavailable")
    image = Image.new(
        "RGB" if fmt == "JPEG" else "RGBA",
        (96, 64),
        (43, 91, 151) if fmt == "JPEG" else (43, 91, 151, 113),
    )
    exif = Image.Exif()
    exif[305] = "ComfyUI"
    exif[315] = "Original author"
    buffer = io.BytesIO()
    image.save(buffer, fmt, exif=exif)
    original = buffer.getvalue()
    result = service.clean_ai(original, "misleading.png", visible=False)
    assert result["original_inspection"]["is_ai_generated"] is True
    assert result["metadata"]["status"] == "removed"
    assert result["metadata"]["remaining"] == {}
    with Image.open(io.BytesIO(original)) as before, Image.open(
        io.BytesIO(result["data"])
    ) as after:
        assert after.format == fmt
        assert after.size == before.size
        assert after.tobytes() == before.tobytes()
        assert after.getexif().get(315) == "Original author"
    assert (
        service.inspect_ai(result["data"], "output.png", False, False)[
            "is_ai_generated"
        ]
        is None
    )


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB"])
def test_transparent_png_noop_preserves_all_container_bytes(
    service, cpu_dependencies, mode
):
    if mode == "P":
        image = Image.new("P", (120, 80), 1)
        image.putpalette([23, 43, 83, 74, 91, 105] + [0] * 762)
        image.info["transparency"] = 1
    elif mode == "RGB":
        image = Image.new("RGB", (120, 80), (43, 91, 151))
        image.info["transparency"] = (43, 91, 151)
    elif mode == "LA":
        image = Image.new("LA", (120, 80), (80, 117))
    else:
        image = Image.new("RGBA", (120, 80), (43, 91, 151, 113))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    original = buffer.getvalue()
    result = service.clean_ai(original, "transparent.png", metadata=False)
    assert result["visible"]["status"] == "no_watermark"
    assert result["data"] == original


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "AVIF"])
def test_visible_noop_preserves_original_bytes(service, cpu_dependencies, fmt):
    if fmt not in Image.SAVE:
        Image.init()
    if fmt not in Image.SAVE:
        pytest.skip(f"Pillow {fmt} encoder unavailable")
    original = image_bytes(fmt, mode="RGB")
    result = service.clean_ai(original, f"unchanged.{fmt.lower()}", metadata=False)
    assert result["visible"]["status"] == "no_watermark"
    assert result["data"] == original


def test_disabled_stages_preserve_original_bytes(service, cpu_dependencies):
    original = image_bytes(ai_metadata=True)
    result = service.clean_ai(original, "original.png", visible=False, metadata=False)
    assert result["data"] == original
    assert result["visible"]["status"] == "skipped"
    assert result["metadata"]["status"] == "skipped"


def test_real_template_cleanup_uses_cpu_and_preserves_unmasked_pixels(
    service, cpu_dependencies
):
    import numpy as np
    from remove_ai_watermarks.gemini_engine import GeminiEngine

    # The library's captured 48px template, composited onto an independent gradient.
    size = 512
    yy, xx = np.mgrid[0:size, 0:size]
    base = 130 + 17 * np.sin(xx / 38) + 13 * np.cos(yy / 51)
    rgb = np.stack((base * 1.03, base, base * 0.97), axis=-1)
    alpha = GeminiEngine().get_interpolated_alpha(48)[..., None]
    rgb[432:480, 432:480] = alpha * 255 + (1 - alpha) * rgb[432:480, 432:480]
    pixels = np.concatenate(
        (np.clip(rgb, 0, 255).astype("uint8"), np.full((size, size, 1), 127, "uint8")),
        axis=-1,
    )
    out = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Original author")
    Image.fromarray(pixels).save(out, "PNG", pnginfo=info)
    result = service.clean_ai(out.getvalue(), "sparkle.png", metadata=False)
    assert result["visible"]["status"] in ("cleaned", "partial", "unvalidated")
    marks = result["visible"]["marks"]
    assert any(mark["key"] == "gemini" for mark in marks)
    assert all(mark["backend"] == "cv2" for mark in marks)
    with Image.open(io.BytesIO(result["data"])) as image:
        after = np.asarray(image.convert("RGBA"))
        assert image.info["Author"] == "Original author"
    np.testing.assert_array_equal(after[..., 3], pixels[..., 3])
    outside = np.ones((size, size), dtype=bool)
    for mark in marks:
        x, y, width, height = mark["mask_bbox"]
        outside[y : y + height, x : x + width] = False
    np.testing.assert_array_equal(after[outside], pixels[outside])
    assert np.any(after[..., :3] != pixels[..., :3])
    json.dumps(
        {key: value for key, value in result.items() if key != "data"}, allow_nan=False
    )


def test_classifier_is_opt_in_and_rejects_non_photos(service, monkeypatch):
    monkeypatch.delenv("IMAGE_API_ENABLE_CLASSIFIER", raising=False)
    with pytest.raises(service.AIServiceError) as caught:
        service.classify_ai(image_bytes(), "photo.png")
    assert caught.value.code == "classifier_disabled"
    with pytest.raises(service.AIServiceError) as caught:
        service.classify_ai(image_bytes(), "scan.png", domain="scan")
    assert caught.value.code == "unsupported_domain"
    assert caught.value.status_code == 422


def test_classifier_never_resolves_uncached_weights(service, monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_API_ENABLE_CLASSIFIER", "true")
    monkeypatch.setenv("RAIW_CLASSIFY_WEIGHTS", str(tmp_path))
    with pytest.raises(service.AIServiceError) as caught:
        service.classify_ai(image_bytes(), "photo.png")
    assert caught.value.status_code == 503
    assert caught.value.code in (
        "classifier_weights_unavailable",
        "ai_dependencies_unavailable",
    )
    assert not list(tmp_path.iterdir())


def test_weight_files_alone_are_not_ready_for_offline_classification(
    service, monkeypatch, tmp_path
):
    monkeypatch.setenv("IMAGE_API_ENABLE_CLASSIFIER", "true")
    monkeypatch.setenv("RAIW_CLASSIFY_WEIGHTS", str(tmp_path))
    for name in (
        "clip-l-ft.pt",
        "probe-weights-clip-l-ft.npz",
        "detector.pt",
        "provider.pt",
    ):
        (tmp_path / name).write_bytes(b"cached")
    result = service.capabilities()["classification"]
    assert result["weights_ready"] is False
    assert "config.json" in result["missing_weights"]
    assert "preprocessor_config.json" in result["missing_weights"]


def test_classifier_uses_local_clip_configuration_and_cpu(
    service, monkeypatch, tmp_path, cpu_dependencies
):
    from remove_ai_watermarks.classify import PixelClassification

    monkeypatch.setenv("IMAGE_API_ENABLE_CLASSIFIER", "true")
    monkeypatch.setenv("RAIW_CLASSIFY_WEIGHTS", str(tmp_path))
    for name in (
        "clip-l-ft.pt",
        "probe-weights-clip-l-ft.npz",
        "detector.pt",
        "provider.pt",
        "config.json",
        "preprocessor_config.json",
    ):
        (tmp_path / name).write_bytes(b"cached")
    original_available = service._module_available
    monkeypatch.setattr(
        service,
        "_module_available",
        lambda name: name in {"torch", "transformers", "huggingface_hub"}
        or original_available(name),
    )
    local_clip = types.SimpleNamespace(BACKBONE="openai/clip-vit-large-patch14")
    original = image_bytes()

    # The unavailable heavyweight model operation is the only double. Readiness,
    # temp-file handling and the real upstream public classification type stay real.
    def classify(path, *, device, backend):
        assert device == "cpu"
        assert backend == "torch"
        assert local_clip.BACKBONE == str(tmp_path)
        assert path.read_bytes() == original
        return PixelClassification("unknown", "photo", "possibly", None)

    upstream_import = service._import

    def importing(name):
        if name == "remove_ai_watermarks._internal.clip_l_ft":
            return local_clip
        if name == "remove_ai_watermarks.classify":
            return types.SimpleNamespace(classify_pixels=classify)
        return upstream_import(name)

    monkeypatch.setattr(service, "_import", importing)
    result = service.classify_ai(original, "photo.png")
    assert result["label"] == "unknown"
    assert result["provider"] is None
    assert result["device"] == "cpu"
    json.dumps(result, allow_nan=False)


def test_capabilities_are_cheap_and_report_partial_weights(
    service, monkeypatch, tmp_path
):
    monkeypatch.setenv("IMAGE_API_ENABLE_CLASSIFIER", "true")
    monkeypatch.setenv("RAIW_CLASSIFY_WEIGHTS", str(tmp_path))
    (tmp_path / "clip-l-ft.pt").write_bytes(b"cached")
    before = set(sys.modules)
    result = service.capabilities()
    assert result["classification"]["weights_ready"] is False
    assert result["classification"]["ready"] is False
    assert "detector.pt" in result["classification"]["missing_weights"]
    assert not (set(sys.modules) - before) & {"torch", "transformers", "cv2", "numpy"}
    assert result["invisible"]["general_removal"] is False
    assert result["invisible"]["synthid_detection"] is False
    json.dumps(result, allow_nan=False)


def test_capabilities_do_not_load_cpu_or_model_runtimes_in_a_fresh_process(service):
    code = (
        "import sys; from app.ai_service import capabilities; "
        "capabilities(); assert not set(sys.modules) & "
        "{'torch','transformers','cv2','numpy'}"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr


def test_surviving_exif_metadata_is_reported_as_partial(
    service, cpu_dependencies, monkeypatch
):
    from remove_ai_watermarks import metadata

    image = Image.new("RGB", (96, 64), (43, 91, 151))
    exif = Image.Exif()
    exif[305] = "ComfyUI"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)

    # The actual upstream verification scanner misses this EXIF signal. Simulate
    # its documented fail-safe unchanged copy so our independent re-scan matters.
    def failed_strip(source, output, *, keep_standard):
        output.write_bytes(source.read_bytes())
        return output, {}

    monkeypatch.setattr(metadata, "strip_and_verify", failed_strip)
    result = service.clean_ai(buffer.getvalue(), "original.jpg", visible=False)
    assert result["metadata"]["status"] == "partial"
    assert result["metadata"]["remaining"]["exif_generator"] == "ComfyUI"
    assert result["metadata"]["removed_fields"] == []


def test_optional_dependency_failure_is_actionable(service, monkeypatch):
    original = service.importlib.import_module

    def unavailable(name, *args, **kwargs):
        if name.startswith("remove_ai_watermarks"):
            raise ModuleNotFoundError(
                "optional library absent", name="remove_ai_watermarks"
            )
        return original(name, *args, **kwargs)

    monkeypatch.setattr(service.importlib, "import_module", unavailable)
    with pytest.raises(service.AIServiceError) as caught:
        service.inspect_ai(image_bytes(), "photo.png")
    assert caught.value.code == "ai_dependencies_unavailable"
    assert caught.value.status_code == 503


def test_inspection_never_invokes_model_based_trustmark(
    service, cpu_dependencies, monkeypatch
):
    from remove_ai_watermarks import identify

    def model_download_forbidden(*args, **kwargs):
        pytest.fail("TrustMark must not run or download models")

    monkeypatch.setattr(identify, "_trustmark", model_download_forbidden)
    result = service.inspect_ai(image_bytes(), "ordinary.png")
    assert result["is_ai_generated"] is None
    assert result["invisible"]["status"] == "checked"
