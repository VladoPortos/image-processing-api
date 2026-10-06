"""Persistent local artifacts; the filesystem itself is the shared agent volume."""

import json
import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from .core import MIME, DecodedImage, decode_base64, decode_image, has_alpha
from .errors import ImageError
from .models import Artifact, InputReference


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.directory = self.root / "artifacts"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _sidecar(self, artifact_id):
        try:
            validated = str(UUID(artifact_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ImageError(
                "artifact_not_found",
                "Unknown artifact ID",
                field="artifact_id",
                status_code=404,
            ) from error
        return self.directory / f"{validated}.json"

    def descriptor(self, artifact_id) -> Artifact:
        sidecar = self._sidecar(artifact_id)
        try:
            return Artifact.model_validate_json(sidecar.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ImageError(
                "artifact_not_found",
                "Unknown artifact ID",
                field="artifact_id",
                status_code=404,
            ) from error

    def resolve(self, source: InputReference) -> tuple[bytes, str]:
        if source.image_base64 is not None:
            return decode_base64(source.image_base64), source.filename or "image"
        if source.artifact_id is not None:
            descriptor = self.descriptor(source.artifact_id)
            candidate = Path(descriptor.path)
            filename = descriptor.filename
        else:
            candidate = Path(source.path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            filename = candidate.name
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root):
            raise ImageError(
                "path_outside_data_root",
                f"Input path must be inside {self.root}",
                field="path",
            )
        try:
            return candidate.read_bytes(), source.filename or filename
        except (FileNotFoundError, IsADirectoryError) as error:
            raise ImageError(
                "input_not_found",
                "Image file does not exist",
                field="path",
                details={"path": str(candidate)},
                status_code=404,
            ) from error

    def save(
        self, data: bytes, filename: str, *, warnings=None, decoded=None
    ) -> Artifact:
        decoded = decoded or decode_image(data, filename)
        artifact_id = str(uuid4())
        format = decoded.inspection["format"]
        extension = "jpg" if format == "jpeg" else format
        path = self.directory / f"{artifact_id}.{extension}"
        descriptor = Artifact(
            id=artifact_id,
            filename=filename,
            path=str(path),
            download_url=f"/artifacts/{artifact_id}",
            mime_type=MIME.get(format, f"image/{format}"),
            format=format,
            width=decoded.image.width,
            height=decoded.image.height,
            mode=decoded.inspection.get("mode", decoded.image.mode),
            has_alpha=decoded.inspection["has_alpha"],
            size_bytes=len(data),
            warnings=warnings or decoded.warnings,
        )
        self._atomic_write(path, data)
        self._atomic_write(
            self._sidecar(artifact_id),
            descriptor.model_dump_json(indent=2).encode("utf-8"),
        )
        return descriptor

    def save_encoded(self, data, filename, image, format, warnings):
        # Read the encoded header, without decoding pixels a second time. Some
        # encoders turn grayscale into RGB or omit an entirely opaque alpha band.
        with Image.open(BytesIO(data)) as header:
            inspection = {
                "format": header.format.lower(),
                "mode": header.mode,
                "has_alpha": has_alpha(header),
            }
        decoded = DecodedImage(image, {}, inspection, warnings)
        return self.save(data, filename, warnings=warnings, decoded=decoded)

    def _atomic_write(self, target, data):
        with tempfile.NamedTemporaryFile(
            dir=self.directory, suffix=".tmp", delete=False
        ) as temporary:
            temporary.write(data)
            temporary_path = temporary.name
        os.replace(temporary_path, target)
