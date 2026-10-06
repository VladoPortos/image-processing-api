"""Check stdio MCP tools against a running local Docker/HTTP API."""

import argparse
import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


async def exercise(url):
    stream = io.BytesIO()
    Image.new("RGB", (64, 32), "#446688").save(stream, "PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "mcp_server.py")],
        env=os.environ | {"IMAGE_API_URL": url},
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 10
            assert all(tool.inputSchema and tool.outputSchema for tool in tools.tools)

            async def successful(name, arguments):
                result = await session.call_tool(name, arguments)
                assert not result.isError, result.model_dump()
                return result

            capabilities = (await successful("capabilities", {})).structuredContent
            inspection = await successful(
                "inspect_image", {"input": {"image_base64": encoded}}
            )
            assert inspection.structuredContent["width"] == 64
            result = await successful(
                "process_image",
                {
                    "input": {"image_base64": encoded},
                    "operations": [{"op": "resize", "width": 32}],
                    "output": {"format": "png"},
                },
            )
            assert any(item.type == "image" for item in result.content)
            assert any(item.type == "resource_link" for item in result.content)
            reference = {"artifact_id": result.structuredContent["artifact"]["id"]}
            variants = await successful(
                "create_variants",
                {
                    "input": reference,
                    "variants": [
                        {"name": "tiny", "operations": [{"op": "resize", "width": 16}]},
                        {
                            "name": "bad",
                            "operations": [
                                {
                                    "op": "crop",
                                    "left": 0,
                                    "top": 0,
                                    "right": 999,
                                    "bottom": 10,
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
                {"jobs": [{"input": {"path": "missing.png"}}, {"input": reference}]},
            )
            assert [item["status"] for item in batch.structuredContent["results"]] == [
                "error",
                "success",
            ]
            sheet = await successful(
                "contact_sheet",
                {
                    "images": [
                        {"input": reference, "label": "Original"},
                        {"input": reference, "label": "Copy"},
                    ],
                    "background": "#0000ff",
                },
            )
            assert len(sheet.structuredContent["items"]) == 2
            vision = await successful(
                "prepare_vision",
                {
                    "input": reference,
                    "max_edge": 16,
                    "max_bytes": 10000,
                    "min_quality": 20,
                    "output": {"format": "jpeg", "quality": 20},
                },
            )
            assert vision.structuredContent["budget_met"] is True
            if capabilities["ai"]["library"]["installed"]:
                evidence = await successful(
                    "ai_inspect",
                    {
                        "input": reference,
                        "check_visible": False,
                        "check_invisible": False,
                    },
                )
                assert evidence.structuredContent["is_ai_generated"] is None
                cleaned = await successful(
                    "ai_clean", {"input": reference, "visible": False}
                )
                assert (
                    cleaned.structuredContent["invisible"]["status"] == "not_processed"
                )
            classifier = await session.call_tool(
                "ai_classify", {"input": reference, "domain": "photo"}
            )
            if not capabilities["ai"]["classification"]["enabled"]:
                assert classifier.isError
                assert (
                    classifier.structuredContent["error"]["code"]
                    == "classifier_disabled"
                )
            return {"status": "passed", "tools": len(tools.tools), "url": url}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8078")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(exercise(args.url))))


if __name__ == "__main__":
    main()
