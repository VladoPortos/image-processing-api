import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

from PIL import Image


def upload(client, png):
    response = client.post("/images", files={"image": ("source-č.png", png)})
    assert response.status_code == 200
    return response.json()


def test_artifacts_compose_operations_and_map_back_to_source(client, png):
    original = upload(client, png)
    response = client.post(
        "/process",
        json={
            "input": {"artifact_id": original["id"]},
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
    assert response.status_code == 200
    result = response.json()
    assert (result["artifact"]["width"], result["artifact"]["height"]) == (30, 20)
    assert result["coordinate_transform"] == [[2, 0, 30], [0, 2, 20], [0, 0, 1]]
    assert Image.open(
        BytesIO(client.get(result["artifact"]["download_url"]).content)
    ).size == (30, 20)
    assert client.get(original["download_url"]).content == png
    assert (
        client.post(
            "/inspect", json={"input": {"path": result["artifact"]["path"]}}
        ).json()["width"]
        == 30
    )


def test_batch_and_variants_preserve_order_and_individual_errors(client, png):
    original = upload(client, png)
    source = {"artifact_id": original["id"]}
    variants = client.post(
        "/variants",
        json={
            "input": source,
            "variants": [
                {"name": "thumb", "operations": [{"op": "resize", "width": 30}]},
                {
                    "name": "bad crop",
                    "operations": [
                        {"op": "crop", "left": 0, "top": 0, "right": 999, "bottom": 10}
                    ],
                },
            ],
        },
    ).json()["results"]
    assert [v["status"] for v in variants] == ["success", "error"]
    assert [v["name"] for v in variants] == ["thumb", "bad crop"]
    assert variants[1]["error"]["code"] == "invalid_crop"
    batch = client.post(
        "/batch",
        json={
            "jobs": [
                {"input": {"path": "missing.png"}},
                {"input": source, "output": {"format": "png"}},
            ]
        },
    ).json()["results"]
    assert [v["status"] for v in batch] == ["error", "success"]
    assert batch[0]["error"]["code"] == "input_not_found"


def test_contact_sheet_labels_map_to_originals_and_preview_is_small(client, png):
    original = upload(client, png)
    response = client.post(
        "/contact-sheet",
        json={
            "images": [
                {"input": {"artifact_id": original["id"]}, "label": "Choice A"},
                {"input": {"artifact_id": original["id"]}, "label": "Choice B"},
            ],
            "columns": 2,
            "cell_width": 128,
            "cell_height": 96,
        },
    )
    assert response.status_code == 200
    sheet = response.json()
    assert [i["label"] for i in sheet["items"]] == ["Choice A", "Choice B"]
    assert sheet["items"][1]["box"] == [128, 0, 256, 96]
    assert sheet["artifact"]["width"] == 256
    preview = client.get(sheet["artifact"]["download_url"] + "/preview?max_edge=64")
    assert preview.status_code == 200
    assert max(Image.open(BytesIO(preview.content)).size) == 64


def test_vision_preparation_meets_dimension_pixel_and_byte_budgets(client):
    noisy = Image.effect_noise((600, 400), 100).convert("RGB")
    stream = BytesIO()
    noisy.save(stream, "PNG")
    response = client.post(
        "/prepare-vision",
        json={
            "input": {"image_base64": base64.b64encode(stream.getvalue()).decode()},
            "max_edge": 300,
            "max_pixels": 40000,
            "max_bytes": 10000,
            "output": {"format": "jpeg", "quality": 85},
        },
    )
    assert response.status_code == 200
    result = response.json()
    artifact = result["artifact"]
    assert max(artifact["width"], artifact["height"]) <= 300
    assert artifact["width"] * artifact["height"] <= 40000
    assert artifact["size_bytes"] <= 10000
    assert result["budget_met"] is True
    impossible = client.post(
        "/prepare-vision",
        json={
            "input": {"artifact_id": artifact["id"]},
            "max_bytes": 1,
            "output": {"format": "png"},
        },
    ).json()
    assert impossible["budget_met"] is False
    assert "byte_budget_unmet" in impossible["warnings"]


def test_input_strategies_are_exclusive_and_schema_is_typed(client, png):
    response = client.post(
        "/process",
        json={
            "input": {"path": "foo.png", "image_base64": base64.b64encode(png).decode()}
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    schema = client.get("/openapi.json").json()
    process = schema["paths"]["/process"]["post"]
    assert process["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ProcessRequest")
    assert process["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ProcessResult")


def test_contact_sheet_base64_returns_reusable_original_reference(client, png):
    response = client.post(
        "/contact-sheet",
        json={
            "images": [
                {
                    "input": {
                        "image_base64": base64.b64encode(png).decode(),
                        "filename": "original.png",
                    }
                }
            ]
        },
    )
    assert response.status_code == 200
    reference = response.json()["items"][0]["input"]
    assert reference["image_base64"] is None
    assert reference["artifact_id"]
    assert client.get("/artifacts/" + reference["artifact_id"]).content == png


def test_pipeline_decodes_and_encodes_once(client, png, monkeypatch):
    from app import workflows

    calls = {"decode": 0, "encode": 0}
    original_decode, original_encode = workflows.decode_image, workflows.encode_image

    def decode(*args, **kwargs):
        calls["decode"] += 1
        return original_decode(*args, **kwargs)

    def encode(*args, **kwargs):
        calls["encode"] += 1
        return original_encode(*args, **kwargs)

    monkeypatch.setattr(workflows, "decode_image", decode)
    monkeypatch.setattr(workflows, "encode_image", encode)
    response = client.post(
        "/process",
        json={
            "input": {"image_base64": base64.b64encode(png).decode()},
            "operations": [
                {"op": "resize", "width": 40},
                {"op": "flip"},
                {"op": "watermark", "text": "Sample", "position": "center", "angle": 0},
            ],
            "output": {"format": "png"},
        },
    )
    assert response.status_code == 200
    assert calls == {"decode": 1, "encode": 1}


def test_health_remains_responsive_during_image_work(client, png, monkeypatch):
    from app import main

    original = getattr(main, "decode_image", None)
    assert original is not None, "API needs the shared worker core"
    entered, release = Event(), Event()

    def blocking_decode(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(main, "decode_image", blocking_decode)
    with ThreadPoolExecutor() as executor:
        job = executor.submit(
            client.post,
            "/convert",
            files={"image": ("input.png", png)},
            data={"format": "png"},
        )
        assert entered.wait(5)
        try:
            assert client.get("/health").status_code == 200
        finally:
            release.set()
        assert job.result(timeout=5).status_code == 200
