import base64
import importlib
from io import BytesIO

import pytest
from PIL import Image
from pydantic import TypeAdapter, ValidationError


def operations_module():
    try:
        return importlib.import_module("app.operations")
    except ModuleNotFoundError as exc:
        if exc.name == "app.operations":
            pytest.fail("Ordered image operations have not been implemented")
        raise


def apply(image, operations):
    return operations_module().apply_operations(image, operations)


def row_image():
    image = Image.new("RGB", (4, 2))
    image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)] * 2)
    return image


def encoded_png(image):
    output = BytesIO()
    image.save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def test_no_operations_returns_independent_image_without_logs():
    original = row_image()
    result, logs, warnings = apply(original, [])
    result.putpixel((0, 0), (0, 0, 0))
    assert original.getpixel((0, 0)) == (255, 0, 0)
    assert logs == []
    assert warnings == []


def test_resize_width_preserves_aspect_and_reports_output_to_input_scale():
    result, logs, warnings = apply(row_image(), [{"op": "resize", "width": 2}])
    assert result.size == (2, 1)
    assert logs[0]["input_size"] == [4, 2]
    assert logs[0]["output_size"] == [2, 1]
    assert logs[0]["coordinate_transform"] == [
        [2.0, 0.0, 0.0],
        [0.0, 2.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    assert warnings == []


@pytest.mark.parametrize(
    "fit,size",
    [("contain", (4, 2)), ("stretch", (4, 4)), ("cover", (4, 4)), ("pad", (4, 4))],
)
def test_resize_fit_modes_produce_expected_geometry(fit, size):
    result, _, _ = apply(
        row_image(),
        [{"op": "resize", "width": 4, "height": 4, "fit": fit, "allow_upscale": True}],
    )
    assert result.size == size


def test_cover_anchor_selects_correct_source_region_and_reports_crop_scale():
    result, logs, _ = apply(
        row_image(),
        [{"op": "resize", "width": 2, "height": 2, "fit": "cover", "anchor": "right"}],
    )
    assert result.getpixel((0, 0)) == (0, 0, 255)
    assert logs[0]["coordinate_transform"] == [
        [1.0, 0.0, 2.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]


def test_resize_pad_background_and_anchor_preserve_source_alpha():
    original = Image.new("RGBA", (2, 1), (255, 0, 0, 128))
    result, logs, _ = apply(
        original,
        [
            {
                "op": "resize",
                "width": 2,
                "height": 3,
                "fit": "pad",
                "anchor": "bottom",
                "background": "#0000ff",
            }
        ],
    )
    assert result.getpixel((0, 0)) == (0, 0, 255, 255)
    assert result.getpixel((0, 2)) == (128, 0, 127, 255)
    assert logs[0]["coordinate_transform"] == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, -2.0],
        [0.0, 0.0, 1.0],
    ]


@pytest.mark.parametrize("fit", ["contain", "cover", "stretch", "pad"])
def test_resize_disallows_upscaling_source_pixels(fit):
    result, logs, warnings = apply(
        Image.new("RGB", (4, 2), "red"),
        [{"op": "resize", "width": 8, "height": 8, "fit": fit, "allow_upscale": False}],
    )
    expected_size = (8, 8) if fit == "pad" else (4, 2)
    assert result.size == expected_size
    transform = logs[0]["coordinate_transform"]
    assert transform[0][0] >= 1
    assert transform[1][1] >= 1
    assert warnings


def test_tiny_percentage_reports_zero_rounding_as_caller_error():
    module = operations_module()
    with pytest.raises(module.OperationError) as caught:
        apply(row_image(), [{"op": "resize", "percentage": 0.01}])
    assert caught.value.code == "invalid_dimensions"
    assert caught.value.field.endswith("percentage")


def test_resize_default_prevents_upscaling():
    result, _, warnings = apply(row_image(), [{"op": "resize", "width": 8}])
    assert result.size == (4, 2)
    assert warnings


def test_normalized_crop_uses_exclusive_pixel_edges_and_reports_offsets():
    result, logs, _ = apply(
        row_image(),
        [
            {
                "op": "crop",
                "left": 0.25,
                "top": 0,
                "right": 0.75,
                "bottom": 1,
                "units": "normalized",
            }
        ],
    )
    assert result.size == (2, 2)
    assert result.getpixel((0, 0)) == (0, 255, 0)
    assert result.getpixel((1, 0)) == (0, 0, 255)
    assert logs[0]["crop_box"] == [1, 0, 3, 2]
    assert logs[0]["coordinate_transform"] == [
        [1.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]


def test_operations_apply_sequentially_against_preceding_image():
    result, logs, _ = apply(
        row_image(),
        [
            {"op": "crop", "left": 1, "top": 0, "right": 4, "bottom": 2},
            {"op": "crop", "left": 1, "top": 0, "right": 3, "bottom": 2},
        ],
    )
    assert result.getpixel((0, 0)) == (0, 0, 255)
    assert [log["input_size"] for log in logs] == [[4, 2], [3, 2]]


@pytest.mark.parametrize(
    "operation,field",
    [
        ({"op": "resize", "width": 0}, "width"),
        ({"op": "resize", "width": -1}, "width"),
        ({"op": "resize", "percentage": 0}, "percentage"),
        ({"op": "resize", "width": 2, "percentage": 50}, None),
        ({"op": "resize"}, None),
        ({"op": "resize", "width": 2, "unexpected": True}, "unexpected"),
        ({"op": "resize", "width": 2, "background": "bad-color"}, "background"),
        ({"op": "crop", "left": 0, "top": 0, "right": 5, "bottom": 2}, None),
        ({"op": "crop", "left": 2, "top": 0, "right": 1, "bottom": 2}, None),
        ({"op": "crop", "left": 0.5, "top": 0, "right": 2, "bottom": 2}, None),
        (
            {
                "op": "crop",
                "left": 0,
                "top": 0,
                "right": 1.1,
                "bottom": 1,
                "units": "normalized",
            },
            None,
        ),
        ({"op": "rotate", "angle": float("nan")}, "angle"),
        ({"op": "pad", "border": 1, "left": 2}, None),
        ({"op": "flatten", "background": "#ffffff00"}, "background"),
        ({"op": "watermark"}, None),
        ({"op": "watermark", "text": "x", "opacity": 2}, "opacity"),
        ({"op": "watermark", "logo_base64": "not-an-image"}, "logo_base64"),
        ({"op": "unknown"}, None),
    ],
)
def test_invalid_operation_is_a_structured_caller_error(operation, field):
    module = operations_module()
    with pytest.raises(module.OperationError) as caught:
        apply(row_image(), [operation])
    assert caught.value.code
    assert caught.value.details is not None
    if field:
        assert caught.value.field.endswith(field)


def test_typed_operations_share_discriminated_api_schema_and_forbid_extras():
    module = operations_module()
    adapter = TypeAdapter(module.Operation)
    operation = adapter.validate_python({"op": "resize", "width": 2})
    result, _, _ = apply(row_image(), [operation])
    assert result.size == (2, 1)
    assert adapter.json_schema()["discriminator"]["propertyName"] == "op"
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {"op": "flip", "direction": "horizontal", "extra": True}
        )


@pytest.mark.parametrize(
    "direction,pixel,matrix",
    [
        (
            "horizontal",
            (255, 255, 0),
            [[-1.0, 0.0, 4.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        ),
        ("vertical", (255, 0, 0), [[1.0, 0.0, 0.0], [0.0, -1.0, 2.0], [0.0, 0.0, 1.0]]),
    ],
)
def test_flip_preserves_pixels_and_reports_edge_coordinate_transform(
    direction, pixel, matrix
):
    result, logs, _ = apply(row_image(), [{"op": "flip", "direction": direction}])
    assert result.getpixel((0, 0)) == pixel
    assert logs[0]["coordinate_transform"] == matrix


def test_quarter_turn_expands_canvas_and_maps_output_edges_to_input():
    result, logs, _ = apply(row_image(), [{"op": "rotate", "angle": 90}])
    assert result.size == (2, 4)
    assert result.getpixel((0, 0)) == (255, 255, 0)
    assert logs[0]["coordinate_transform"] == [
        [0.0, -1.0, 4.0],
        [1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
    ]


def test_arbitrary_rotation_has_correct_center_transform_and_transparent_background():
    result, logs, _ = apply(
        Image.new("RGB", (4, 2), "red"), [{"op": "rotate", "angle": 45, "expand": True}]
    )
    assert result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0
    transform = logs[0]["coordinate_transform"]
    x, y = result.width / 2, result.height / 2
    assert transform[0][0] * x + transform[0][1] * y + transform[0][2] == pytest.approx(
        2
    )
    assert transform[1][0] * x + transform[1][1] * y + transform[1][2] == pytest.approx(
        1
    )


def test_pad_individual_sides_composites_alpha_once_and_records_translation():
    result, logs, _ = apply(
        Image.new("RGBA", (1, 1), (255, 0, 0, 128)),
        [{"op": "pad", "left": 2, "right": 1, "top": 1, "background": "blue"}],
    )
    assert result.size == (4, 2)
    assert result.getpixel((2, 1)) == (128, 0, 127, 255)
    assert logs[0]["coordinate_transform"] == [
        [1.0, 0.0, -2.0],
        [0.0, 1.0, -1.0],
        [0.0, 0.0, 1.0],
    ]


def test_trim_crops_visible_alpha_and_reports_offset():
    image = Image.new("RGBA", (5, 4))
    image.putpixel((2, 1), (255, 0, 0, 255))
    image.putpixel((3, 2), (0, 255, 0, 128))
    result, logs, _ = apply(image, [{"op": "trim"}])
    assert result.size == (2, 2)
    assert result.getpixel((0, 0)) == (255, 0, 0, 255)
    assert logs[0]["crop_box"] == [2, 1, 4, 3]


def test_trim_fully_transparent_image_retains_dimensions_and_warns():
    result, _, warnings = apply(Image.new("RGBA", (3, 4)), [{"op": "trim"}])
    assert result.size == (3, 4)
    assert warnings


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_flatten_handles_all_transparency_modes(mode):
    if mode == "P":
        image = Image.new("P", (1, 1), 0)
        image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        image.info["transparency"] = bytes([128] + [255] * 255)
        expected = (128, 0, 127)
    elif mode == "LA":
        image = Image.new("LA", (1, 1), (255, 128))
        expected = (128, 128, 255)
    else:
        image = Image.new("RGBA", (1, 1), (255, 0, 0, 128))
        expected = (128, 0, 127)
    result, _, _ = apply(image, [{"op": "flatten", "background": "blue"}])
    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == expected


def test_logo_watermark_applies_opacity_exactly_once():
    logo = encoded_png(Image.new("RGBA", (2, 2), (255, 255, 255, 255)))
    result, logs, _ = apply(
        Image.new("RGB", (4, 4), "black"),
        [
            {
                "op": "watermark",
                "logo_base64": logo,
                "position": "center",
                "angle": 0,
                "opacity": 0.5,
                "margin": 0,
            }
        ],
    )
    assert result.getpixel((1, 1)) == (128, 128, 128, 255)
    assert result.getpixel((0, 0)) == (0, 0, 0, 255)
    assert logs[0]["coordinate_transform"] == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]


def test_logo_watermark_preserves_original_alpha_before_global_opacity():
    logo = encoded_png(Image.new("RGBA", (1, 1), (255, 0, 0, 128)))
    result, _, _ = apply(
        Image.new("RGBA", (1, 1)),
        [
            {
                "op": "watermark",
                "logo_base64": logo,
                "position": "center",
                "angle": 0,
                "opacity": 0.5,
                "margin": 0,
            }
        ],
    )
    assert result.getpixel((0, 0)) == (255, 0, 0, 64)


def test_repeated_text_watermark_marks_multiple_regions_without_attenuating_alpha():
    result, _, _ = apply(
        Image.new("RGBA", (160, 100)),
        [
            {
                "op": "watermark",
                "text": "TEST",
                "font_size": 18,
                "angle": 0,
                "opacity": 0.5,
                "spacing": 10,
            }
        ],
    )
    alpha = result.getchannel("A")
    assert alpha.getextrema()[1] == 128
    assert alpha.crop((0, 0, 80, 50)).getbbox() is not None
    assert alpha.crop((80, 50, 160, 100)).getbbox() is not None


def test_unicode_text_watermark_renders_without_external_font_downloads():
    result, _, _ = apply(
        Image.new("RGB", (120, 50), "black"),
        [
            {
                "op": "watermark",
                "text": "Príliš žltý",
                "font_size": 16,
                "angle": 0,
                "position": "center",
                "opacity": 1,
            }
        ],
    )
    assert result.convert("RGB").getbbox() is not None
