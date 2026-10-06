from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

try:
    import pillow_avif  # noqa: F401 -- Match the runtime codec registration on Pillow 11.
except ImportError:
    pass


def image_bytes(image, format="PNG", **kwargs):
    stream = BytesIO()
    image.save(stream, format=format, **kwargs)
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    from app import main

    application = main.create_app(tmp_path) if hasattr(main, "create_app") else main.app
    with TestClient(application, raise_server_exceptions=False) as connection:
        yield connection


@pytest.fixture
def png():
    return image_bytes(Image.new("RGB", (120, 80), "#226688"))
