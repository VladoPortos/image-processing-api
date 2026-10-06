import base64
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from conftest import image_bytes


def convert(client, data, filename="photo.png", format="jpg"):
    return client.post(
        "/convert", files={"image": (filename, data)}, data={"format": format}
    )


def test_exif_orientation_applied_before_resize(client):
    source = Image.new("RGB", (120, 80), "red")
    exif = Image.Exif()
    exif[274] = 6
    response = client.post(
        "/resize",
        files={"image": ("camera.jpg", image_bytes(source, "JPEG", exif=exif))},
        data={"width": 40, "format": "jpg"},
    )
    assert response.status_code == 200
    result = Image.open(BytesIO(response.content))
    assert result.size == (40, 60)
    assert result.getexif().get(274) is None


@pytest.mark.parametrize(
    "mode,transparent", [("P", False), ("P", True), ("LA", True), ("RGBA", True)]
)
def test_every_palette_and_alpha_mode_converts_to_jpeg(client, mode, transparent):
    source = Image.new(mode, (24, 24))
    if mode == "P":
        source.putpalette([0, 0, 0] + [255, 0, 0] * 255)
        if transparent:
            source.info["transparency"] = 0
    response = convert(client, image_bytes(source))
    assert response.status_code == 200
    result = Image.open(BytesIO(response.content))
    assert result.mode == "RGB"
    if transparent:
        assert min(result.getpixel((12, 12))) > 250
    assert response.headers["content-type"] == "image/jpeg"


def test_unicode_filename_has_downloadable_header(client, png):
    response = convert(client, png, "fotografia-č.png")
    assert response.status_code == 200
    assert (
        "filename*=UTF-8''fotografia-%C4%8D.jpg"
        in response.headers["content-disposition"]
    )


@pytest.mark.parametrize(
    "endpoint,fields",
    [
        ("/resize", {"width": 0}),
        ("/resize", {"percentage": 0.001}),
        ("/crop", {"left": 0, "top": 0, "right": 130, "bottom": 80}),
    ],
)
def test_invalid_geometry_has_structured_caller_error(client, png, endpoint, fields):
    response = client.post(
        endpoint, files={"image": ("photo.png", png)}, data={"format": "png", **fields}
    )
    assert 400 <= response.status_code < 500
    assert response.json()["error"]["code"]


def test_non_image_is_caller_error(client):
    response = convert(client, b"plain text", "note.txt")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image"


def test_openapi_describes_binary_and_base64_body(client):
    schema = client.get("/openapi.json").json()
    convert_schema = schema["paths"]["/convert"]["post"]
    assert (
        convert_schema["responses"]["200"]["content"]["image/jpeg"]["schema"]["format"]
        == "binary"
    )
    base64_schema = schema["paths"]["/convert/base64"]["post"]
    assert base64_schema["requestBody"]["content"]["application/json"]["schema"]


def test_metadata_keeps_dpi_and_inspection_reports_animation(client):
    source = image_bytes(Image.new("RGB", (20, 10)), dpi=(300, 300))
    metadata = client.post("/metadata", files={"image": ("dpi.png", source)}).json()
    assert abs(metadata["info"]["dpi"][0] - 300) < 1
    stream = BytesIO()
    Image.new("RGB", (20, 10), "red").save(
        stream,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (20, 10), "blue")],
        duration=100,
        loop=0,
    )
    response = client.post(
        "/inspect",
        json={"input": {"image_base64": base64.b64encode(stream.getvalue()).decode()}},
    )
    assert response.status_code == 200
    assert response.json()["frames"] == 2
    assert "first_frame_only" in response.json()["warnings"]


def test_icc_preserved_and_metadata_can_be_stripped(client):
    source = image_bytes(
        Image.new("RGB", (20, 10)), "JPEG", icc_profile=b"test-profile"
    )
    assert (
        Image.open(BytesIO(convert(client, source).content)).info["icc_profile"]
        == b"test-profile"
    )
    request = {
        "input": {"image_base64": base64.b64encode(source).decode()},
        "output": {"format": "jpeg", "metadata": "strip"},
    }
    processed = client.post("/process", json=request)
    assert processed.status_code == 200
    data = client.get(processed.json()["artifact"]["download_url"]).content
    assert "icc_profile" not in Image.open(BytesIO(data)).info


