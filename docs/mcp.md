# Local MCP adapter

The optional MCP adapter is a stdio server that calls the same HTTP API as ordinary clients. Start the API first. Its default address is `http://127.0.0.1:8078`; set `IMAGE_API_URL` to change it.

Install the adapter dependencies in a virtual environment from the repository checkout:

```powershell
python -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install -r requirements-mcp.txt
.venv-mcp\Scripts\python.exe mcp_server.py
```

On Linux/macOS, use `.venv-mcp/bin/python` for the last two commands. The MCP process speaks the protocol on stdout and reserves stderr for SDK logs. Run it through an MCP client rather than typing requests in a terminal.

Example MCP client configuration on Windows:

```json
{
  "mcpServers": {
    "images": {
      "command": "E:/image-processing-api/.venv-mcp/Scripts/python.exe",
      "args": ["E:/image-processing-api/mcp_server.py"],
      "env": {
        "IMAGE_API_URL": "http://127.0.0.1:8078"
      }
    }
  }
}
```

Use absolute paths for the Python executable and script. The adapter does not need its working directory set to the repository. On Linux, use the matching absolute virtual-environment and checkout paths.

Tools expose nested request schemas, operation choices, format enums, and numeric constraints:

| Tool | HTTP endpoint | Purpose |
| --- | --- | --- |
| `capabilities` | `GET /capabilities` | Discover formats, operations and optional AI dependencies |
| `inspect_image` | `POST /inspect` | Inspect oriented image dimensions and metadata |
| `process_image` | `POST /process` | Apply ordered operations and encode once |
| `create_variants` | `POST /variants` | Produce named variants with ordered item results |
| `process_batch` | `POST /batch` | Produce independent ordered job results |
| `contact_sheet` | `POST /contact-sheet` | Compose labeled cells with links to original inputs |
| `prepare_vision` | `POST /prepare-vision` | Apply requested edge, pixel and byte budgets |
| `ai_inspect` | `POST /ai/inspect` | Inspect provenance and supported watermark evidence |
| `ai_classify` | `POST /ai/classify` | Use the optional CPU photo classifier |
| `ai_clean` | `POST /ai/clean` | Create an artifact after supported CPU cleanup |

Each image input supplies exactly one of `artifact_id`, `path`, or `image_base64`, plus an optional `filename`. Reuse artifact IDs after the first call so full image bytes do not repeatedly pass through a conversation.

Paths are interpreted by the HTTP API inside `IMAGE_DATA_DIR`. If Docker mounts `E:/image-processing-api/data` at `/data`, the host file `E:/image-processing-api/data/input/photo.png` is passed as `{"path":"input/photo.png"}` or `{"path":"/data/input/photo.png"}`. A Windows path outside the API's data directory is not a usable container path. The MCP process only needs HTTP connectivity; it does not read image files itself.

Example `process_image` arguments:

```json
{
  "input": {"path": "input/photo.png"},
  "operations": [
    {"op": "resize", "width": 800, "height": 600, "fit": "contain", "allow_upscale": false}
  ],
  "output": {"format": "webp", "quality": 85, "metadata": "strip"}
}
```

Results retain the HTTP JSON in MCP `structuredContent`, with reusable artifact IDs and download resource links. The adapter attaches at most one preview per call, fetched from `/artifacts/{id}/preview?max_edge=512` and limited to 512 pixels on either edge and 128 KiB. Missing, invalid or oversized previews leave the successful artifact result available. Image base64 fields are omitted from textual and structured results; only the bounded MCP image content contains encoded preview bytes.

`prepare_vision` accepts `min_quality` from 1 to 100, defaulting to 35. It must be no greater than `output.quality`; for example, set both to 20 to prepare a JPEG at quality 20. `contact_sheet` accepts a `background` color, defaulting to `#ffffff`.

HTTP failures produce MCP tool errors with `error.code`, `error.message`, `error.field`, and `error.details`. Per-item variant and batch failures stay in their original order while successful items retain artifact links. SDK argument validation rejects malformed requests before they reach the API.

Call `capabilities` before using optional AI tools. Provenance inspection reports evidence, and the optional photo classifier is a separate feature with local dependencies and weights. General invisible image cleanup is unsupported on CPU. The adapter installs no AI models and performs no processing or implicit model downloads.
