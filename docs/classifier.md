# Optional local CPU photo classifier

The ordinary Docker image supports model-free AI evidence/cleanup. Photo classification is an optional larger installation. It has not been benchmarked on this user's photographs, and a camera-like result is not proof of human authorship.

Build the ordinary image first, then the explicit CPU classifier variant:

```powershell
docker compose build
docker compose -f docker-compose.yml -f docker-compose.classify.yml build
```

The classifier Dockerfile installs `torch==2.11.0+cpu` from the official CPU index before resolving the classifier dependencies. It does not install a default CUDA Torch wheel. To use a separate Python environment instead:

```powershell
python -m pip install torch==2.11.0+cpu --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-classify.txt
```

Fetch the pinned weights/configuration once, explicitly, outside API requests. The following Python snippet uses the installed Hugging Face client:

```python
from pathlib import Path
from huggingface_hub import snapshot_download, hf_hub_download
import shutil

folder = Path('models/photo-classifier').resolve()
folder.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id='wiltodelta/raiw-photo-classify',
    revision='de668f0d8a53412923478cc21382a56890748310',
    local_dir=folder,
    allow_patterns=['clip-l-ft.pt', 'probe-weights-clip-l-ft.npz',
                    'detector.pt', 'provider.pt', 'operating-point.json',
                    'receipt-gate.npz', 'run1/*'],
)
for name in ('config.json', 'preprocessor_config.json'):
    cached = hf_hub_download(
        repo_id='openai/clip-vit-large-patch14', filename=name,
        revision='32bd64288804d66eefd0ccbe215aa642df71cc41',
    )
    shutil.copyfile(cached, folder / name)
```

These small CLIP configuration files are required because upstream 0.45.0 otherwise resolves them online even when its trained weights are local. The adapter points the upstream CLIP loader to the local directory. It accepts weights at the root or under `run1`, checks required files, forces `device='cpu'`, and serializes classifier initialization/inference.

Start with the override once the local directory is complete:

```powershell
docker compose -f docker-compose.yml -f docker-compose.classify.yml up -d
curl.exe http://127.0.0.1:8078/capabilities
```

Outside Docker, set `IMAGE_API_ENABLE_CLASSIFIER=true`, `RAIW_CLASSIFY_WEIGHTS` to the absolute local folder, and `HF_HUB_OFFLINE=1`. Restart the API after changing the configured model directory. The normal compose mount maps `./models` to `/models` read-only. Required files are the four trained weight files plus `config.json` and `preprocessor_config.json`; readiness reports missing names. Models are loaded only by an explicit `/ai/classify` call.

Use `domain:'photo'`. Before automating actions from its labels, measure CPU latency/memory and validate ai/human/unknown behavior on a representative set of your photographs, including resized/JPEG copies. Keep classifier opinions separate from metadata and watermark evidence.
