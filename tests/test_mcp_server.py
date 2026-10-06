"""MCP protocol tests against a real local HTTP boundary."""

import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
import httpx

pytest.importorskip(
    "mcp", reason="Install requirements-mcp.txt to run optional adapter tests"
)
from mcp.shared.memory import create_connected_server_and_client_session
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


def load_adapter():
    try:
        return importlib.import_module("mcp_server")
    except ModuleNotFoundError as exc:
        pytest.fail(f"The MCP adapter has not been implemented: {exc}")


def png(width=64, height=32):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def artifact(identifier="result-one", **updates):
    result = {
        "id": identifier,
        "filename": f"{identifier}.png",
        "path": f"/data/artifacts/{identifier}.png",
        "download_url": f"/artifacts/{identifier}",
        "mime_type": "image/png",
        "format": "png",
        "width": 64,
        "height": 32,
        "mode": "RGB",
        "has_alpha": False,
        "size_bytes": len(png()),
        "warnings": [],
    }
    result.update(updates)
    return result


@contextmanager
def http_api(routes):
    """Run fixture responses through real HTTP; wrong requests get an error."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond(None)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", 0))
            self.respond(json.loads(self.rfile.read(size)))

        def respond(self, body):
            seen.append((self.command, self.path, body))
            route = routes.get((self.command, self.path))
            if route is None:
                status, content_type, payload = (
                    404,
                    "application/json",
                    {"detail": "Wrong route"},
                )
            else:
                status, content_type, payload = route
                if callable(payload):
                    status, payload = payload(body)
            data = (
                json.dumps(payload).encode()
                if content_type == "application/json"
                else payload
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def call_tool(url, name, arguments):
    async def call():
        server = load_adapter().create_server(api_url=url)
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(name, arguments)

    return asyncio.run(call())


def test_sdk_discovery_exposes_nested_models_and_operation_choices():
    async def discover():
        server = load_adapter().create_server()
        async with create_connected_server_and_client_session(server) as session:
            return (await session.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(discover())}
    assert set(tools) == {
        "capabilities",
        "inspect_image",
        "process_image",
        "create_variants",
        "process_batch",
        "contact_sheet",
        "prepare_vision",
        "ai_inspect",
        "ai_classify",
        "ai_clean",
    }
    process = tools["process_image"]
    assert process.inputSchema["properties"]["input"]["$ref"]
    assert (
        process.inputSchema["properties"]["operations"]["items"]["discriminator"][
            "propertyName"
        ]
        == "op"
    )
    assert len(process.inputSchema["properties"]["operations"]["items"]["oneOf"]) >= 8
    assert "quality" in process.inputSchema["$defs"]["OutputOptions"]["properties"]
    for tool in tools.values():
        assert tool.outputSchema is not None
        assert tool.outputSchema.get("type") == "object"
    assert tools["contact_sheet"].inputSchema["properties"]["columns"]["minimum"] == 1
    assert (
        tools["contact_sheet"].inputSchema["properties"]["background"]["default"]
        == "#ffffff"
    )
    assert (
        tools["prepare_vision"].inputSchema["properties"]["allow_upscale"]["default"]
        is False
    )
    minimum_quality = tools["prepare_vision"].inputSchema["properties"]["min_quality"]
    assert minimum_quality["default"] == 35
    assert (minimum_quality["minimum"], minimum_quality["maximum"]) == (1, 100)


def test_process_returns_structured_artifact_link_and_bounded_image_preview():
    payload = {
        "artifact": artifact(),
        "operations": [],
        "warnings": [],
        "coordinate_transform": None,
    }
    request = {
        "input": {"path": "input/photo.png"},
        "operations": [],
        "output": {"format": "png", "quality": 85},
    }

    def process(body):
        if (
            body["input"] != request["input"]
            or body["operations"] != []
            or body["output"]["format"] != "png"
        ):
            return 400, {
                "error": {"code": "wrong_payload", "message": "Wrong process input"}
            }
        return 200, payload

    with http_api(
        {
            ("POST", "/process"): (200, "application/json", process),
            ("GET", "/artifacts/result-one/preview?max_edge=512"): (
                200,
                "image/png",
                png(),
            ),
        }
    ) as (url, _seen):
        result = call_tool(url, "process_image", request)
    assert result.isError is False
    assert result.structuredContent["artifact"]["id"] == "result-one"
    links = [content for content in result.content if content.type == "resource_link"]
    assert len(links) == 1
    assert str(links[0].uri) == f"{url}/artifacts/result-one"
    assert links[0].mimeType == "image/png"
    previews = [content for content in result.content if content.type == "image"]
    assert len(previews) == 1
    assert previews[0].mimeType == "image/png"
    with Image.open(BytesIO(base64.b64decode(previews[0].data))) as preview:
        assert preview.size == (64, 32)
    assert "image_base64" not in json.dumps(result.structuredContent)
    assert all(
        previews[0].data not in content.text
        for content in result.content
        if content.type == "text"
    )


def test_http_error_preserves_actionable_code_and_field():
    error = {
        "code": "invalid_crop",
        "message": "Crop exceeds image dimensions",
        "field": "operations.0",
        "details": {"width": 64},
    }
    with http_api(
        {
            ("POST", "/process"): (
                422,
                "application/json",
                {"detail": error["message"], "error": error},
            )
        }
    ) as (url, _seen):
        result = call_tool(
            url,
            "process_image",
            {
                "input": {"path": "bad.png"},
                "operations": [],
                "output": {"format": "png"},
            },
        )
    assert result.isError is True
    assert result.structuredContent["error"] == error
    assert any(
        "invalid_crop" in content.text
        for content in result.content
        if content.type == "text"
    )
    assert not any(
        content.type in {"image", "resource_link"} for content in result.content
    )


@pytest.mark.parametrize(
    "content_type,preview",
    [
        ("image/png", b"x" * 131073),
        ("text/html", b"not an image"),
        ("image/png", b"not an image"),
        ("image/png", png(513, 32)),
        ("image/jpeg", png()),
    ],
    ids=[
        "oversized-bytes",
        "wrong-mime",
        "invalid-image",
        "oversized-dimensions",
        "mismatched-mime",
    ],
)
def test_unusable_preview_does_not_hide_successful_artifact(content_type, preview):
    with http_api(
        {
            ("POST", "/process"): (
                200,
                "application/json",
                {
                    "artifact": artifact(),
                    "operations": [],
                    "warnings": [],
                    "coordinate_transform": None,
                },
            ),
            ("GET", "/artifacts/result-one/preview?max_edge=512"): (
                200,
                content_type,
                preview,
            ),
        }
    ) as (url, _seen):
        result = call_tool(
            url,
            "process_image",
            {"input": {"artifact_id": "original"}, "operations": []},
        )
    assert result.isError is False
    assert result.structuredContent["artifact"]["id"] == "result-one"
    assert any(content.type == "resource_link" for content in result.content)
    assert not any(content.type == "image" for content in result.content)


def test_non_json_upstream_failure_is_a_tool_error():
    with http_api({("POST", "/process"): (503, "text/plain", b"Unavailable")}) as (
        url,
        _seen,
    ):
        result = call_tool(
            url, "process_image", {"input": {"path": "photo.png"}, "operations": []}
        )
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "api_error"
    assert result.structuredContent["error"]["details"]["status_code"] == 503


def test_base64_image_strings_are_not_repeated_in_result_content():
    encoded = base64.b64encode(png()).decode()
    with http_api(
        {
            ("POST", "/process"): (
                200,
                "application/json",
                {
                    "artifact": artifact(),
                    "operations": [],
                    "warnings": [],
                    "coordinate_transform": None,
                    "image_base64": encoded,
                },
            )
        }
    ) as (url, _seen):
        result = call_tool(
            url, "process_image", {"input": {"image_base64": encoded}, "operations": []}
        )
    assert result.isError is False
    assert encoded not in json.dumps(result.structuredContent)
    assert all(
        encoded not in content.text
        for content in result.content
        if content.type == "text"
    )


INSPECTION = {
    "filename": "photo.png",
    "format": "png",
    "width": 64,
    "height": 32,
    "raw_width": 64,
    "raw_height": 32,
    "mode": "RGB",
    "has_alpha": False,
    "orientation": None,
    "frames": 1,
    "animated": False,
    "size_bytes": 100,
    "has_icc": False,
    "has_exif": False,
    "dpi": None,
    "warnings": [],
}
AI_INSPECTION = {
    "schema_version": 1,
    "is_ai_generated": None,
    "platform": None,
    "confidence": "none",
    "ai_source_kind": None,
    "ai_from_metadata": False,
    "watermarks": [],
    "signals": [],
    "caveats": ["Absent evidence leaves origin unknown"],
    "integrity_clashes": [],
    "c2pa_validation": None,
    "visible": {"status": "skipped"},
    "invisible": {"status": "skipped", "detected": None, "synthid": "unsupported"},
    "warnings": [],
}
CAPABILITIES = {
    "version": "0.2.0",
    "formats": ["png", "webp"],
    "operations": ["resize", "crop"],
    "data_root": "/data",
    "worker_limit": 2,
    "animation": "first_frame_only",
    "ai": {
        "library": {"installed": False, "version": None},
        "metadata": {"inspection": True, "removal": True},
        "visible": {"available": False, "backend": "cv2", "marks": []},
        "invisible": {
            "dwt_dct_detection": False,
            "synthid_detection": False,
            "general_removal": False,
        },
        "classification": {
            "enabled": False,
            "dependencies_ready": False,
            "weights_ready": False,
            "ready": False,
            "domain": "photo",
            "device": "cpu",
            "backend": "torch",
            "missing_weights": [],
        },
    },
}


@pytest.mark.parametrize(
    "tool,route,arguments,response",
    [
        ("inspect_image", "/inspect", {"input": {"path": "photo.png"}}, INSPECTION),
        (
            "create_variants",
            "/variants",
            {
                "input": {"artifact_id": "original"},
                "variants": [
                    {
                        "name": "thumb",
                        "operations": [{"op": "resize", "width": 64}],
                        "output": {"format": "png"},
                    },
                    {
                        "name": "invalid",
                        "operations": [
                            {
                                "op": "crop",
                                "left": 0,
                                "top": 0,
                                "right": 1000,
                                "bottom": 1000,
                            }
                        ],
                    },
                ],
            },
            {
                "results": [
                    {
                        "index": 0,
                        "name": "thumb",
                        "status": "success",
                        "result": {
                            "artifact": artifact(),
                            "operations": [],
                            "warnings": [],
                        },
                        "error": None,
                    },
                    {
                        "index": 1,
                        "name": "invalid",
                        "status": "error",
                        "result": None,
                        "error": {
                            "code": "invalid_crop",
                            "message": "Invalid crop",
                            "field": "operations.0",
                            "details": {"width": 64},
                        },
                    },
                ]
            },
        ),
        (
            "process_batch",
            "/batch",
            {"jobs": [{"input": {"artifact_id": "original"}, "operations": []}]},
            {
                "results": [
                    {
                        "index": 0,
                        "status": "success",
                        "result": {
                            "artifact": artifact(),
                            "operations": [],
                            "warnings": [],
                        },
                        "error": None,
                    }
                ]
            },
        ),
        (
            "contact_sheet",
            "/contact-sheet",
            {
                "images": [{"input": {"path": "photo.png"}, "label": "A"}],
                "columns": 1,
                "cell_width": 64,
                "cell_height": 64,
                "background": "#123456",
            },
            {
                "artifact": artifact(),
                "items": [
                    {
                        "index": 0,
                        "label": "A",
                        "input": {"path": "photo.png"},
                        "box": [0, 0, 64, 64],
                    }
                ],
                "warnings": [],
            },
        ),
        (
            "prepare_vision",
            "/prepare-vision",
            {
                "input": {"artifact_id": "original"},
                "max_edge": 64,
                "max_pixels": 2048,
                "max_bytes": 1000,
                "allow_upscale": False,
                "min_quality": 20,
                "output": {"format": "jpeg", "quality": 20},
            },
            {
                "artifact": artifact(),
                "operations": [],
                "warnings": [],
                "quality": 20,
                "budget_met": True,
            },
        ),
        (
            "ai_inspect",
            "/ai/inspect",
            {
                "input": {"path": "photo.png"},
                "check_visible": False,
                "check_invisible": False,
            },
            AI_INSPECTION,
        ),
        (
            "ai_classify",
            "/ai/classify",
            {"input": {"artifact_id": "original"}, "domain": "photo"},
            {
                "label": "unknown",
                "domain": "photo",
                "detector": "local-photo-detector",
                "provider": None,
                "device": "cpu",
                "backend": "torch",
                "warnings": ["Unbenchmarked classifier"],
            },
        ),
        (
            "ai_clean",
            "/ai/clean",
            {"input": {"path": "photo.png"}, "visible": False, "metadata": True},
            {
                "artifact": artifact(),
                "visible": {"status": "skipped"},
                "metadata": {
                    "status": "no_metadata",
                    "removed_fields": [],
                    "remaining": {},
                },
                "invisible": {
                    "status": "not_processed",
                    "reason": "general_image_removal_unsupported_on_cpu",
                },
                "original_inspection": AI_INSPECTION,
                "warnings": [],
            },
        ),
    ],
    ids=[
        "inspect",
        "variants",
        "batch",
        "sheet",
        "vision",
        "ai-inspect",
        "ai-classify",
        "ai-clean",
    ],
)
def test_tools_forward_their_http_contract_and_preserve_structured_results(
    tool, route, arguments, response
):
    def includes(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and includes(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(actual) == len(expected)
                and all(includes(a, e) for a, e in zip(actual, expected))
            )
        return actual == expected

    def endpoint(body):
        # Compare caller-controlled fields while allowing documented model defaults.
        if not includes(body, arguments):
            return 422, {
                "error": {"code": "wrong_payload", "message": "Wrong tool arguments"}
            }
        return 200, response

    with http_api({("POST", route): (200, "application/json", endpoint)}) as (
        url,
        _seen,
    ):
        result = call_tool(url, tool, arguments)
    assert result.isError is False
    assert result.structuredContent == response
    if tool == "create_variants":
        assert [item["status"] for item in result.structuredContent["results"]] == [
            "success",
            "error",
        ]
        assert [
            item.name for item in result.content if item.type == "resource_link"
        ] == ["result-one.png"]


def test_capabilities_uses_environment_url(monkeypatch):
    with http_api(
        {("GET", "/capabilities"): (200, "application/json", CAPABILITIES)}
    ) as (url, _seen):
        monkeypatch.setenv("IMAGE_API_URL", url)
        result = call_tool(None, "capabilities", {})
    assert result.isError is False
    assert result.structuredContent["ai"]["classification"]["ready"] is False
    assert result.structuredContent["formats"] == ["png", "webp"]


def test_transport_failure_is_reported_as_structured_tool_error():
    with http_api({}) as (url, _seen):
        pass
    result = call_tool(url, "capabilities", {})
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "api_unavailable"
    assert result.structuredContent["error"]["details"]["api_url"] == url


def test_invalid_input_reference_is_rejected_before_http():
    with http_api({("POST", "/inspect"): (200, "application/json", INSPECTION)}) as (
        url,
        seen,
    ):
        result = call_tool(
            url,
            "inspect_image",
            {"input": {"path": "photo.png", "artifact_id": "original"}},
        )
        assert seen == []
    assert result.isError is True
    assert any(
        "exactly one" in item.text for item in result.content if item.type == "text"
    )


def test_sdk_stdio_handshake_and_call_leave_stdout_for_protocol():
    async def run(url):
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "mcp_server.py")],
            env={"IMAGE_API_URL": url},
        )
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                discovered = await session.list_tools()
                result = await session.call_tool("capabilities", {})
                return discovered, result

    with http_api(
        {("GET", "/capabilities"): (200, "application/json", CAPABILITIES)}
    ) as (url, _seen):
        discovered, result = asyncio.run(run(url))
    assert len(discovered.tools) == 10
    assert result.isError is False
    assert result.structuredContent["worker_limit"] == 2


def test_discovered_capabilities_schema_describes_runtime_and_ai_availability():
    async def discover():
        async with create_connected_server_and_client_session(
            load_adapter().create_server()
        ) as session:
            return next(
                tool
                for tool in (await session.list_tools()).tools
                if tool.name == "capabilities"
            )

    tool = asyncio.run(discover())
    schema = tool.outputSchema["$defs"]["Capabilities"]
    assert {
        "version",
        "formats",
        "operations",
        "data_root",
        "worker_limit",
        "ai",
    } <= schema["properties"].keys()
    ai = tool.outputSchema["$defs"]["AICapabilities"]
    assert {"metadata", "visible", "invisible", "classification"} <= ai[
        "properties"
    ].keys()


def test_nested_image_and_logo_data_are_omitted_from_structured_error_details():
    encoded = "a" * 2048
    error = {
        "code": "bad_logo",
        "message": "Invalid logo",
        "details": {"operation": {"logo_base64": encoded}, "input": encoded},
    }
    with http_api(
        {
            ("POST", "/process"): (
                422,
                "application/json",
                {"detail": "Invalid logo", "error": error},
            )
        }
    ) as (url, _seen):
        result = call_tool(
            url, "process_image", {"input": {"path": "photo.png"}, "operations": []}
        )
    assert result.isError is True
    assert encoded not in json.dumps(result.structuredContent)
    assert all(
        encoded not in content.text
        for content in result.content
        if content.type == "text"
    )


def test_sdk_tools_integrate_with_real_api_and_reusable_artifacts(tmp_path):
    """Protect the adapter against drift from the HTTP service's real JSON shapes."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = os.environ | {
        "IMAGE_DATA_DIR": str(tmp_path),
        "IMAGE_API_ENABLE_CLASSIFIER": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    encoded = base64.b64encode(png()).decode()

    async def exercise():
        async with create_connected_server_and_client_session(
            load_adapter().create_server(url)
        ) as session:

            async def successful(name, arguments):
                result = await session.call_tool(name, arguments)
                assert not result.isError, result.model_dump()
                return result

            capabilities = await successful("capabilities", {})
            assert "png" in capabilities.structuredContent["formats"]
            inspection = await successful(
                "inspect_image",
                {"input": {"image_base64": encoded, "filename": "source.png"}},
            )
            assert (
                inspection.structuredContent["width"],
                inspection.structuredContent["height"],
            ) == (64, 32)
            result = await successful(
                "process_image",
                {
                    "input": {"image_base64": encoded},
                    "operations": [{"op": "resize", "width": 32}],
                    "output": {"format": "png"},
                },
            )
            image_id = result.structuredContent["artifact"]["id"]
            assert result.structuredContent["artifact"]["width"] == 32
            assert any(item.type == "image" for item in result.content)
            variants = await successful(
                "create_variants",
                {
                    "input": {"artifact_id": image_id},
                    "variants": [
                        {"name": "tiny", "operations": [{"op": "resize", "width": 16}]},
                        {
                            "name": "bad-crop",
                            "operations": [
                                {
                                    "op": "crop",
                                    "left": 0,
                                    "top": 0,
                                    "right": 1000,
                                    "bottom": 1000,
                                }
                            ],
                        },
                    ],
                },
            )
            assert [
                item["status"] for item in variants.structuredContent["results"]
            ] == ["success", "error"]
            batch = await successful(
                "process_batch",
                {
                    "jobs": [
                        {"input": {"artifact_id": image_id}, "operations": []},
                        {"input": {"path": "missing.png"}, "operations": []},
                    ]
                },
            )
            assert [item["status"] for item in batch.structuredContent["results"]] == [
                "success",
                "error",
            ]
            sheet = await successful(
                "contact_sheet",
                {
                    "images": [
                        {"input": {"image_base64": encoded}, "label": "source"},
                        {"input": {"artifact_id": image_id}, "label": "resized"},
                    ],
                    "columns": 2,
                    "cell_width": 64,
                    "cell_height": 64,
                    "background": "#0000ff",
                },
            )
            assert "artifact_id" in sheet.structuredContent["items"][0]["input"]
            assert encoded not in json.dumps(sheet.structuredContent)
            sheet_preview = next(item for item in sheet.content if item.type == "image")
            with Image.open(BytesIO(base64.b64decode(sheet_preview.data))) as picture:
                assert picture.convert("RGB").getpixel((0, 0)) == (0, 0, 255)
            vision = await successful(
                "prepare_vision",
                {
                    "input": {"artifact_id": image_id},
                    "max_edge": 16,
                    "max_pixels": 128,
                    "max_bytes": 100000,
                    "min_quality": 20,
                    "output": {"format": "jpeg", "quality": 20},
                },
            )
            assert vision.structuredContent["artifact"]["width"] == 16
            assert vision.structuredContent["budget_met"] is True
            assert vision.structuredContent["quality"] == 20
            assert vision.structuredContent["artifact"]["format"] == "jpeg"
            ai = await session.call_tool(
                "ai_inspect",
                {
                    "input": {"artifact_id": image_id},
                    "check_visible": False,
                    "check_invisible": False,
                },
            )
            if ai.isError:
                assert (
                    ai.structuredContent["error"]["code"]
                    == "ai_dependencies_unavailable"
                )
            else:
                assert ai.structuredContent["is_ai_generated"] is None
            classifier = await session.call_tool(
                "ai_classify", {"input": {"artifact_id": image_id}, "domain": "photo"}
            )
            assert classifier.isError is True
            assert (
                classifier.structuredContent["error"]["code"] == "classifier_disabled"
            )
            cleaned = await session.call_tool(
                "ai_clean",
                {
                    "input": {"artifact_id": image_id},
                    "visible": False,
                    "metadata": False,
                },
            )
            if cleaned.isError:
                assert (
                    cleaned.structuredContent["error"]["code"]
                    == "ai_dependencies_unavailable"
                )
            else:
                assert (
                    cleaned.structuredContent["invisible"]["status"] == "not_processed"
                )

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    f"API exited during startup: {process.stderr.read().decode(errors='replace')}"
                )
            try:
                if (
                    httpx.get(f"{url}/health", timeout=0.2, trust_env=False).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("The real API did not start within 10 seconds")
        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
