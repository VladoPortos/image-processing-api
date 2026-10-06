FROM python:3.11-slim AS core

WORKDIR /app

# Pillow/OpenCV use binary wheels. Only fonts need a system package.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY ./app /app/app
ENV IMAGE_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
VOLUME ["/data"]

# Expose API port
EXPOSE 8000

# Command to run the API
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# CPU metadata, known visible marks and DWT-DCT inspection. No Torch/CUDA.
FROM core AS ai
COPY requirements-ai.txt .
ARG PIP_FIND_LINKS
ARG PIP_TRUSTED_HOST
ARG PIP_NO_INDEX
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements-ai.txt
