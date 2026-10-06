# Image Processing API

A local CPU image utility for HTTP clients and AI agents. Converts PNG, JPEG, WebP and AVIF; applies ordered transformations; stores reusable artifacts; prepares vision inputs; and inspects/removes supported AI marks. The default Docker setup binds to `127.0.0.1:8078` and needs no GPU or model download.

## Run locally

```powershell
docker compose up -d --build
curl.exe http://127.0.0.1:8078/health
curl.exe http://127.0.0.1:8078/capabilities
```

Interactive documentation: [http://127.0.0.1:8078/docs](http://127.0.0.1:8078/docs). Machine-readable contract: `/openapi.json`. The image works without a source-code bind mount. Development reload is explicit:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

For conversions without optional AI dependencies, build the smaller target:

```powershell
docker build --target core -t image-processing-api:core .
docker run --rm -p 127.0.0.1:8078:8000 -v "${PWD}/data:/data" image-processing-api:core
```

`IMAGE_DATA_DIR` defaults to the repository's `data` directory outside Docker and `/data` in Docker. `IMAGE_WORKERS` defaults to 2 and bounds CPU work; processing runs in worker threads so health checks can respond during encoding. Adjust the limit to suit the machine.

## Connect an AI agent

Point an HTTP-capable agent to **[http://127.0.0.1:8078/openapi.json](http://127.0.0.1:8078/openapi.json)** for the complete machine-readable endpoint and request schemas. Have it read **[http://127.0.0.1:8078/capabilities](http://127.0.0.1:8078/capabilities)** to discover the operations and optional AI features available in the running service. Human-readable API documentation is at **[http://127.0.0.1:8078/docs](http://127.0.0.1:8078/docs)**.

Choose the base URL according to where the agent runs:

| Agent location | Base URL |
| --- | --- |
| On the same computer as Docker | `http://127.0.0.1:8078` |
| In another container attached to the API's Compose network | `http://image-processing-api:8000` |

Inside an agent container, `127.0.0.1` refers to that container. Attach it to the API's Docker network before using the service-name address. The default host port remains bound to loopback; this setup does not expose the API to other computers. The local API requires no API key.

Copy this instruction to an agent, adjusting the base URL if it runs in Docker:

```text
Use our local Image Processing API at http://127.0.0.1:8078.
First GET /openapi.json for request schemas and GET /capabilities for readiness.

Upload originals with POST /images, multipart field "image".
The response has an "id". Reuse it as {"input":{"artifact_id":"ID"}}.
Inspect originals before conversion, because conversion can discard provenance.

AI inspection and cleanup:
1. POST /ai/inspect with {"input":{"artifact_id":"ORIGINAL_ID"}}.
2. When cleanup is requested, POST /ai/clean with
   {"input":{"artifact_id":"ORIGINAL_ID"},"visible":true,"metadata":true}.
3. Use the cleanup response's artifact.id to POST /ai/inspect again.
4. Download the cleaned image with GET /artifacts/CLEANED_ID.

Use POST /process for ordered image transformations.
Use POST /variants or /batch for multiple outputs or independent jobs.
Use POST /prepare-vision to meet image dimension and byte budgets.
Follow the schemas in /openapi.json for these requests.

is_ai_generated:null means unknown, not human-generated.
Cleanup removes supported marks/metadata; it does not change an image's origin.
General invisible-watermark removal is unsupported on CPU.
The optional photo classifier is disabled by default; check /capabilities.
```

For an MCP-capable agent, configure the [stdio MCP adapter](docs/mcp.md) to expose the 10 typed tools. The client launches `mcp_server.py` as a local process with `IMAGE_API_URL` set to the reachable HTTP base URL. The adapter uses stdio; there is no HTTP `/mcp` endpoint. Keep the Docker API running while either type of agent uses it.

## Agent workflow

Upload once, reuse an ID, and request a pipeline rather than repeatedly transferring and compressing images:

```powershell
curl.exe -F "image=@photo.jpg" http://127.0.0.1:8078/images
```

The response is an artifact descriptor with `id`, `path`, `download_url`, filename, MIME type, displayed dimensions, encoded mode, alpha presence, byte size and warnings. An upload preserves the original bytes for later provenance inspection. Use `GET /artifacts/{id}` to download and `GET /artifacts/{id}/preview?max_edge=512` for a small PNG preview.

Every JSON image input provides exactly one source:

```json
{"artifact_id":"<returned UUID>"}
```

```json
{"path":"input/photo.jpg"}
```

```json
{"image_base64":"<raw base64 or image data URI>","filename":"photo.jpg"}
```

Paths must be inside the configured data directory. With the default mount, `E:/image-processing-api/data/input/photo.jpg` on Windows is `/data/input/photo.jpg` in Docker; either that container path or relative `input/photo.jpg` works. A Windows host path cannot be passed directly to the Linux container. Generated files and descriptor sidecars live under `data/artifacts`. They persist across restarts and are retained until you remove them from that directory; remove both the image and its matching UUID JSON sidecar when tidying up.

`POST /process` decodes the source once, applies operations in order and encodes the final image once:

```json
{
  "input":{"path":"input/photo.jpg"},
  "operations":[
    {"op":"crop","left":0.1,"top":0.1,"right":0.9,"bottom":0.9,"units":"normalized"},
    {"op":"resize","width":800,"height":600,"fit":"pad","background":"#ffffff","allow_upscale":false},
    {"op":"watermark","text":"Sample","position":"bottom-right","angle":0,"color":"#222222","opacity":0.7}
  ],
  "output":{"format":"webp","quality":85,"metadata":"preserve"}
}
```

The response contains `artifact`, `operations`, `warnings`, and `coordinate_transform`. Operation logs include input/output dimensions and geometry. EXIF/XMP orientation is applied before geometry, so coordinates use the displayed source. Pixel crop edges are integers with exclusive right/bottom edges. Normalized edges are fractions in `[0,1]`, rounded outward to retain the requested region. Each log's 3×3 matrix maps output pixel-edge coordinates back to that operation's input; the top-level matrix composes these back to the oriented source. Multiply `[x,y,1]` by it to map a box from a preview back to the source. Added padding can map outside the original; `content_box` identifies actual placed content.

| Operation | Parameters and behavior |
| --- | --- |
| `resize` | Width/height **or** percentage; `contain`, `cover`, `stretch`, or `pad`; `anchor`, background, `allow_upscale` (default false). Cover/stretch/pad need both dimensions. Cover without upscaling can return a smaller crop. Pad returns the requested canvas. |
| `crop` | left/top/right/bottom; `units` pixels or normalized; rejects out-of-bounds crops. |
| `rotate` | Counterclockwise angle in degrees, `expand` (true), background. |
| `flip` | `direction`: horizontal or vertical. |
| `pad` | Uniform `border` or individual left/top/right/bottom; background. |
| `trim` | Removes transparent borders, optionally `alpha_threshold`; entirely transparent inputs retain dimensions with a warning. |
| `flatten` | Composites transparency on an opaque background, default white. |
| `watermark` | Text and/or `logo_base64`, color, opacity, font size, angle, position, margin, spacing, density, optional logo width. Position can be repeated, corners, center or a side/corner. Alpha is composited once. |

Anchors/positions: `center`, `top-left`, `top`, `top-right`, `left`, `right`, `bottom-left`, `bottom`, `bottom-right`.

Output options:

| Option | Meaning |
| --- | --- |
| `format` | png, webp, jpg/jpeg, avif; JPEG uses MIME `image/jpeg`. |
| `quality` | 1–100, default 85, for lossy JPEG/WebP/AVIF. PNG is always lossless. |
| `background` | Opaque JPEG flattening background, default white. Palette and grayscale alpha are handled consistently. |
| `metadata` | `preserve` retains readable EXIF, ICC, DPI and PNG text where supported by the format; orientation and EXIF dimensions are updated. `strip` omits these. Container signatures/animation are not preserved by processing. |
| `lossless`, `method` | WebP lossless switch and effort 0–6 (default 4). |
| `compression_level` | PNG compression 0–9 (default 6), separate from quality. |
| `speed` | AVIF speed 0–10 (default 6). |
| `progressive` | Progressive JPEG, default false. |

Animated uploads can be stored unchanged. Processing uses the first frame and reports `first_frame_only` in JSON or `X-Image-Warnings` on binary routes. `/inspect` reports frame count and raw/displayed dimensions. Visible AI cleanup requires a static image.

## Other JSON workflows

| Endpoint | Request / result |
| --- | --- |
| `POST /inspect` | `{input}` → dimensions, input format/mode, bytes, alpha, orientation, frames, ICC/EXIF presence, DPI and warnings. Does not run compression comparisons. |
| `POST /compare` | `{input,formats:["webp","png"],quality:85}` → per-format sizes/savings; numeric savings are `percentage_value`. |
| `POST /variants` | `{input,variants:[{name,operations,output},...]}` → ordered results with per-item success/error. Source pixels are decoded once and reused. |
| `POST /batch` | `{jobs:[{input,operations,output},...]}` → ordered independent results; missing files or invalid crop bounds do not abort other jobs. Malformed request schemas still return 422 for the request. |
| `POST /contact-sheet` | `{images:[{input,label},...],columns:3,cell_width:256,cell_height:256,background:"#ffffff",output:{format:"png"}}` → labeled artifact and cell boxes referencing the originals. Base64 originals become reusable artifact IDs. |
| `POST /prepare-vision` | `{input,max_edge:1024,max_pixels:1000000,max_bytes:200000,allow_upscale:false,min_quality:35,output:{format:"jpeg",quality:85}}` → chosen artifact/quality, geometry and `budget_met`. |
| `POST /images/base64` | `{input}` → stores an original from a base64, path or ID source. |

Vision preparation has no hardcoded model limits. It first meets edge/pixel limits, then searches lossy quality between `min_quality` and `output.quality` and reduces dimensions if needed. PNG/lossless WebP meet a byte budget through dimension changes. If even a 1×1 output exceeds the byte budget, it returns `budget_met:false` and `byte_budget_unmet`; inspect the returned dimensions before using it. Set `min_quality` at or below the requested quality. For text-heavy screenshots, PNG or lossless WebP avoids lossy text artifacts.

Caller errors return 4xx and an actionable JSON object:

```json
{"detail":"Crop is outside the image bounds","error":{"code":"invalid_crop","message":"Crop is outside the image bounds","field":"operations.0","details":{"input_size":[120,80],"crop_box":[0,0,999,10]}}}
```

Legacy multipart binary routes remain available: `/convert`, `/resize`, `/crop`, `/watermark`; `/metadata` and `/info` return JSON. `/convert/base64` supports its existing JSON/form/plain-text inputs. Legacy resize permits upscaling; the JSON resize operation defaults to preventing it. Unicode download filenames use an encoded UTF-8 header.

## CPU AI inspection and cleanup

The default image installs the CPU subset of [wiltodelta/remove-ai-watermarks](https://github.com/wiltodelta/remove-ai-watermarks), pinned to 0.45.0. This fits the existing Python image API better than introducing another general file-cleaning HTTP service. OpenCV filling requires no downloaded model; capabilities report the library's registered mark families and installed detectors.

```json
{"input":{"artifact_id":"<original ID>"},"check_visible":true,"check_invisible":true}
```

Send that to `POST /ai/inspect`. Inspect originals before conversion: re-encoding can discard provenance. Results distinguish provenance signals, known visible marks and supported open DWT-DCT pixel watermarks. `is_ai_generated:null` means unknown; no readable signal is not proof of human origin. Content Credentials alone do not mean an AI-generated image. General SynthID pixel verification and TrustMark model detection are not enabled.

`POST /ai/clean` accepts `{input,visible:true,metadata:true}` and creates a new artifact. It returns the original inspection, per-mark cleanup status/confidence/mask boxes, verified metadata removal status, warnings and `invisible:{status:"not_processed",reason:"general_image_removal_unsupported_on_cpu"}`. A mark's `mask_bbox` is `[x,y,width,height]` in stored-raster coordinates, before display orientation. General invisible image watermark removal is unsupported on CPU. Cleaning visible marks or metadata does not establish that an image has no hidden pixel marks.

Actual visible edits are saved as lossless PNG so JPEG re-encoding does not change pixels outside the reported masks. No-mark cleanup preserves the original bytes when metadata removal is disabled. Metadata cleanup retains standard author/title fields where supported and verifies remaining recognized AI fields. Pixel edits can invalidate signed provenance; filling textured areas can blur them, and a lower detector score is not a reconstruction-quality guarantee.

`POST /ai/classify` is separately opt-in and accepts `{input,domain:"photo"}`. It returns ai/human/unknown, detector opinion and optional provider attribution, with explicit CPU device. Photographs are its supported domain; it is not a universal screenshot/art/scan detector. Classification is disabled in the default image and has not been benchmarked on your photo collection.

Optional classifier setup is in [docs/classifier.md](docs/classifier.md). It uses CPU PyTorch and pinned local weights/configuration. Missing dependencies or local files return 503 without downloading them. Check `/capabilities` for readiness.

## MCP

An optional stdio adapter supplies 10 typed tools, structured results, downloadable artifact links and a small image preview. It calls this same HTTP service. See [docs/mcp.md](docs/mcp.md) for installation and an MCP client configuration.

## Development and checks

Use Python 3.11–3.13 for the pinned full runtime dependencies (Docker uses 3.11):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-ai.txt -r requirements-mcp.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m black --check app tests mcp_server.py scripts
.venv\Scripts\python.exe scripts/smoke.py --url http://127.0.0.1:8078
.venv\Scripts\python.exe scripts/mcp_smoke.py --url http://127.0.0.1:8078
```

The tests cover image modes/orientation/metadata, invalid inputs, typed contracts, ordered geometry, artifacts, vision limits, CPU known-mark/metadata behavior, and MCP discovery/stdio/real-HTTP calls. AI and MCP tests skip when their optional dependencies are absent. The Docker smoke script exercises real endpoints and registered CPU visible cleanup. No classifier accuracy claim comes from these tests.