def test_json_form_and_plain_base64_remain_supported(client, png):
    encoded = base64.b64encode(png).decode()
    responses = [
        client.post("/convert/base64", json={"image_base64": encoded, "format": "png"}),
        client.post("/convert/base64", data={"image_base64": encoded, "format": "png"}),
        client.post(
            "/convert/base64?format=png",
            content=encoded,
            headers={"content-type": "text/plain"},
        ),
    ]
    assert all(r.status_code == 200 for r in responses)
    assert all(Image.open(BytesIO(r.content)).size == (120, 80) for r in responses)


def test_legacy_stretch_accepts_a_single_dimension(client, png):
    response = client.post(
        "/resize",
        files={"image": ("photo.png", png)},
        data={"width": 30, "maintain_aspect_ratio": "false", "format": "png"},
    )
    assert response.status_code == 200
    assert Image.open(BytesIO(response.content)).size == (30, 80)


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_png_color_key_transparency_flattens_white(client, mode):
    source = Image.new(mode, (20, 20), 0)
    source.info["transparency"] = 0 if mode == "L" else (0, 0, 0)
    response = convert(client, image_bytes(source))
    assert response.status_code == 200
    assert min(Image.open(BytesIO(response.content)).getpixel((10, 10))) > 250


def test_output_background_is_validated_even_for_opaque_images(client, png):
    response = client.post(
        "/process",
        json={
            "input": {"image_base64": base64.b64encode(png).decode()},
            "output": {"format": "jpeg", "background": "not-a-color"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_legacy_resize_still_requires_sizing_parameters(client, png):
    response = client.post(
        "/resize",
        files={"image": ("photo.png", png)},
        data={"maintain_aspect_ratio": "false", "format": "png"},
    )
    assert 400 <= response.status_code < 500


def test_legacy_conversion_accepts_uppercase_format(client, png):
    response = convert(client, png, format="PNG")
    assert response.status_code == 200


def test_artifact_descriptor_reports_the_encoded_mode(client):
    source = image_bytes(Image.new("L", (20, 10), 100))
    response = client.post(
        "/process",
        json={
            "input": {"image_base64": base64.b64encode(source).decode()},
            "output": {"format": "webp"},
        },
    )
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    encoded = Image.open(BytesIO(client.get(artifact["download_url"]).content))
    assert artifact["mode"] == encoded.mode


def test_xmp_orientation_is_sanitized_after_transposing_pixels(client):
    info = PngImagePlugin.PngInfo()
    info.add_itxt(
        "XML:com.adobe.xmp",
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6" /></rdf:RDF></x:xmpmeta>',
    )
    source = image_bytes(Image.new("RGB", (120, 80), "red"), pnginfo=info)
    response = convert(client, source, format="png")
    assert response.status_code == 200
    output = Image.open(BytesIO(response.content))
    assert output.size == (80, 120)
    assert output.getexif().get(274) is None


def test_nested_exif_dimensions_match_resized_pixels(client):
    exif = Image.Exif()
    exif[34665] = {40962: 120, 40963: 80}
    source = image_bytes(Image.new("RGB", (120, 80)), "JPEG", exif=exif)
    response = client.post(
        "/resize",
        files={"image": ("photo.jpg", source)},
        data={"width": 30, "format": "jpg"},
    )
    assert response.status_code == 200
    output = Image.open(BytesIO(response.content))
    assert output.getexif().get_ifd(34665)[40962] == 30
    assert output.getexif().get_ifd(34665)[40963] == 20


def test_metadata_extracts_nested_camera_and_gps_fields(client):
    exif = Image.Exif()
    exif[271] = "Camera maker"
    exif[34665] = {36867: "2026:10:06 12:30:00", 33434: 0.005, 37385: 16}
    exif[34853] = {1: "N", 2: (48.0, 8.0, 20.0)}
    source = image_bytes(Image.new("RGB", (120, 80)), "JPEG", exif=exif)
    response = client.post("/metadata", files={"image": ("camera.jpg", source)})
    assert response.status_code == 200
    fields = response.json()["exif"]
    assert fields["DateTimeOriginal"] == "2026:10:06 12:30:00"
    assert fields["ExposureTime"] == pytest.approx(0.005)
    assert fields["Flash"] == 16
    assert fields["GPSInfo"]["GPSLatitudeRef"] == "N"
    assert fields["GPSInfo"]["GPSLatitude"] == [48, 8, 20]
