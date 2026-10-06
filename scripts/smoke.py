"""Exercise a running API using generated images; no external service calls."""

import argparse
import base64
import io
import json
import time
import urllib.error
import urllib.request

from PIL import Image, PngImagePlugin

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass


def encoded(image, format="PNG", **kwargs):
    output = io.BytesIO()
    image.save(output, format, **kwargs)
    return output.getvalue()


class Probe:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.observations = []

    def request(self, path, payload=None, expected=200, raw=None, headers=None):
        if payload is not None:
            raw = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        started = time.perf_counter()
        request = urllib.request.Request(
            self.url + path, data=raw, headers=headers or {}
        )
        try:
            response = urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            status = response.status
            mime = response.headers.get("Content-Type", "")
        assert status == expected, (path, status, body[:400])
        self.observations.append(
            {
                "path": path,
                "status": status,
                "seconds": round(time.perf_counter() - started, 4),
            }
        )
        return json.loads(body) if mime.startswith("application/json") else body

    def upload(self, path, image, fields=None, filename="fixture-č.png", expected=200):
        boundary = "local-image-api-smoke"
        chunks = []
        for key, value in (fields or {}).items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        chunks.extend([image, f"\r\n--{boundary}--\r\n".encode()])
        return self.request(
            path,
            expected=expected,
            raw=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8078")
    parser.add_argument("--report", help="Optional JSON report path")
    args = parser.parse_args()
    probe = Probe(args.url)
    assert probe.request("/health")["status"] == "healthy"
    capabilities = probe.request("/capabilities")
    schema = probe.request("/openapi.json")
    assert (
        schema["paths"]["/convert"]["post"]["responses"]["200"]["content"][
            "image/jpeg"
        ]["schema"]["format"]
        == "binary"
    )
    source = encoded(Image.new("RGBA", (120, 80), (42, 91, 153, 117)))
    original = probe.upload("/images", source)
    reference = {"artifact_id": original["id"]}
    assert probe.request(original["download_url"]) == source
    inspection = probe.request("/inspect", {"input": reference})
    assert (inspection["width"], inspection["height"], inspection["has_alpha"]) == (
        120,
        80,
        True,
    )
    assert (
        probe.request("/inspect", {"input": {"path": original["path"]}})["width"] == 120
    )
    for format in ("png", "jpg", "jpeg", "webp", "avif"):
        data = probe.upload("/convert", source, {"format": format})
        with Image.open(io.BytesIO(data)) as result:
            assert result.size == (120, 80)
            if format in ("jpg", "jpeg"):
                assert result.mode == "RGB"
    exif = Image.Exif()
    exif[274] = 6
    camera = encoded(Image.new("RGB", (120, 80), "red"), "JPEG", exif=exif)
    resized = probe.upload("/resize", camera, {"width": 40, "format": "jpg"})
    with Image.open(io.BytesIO(resized)) as result:
        assert result.size == (40, 60)
        assert result.getexif().get(274) is None
    palette = Image.new("P", (20, 20))
    palette.putpalette([0, 0, 0] + [255, 0, 0] * 255)
    palette.info["transparency"] = 0
    with Image.open(
        io.BytesIO(probe.upload("/convert", encoded(palette), {"format": "jpg"}))
    ) as result:
        assert min(result.getpixel((10, 10))) > 250
    assert (
        probe.upload(
            "/crop",
            source,
            {"left": 0, "top": 0, "right": 999, "bottom": 10, "format": "png"},
            expected=400,
        )["error"]["code"]
        == "invalid_crop"
    )
    assert (
        probe.upload("/convert", b"not an image", {"format": "png"}, expected=400)[
            "error"
        ]["code"]
        == "invalid_image"
    )
    result = probe.request(
        "/process",
        {
            "input": reference,
            "operations": [
                {
                    "op": "crop",
                    "left": 0.25,
                    "top": 0.25,
                    "right": 0.75,
                    "bottom": 0.75,
                    "units": "normalized",
                },
                {"op": "resize", "width": 30},
            ],
            "output": {"format": "png"},
        },
    )
    assert (result["artifact"]["width"], result["artifact"]["height"]) == (30, 20)
    assert result["coordinate_transform"] == [[2, 0, 30], [0, 2, 20], [0, 0, 1]]
    with Image.open(
        io.BytesIO(
            probe.request(result["artifact"]["download_url"] + "/preview?max_edge=16")
        )
    ) as preview:
        assert max(preview.size) == 16
    variants = probe.request(
        "/variants",
        {
            "input": reference,
            "variants": [
                {"name": "tiny", "operations": [{"op": "resize", "width": 30}]},
                {
                    "name": "bad",
                    "operations": [
                        {"op": "crop", "left": 0, "top": 0, "right": 999, "bottom": 10}
                    ],
                },
            ],
        },
    )
    assert [item["status"] for item in variants["results"]] == ["success", "error"]
    batch = probe.request(
        "/batch", {"jobs": [{"input": {"path": "missing.png"}}, {"input": reference}]}
    )
    assert [item["status"] for item in batch["results"]] == ["error", "success"]
    sheet = probe.request(
        "/contact-sheet",
        {
            "images": [
                {"input": reference, "label": "original"},
                {"input": {"artifact_id": result["artifact"]["id"]}, "label": "crop"},
            ],
            "columns": 2,
        },
    )
    assert [item["label"] for item in sheet["items"]] == ["original", "crop"]
    assert sheet["artifact"]["width"] == 512
    vision = probe.request(
        "/prepare-vision",
        {
            "input": reference,
            "max_edge": 60,
            "max_pixels": 2000,
            "max_bytes": 3000,
            "output": {"format": "webp"},
        },
    )
    assert vision["budget_met"] is True
    assert vision["artifact"]["width"] * vision["artifact"]["height"] <= 2000
    assert vision["artifact"]["size_bytes"] <= 3000
    comparison = probe.request(
        "/compare", {"input": reference, "formats": ["png", "webp"]}
    )
    assert isinstance(
        comparison["formats"]["webp"]["savings"]["percentage_value"], (float, int)
    )
    ai_exercised = capabilities["ai"]["library"]["installed"]
    if ai_exercised:
        unknown = probe.request(
            "/ai/inspect",
            {"input": reference, "check_visible": False, "check_invisible": False},
        )
        assert unknown["is_ai_generated"] is None
        info = PngImagePlugin.PngInfo()
        info.add_text("parameters", "A forest, Steps: 20, Sampler: Euler, Seed: 123")
        ai_source = probe.upload(
            "/images",
            encoded(Image.new("RGBA", (120, 80), (42, 91, 153, 117)), pnginfo=info),
        )
        ai_reference = {"artifact_id": ai_source["id"]}
        evidence = probe.request(
            "/ai/inspect",
            {"input": ai_reference, "check_visible": False, "check_invisible": False},
        )
        assert evidence["is_ai_generated"] is True
        cleaned = probe.request("/ai/clean", {"input": ai_reference, "visible": False})
        assert cleaned["metadata"]["status"] == "removed"
        assert cleaned["invisible"]["status"] == "not_processed"
        after = probe.request(
            "/ai/inspect",
            {
                "input": {"artifact_id": cleaned["artifact"]["id"]},
                "check_visible": False,
                "check_invisible": False,
            },
        )
        assert after["is_ai_generated"] is None
        with Image.open(
            io.BytesIO(probe.request(cleaned["artifact"]["download_url"]))
        ) as pixels:
            assert pixels.getpixel((10, 10)) == (42, 91, 153, 117)
        if not capabilities["ai"]["classification"]["enabled"]:
            assert (
                probe.request("/ai/classify", {"input": reference}, expected=503)[
                    "error"
                ]["code"]
                == "classifier_disabled"
            )
        if capabilities["ai"]["visible"]["available"]:
            import numpy as np
            from remove_ai_watermarks.gemini_engine import GeminiEngine

            yy, xx = np.mgrid[0:512, 0:512]
            gradient = 130 + 17 * np.sin(xx / 38) + 13 * np.cos(yy / 51)
            rgb = np.stack((gradient * 1.03, gradient, gradient * 0.97), axis=-1)
            alpha = GeminiEngine().get_interpolated_alpha(48)[..., None]
            rgb[432:480, 432:480] = alpha * 255 + (1 - alpha) * rgb[432:480, 432:480]
            pixels = np.clip(rgb, 0, 255).astype("uint8")
            marked = probe.upload("/images", encoded(Image.fromarray(pixels)))
            cleaned = probe.request(
                "/ai/clean", {"input": {"artifact_id": marked["id"]}, "metadata": False}
            )
            assert cleaned["visible"]["status"] in ("cleaned", "partial", "unvalidated")
            assert any(mark["key"] == "gemini" for mark in cleaned["visible"]["marks"])
            with Image.open(
                io.BytesIO(probe.request(cleaned["artifact"]["download_url"]))
            ) as result:
                after_pixels = np.asarray(result.convert("RGB"))
            outside = np.ones((512, 512), dtype=bool)
            for mark in cleaned["visible"]["marks"]:
                x, y, width, height = mark["mask_bbox"]
                outside[y : y + height, x : x + width] = False
            np.testing.assert_array_equal(after_pixels[outside], pixels[outside])
            assert np.any(after_pixels != pixels)
    report = {
        "url": args.url,
        "requests": len(probe.observations),
        "ai_exercised": ai_exercised,
        "observations": probe.observations,
    }
    if args.report:
        from pathlib import Path

        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "passed",
                "requests": report["requests"],
                "ai_exercised": ai_exercised,
            }
        )
    )


if __name__ == "__main__":
    main()
