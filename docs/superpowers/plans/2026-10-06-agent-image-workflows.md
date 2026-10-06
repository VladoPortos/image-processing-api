# Agent image workflows implementation

Goal: implement the approved practical review in `temp/review/REVIEW.md` and CPU-only integration in `temp/review/WATERMARKS.md`.

Architecture: a shared Pillow core, typed ordered operations, a local artifact store, bounded worker offloading, FastAPI compatibility routes, and a thin optional MCP HTTP adapter. Keep original bytes for provenance inspection. Use `remove-ai-watermarks` 0.45.0 with explicit OpenCV CPU cleanup. Optional photo classification is separate from evidence-based inspection; absence of evidence remains unknown. General invisible image cleanup is unsupported on CPU.

Tech stack: Python 3.11+, FastAPI/Pydantic, Pillow, Docker Compose, pytest/httpx, optional OpenCV/C2PA library and MCP SDK.

Global constraints: local use only; no authentication infrastructure or speculative security features. Preserve legacy upload/base64 workflows. No CUDA installs, implicit model downloads, or classifier accuracy claims. User approved autonomous implementation; use existing feature branch rather than adding an approval gate for worktree selection.

Review focus: oriented coordinates, all alpha modes, metadata policy, real binary/OpenAPI contracts, error semantics, decode/encode once, reusable artifact paths and IDs, honest AI capability results, and CPU behavior.

## Shared contracts

- `InputReference`: exactly one `artifact_id`, `path` (inside data root), or `image_base64`; optional filename.
- `OutputOptions`: format png/webp/jpeg/jpg/avif, quality 1..100, background, metadata preserve/strip, lossless, method 0..6, speed 0..10, compression_level 0..9, progressive.
- `POST /process`: `{input, operations: [{op: ...}], output}` -> `{artifact, operations, warnings}`. Operation coordinates refer to the displayed, EXIF-oriented image. Log scale/crop offsets and coordinate transforms.
- Artifact descriptor: id, filename, path, download_url, mime_type, format, width, height, mode, has_alpha, size_bytes, warnings. Original uploads are preserved. `GET /artifacts/{id}` downloads bytes; `/preview` supplies a small image.
- `POST /inspect`, `/variants` (`input`, named `variants` with operations/output), `/batch` (`jobs` of process requests), `/contact-sheet` (`images` of `{input,label}`, columns/cell_width/cell_height/output), `/prepare-vision` (input,max_edge,max_pixels,max_bytes,allow_upscale,output).
- AI: `/ai/inspect` input + check_visible/check_invisible; `/ai/classify` input + domain photo; `/ai/clean` input + visible/metadata booleans. Return structured summaries, never NumPy image arrays.
- Errors: `{detail, error: {code,message,field,details}}`; validation errors 4xx. Batch/variants retain ordered per-item results.

## Execution tasks

1. Root: regression tests showing existing orientation/palette/Unicode/errors/schema failures; shared decoding/encoding/errors/models/artifacts and API routes. Verify tests fail before fixes.
2. Operations delegate: only `app/operations.py`, `tests/test_operations.py`. Typed discriminated operation models and `apply_operations(image, operations)` -> `(image, logs, warnings)`. Resize contain/cover/stretch/pad with anchors/upscale; pixel/normalized crop; rotate, flip, pad, trim, flatten; text/logo watermark with alpha compositing. Meaningful tests first.
3. AI delegate: only `app/ai_service.py`, `tests/test_ai_service.py`, `requirements-ai.txt`, `requirements-classify.txt`. API functions `inspect_ai(data,filename,check_visible=True,check_invisible=True)`, `clean_ai(data,filename,visible=True,metadata=True)` -> dict with bytes key `data` plus summary, `classify_ai(data,filename,domain='photo')`, `capabilities()`. Own `AIServiceError` with code/message/status_code. Validate against installed real library; CPU only, no request-time weight downloads.
4. MCP delegate: only `mcp_server.py`, `tests/test_mcp_server.py`, `requirements-mcp.txt`, `docs/mcp.md`. Thin SDK HTTP client, explicit tool schemas mirroring contracts; structured artifact results with optional small image previews, resource links. No stdout logging. Verify SDK tool discovery and actual HTTP protocol behavior.
5. Root: variants/batches/contact sheets/vision budgets and inspection/comparison; integration of independent modules.
6. Root: runtime/dev dependency split, standalone Docker image, local Compose binding/data volume, dev reload override, README examples and CPU/classifier setup.
7. Verification: full pytest suite, real CPU Docker endpoints and MCP handshake/tool calls, standalone image health, real upstream known-mark/no-op/metadata tests; review final diff and fix material issues. Optional photo classifier benchmark only if local weights/dependencies are available; otherwise clearly document unbenchmarked optional feature.

## Progress

- [x] Approved design recorded, feature branch created.
- [x] Regression baseline and common core.
- [x] Operations and AI integration.
- [x] Artifact workflows, variants, batches, vision preparation, contact sheets.
- [x] MCP adapter.
- [x] Docker and documentation.
- [x] Full verification and review.

## Completion evidence

- Host Python 3.14/Pillow 12.3: 132 tests passed.
- Linux Docker Python 3.11.16/Pillow 11.1.0/FastAPI 0.115.8/Pydantic 2.13.5: 132 tests passed, no skips. Two upstream dependency warnings concern Starlette's deprecated AnyIO alias and an MCP settings forward reference; tool calls passed.
- Black: all 18 Python files pass. Whitespace checks pass.
- Standalone core and CPU AI Docker targets built using the actual Dockerfile. AI dependency wheels were fetched once and served from a temporary loopback wheel cache for the build. No source mount is required by the runtime image.
- Compose runtime is healthy, bound to 127.0.0.1:8078, with data and read-only model volumes. Torch is absent from the default CPU image.
- Final packaging check: running application files match the workspace byte for byte. The local Compose image tag points to this validated build; earlier delayed builds have completed.
- Real Docker HTTP smoke: 33 requests passed, including known Gemini template cleanup with exact unchanged pixels outside reported masks, verified AI metadata cleanup, every output codec, artifacts, geometry, variants, batches and vision budgets.
- Real SDK stdio MCP smoke against Docker: all 10 tools passed, including structured results, previews, resource links, ordered item errors and disabled-classifier errors.
- Read-only review found and confirmed fixes for preserved XMP orientation, nested EXIF size tags, nested camera/GPS extraction, MCP argument omissions and C2PA readiness filtering. No outstanding material findings.
- Real upstream Baidu, SDXL and C2PA fixtures were checked during integration. Classification weights were absent, so full photo inference/accuracy/latency remain explicitly unbenchmarked optional capabilities; dependency/readiness/offline/CPU contracts are tested. General invisible removal is not implemented because the selected library's general image path requires CUDA.
