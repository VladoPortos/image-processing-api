# Image Processing API
Docker image for processing images for resizing and format changes

## Overview
This API provides endpoints for converting images between different formats (AVIF, WebP, PNG, JPG) with adjustable quality settings. It also offers multiple image processing capabilities like resizing, cropping, watermarking, and metadata extraction.

## Features
- Convert images to AVIF, WebP, PNG, and JPG formats
- Adjust quality settings for optimized compression
- Resize images to specific dimensions or by percentage
- Crop images to specific regions
- Add diagonal watermarking text across images
- Extract metadata and EXIF information from images
- Get detailed information about potential image size savings
- Health check endpoint with system information
- Runs in Docker with Docker Compose
- Development environment with hot-reload

## Requirements
- Docker and Docker Compose

## Quick Start
1. Clone this repository
2. Run the API with Docker Compose:
```bash
docker-compose up -d
```
3. Access the API at http://localhost:8078

## API Endpoints

### 1. Convert Image
**Endpoint:** `POST /convert`

**Parameters:**
- `image`: The image file to convert (multipart/form-data)
- `format`: Target format (avif, webp, png, jpg)
- `quality`: Quality setting (1-100), default is 85

**Response:** The converted image file

### 2. Image Information
**Endpoint:** `POST /info`

**Parameters:**
- `image`: The image file to analyze (multipart/form-data)
- `quality`: Quality setting (1-100), default is 85

**Response:** JSON with size information for all supported formats
```json
{
  "original": {
    "size_bytes": 123456,
    "size_human": "120.56 KB",
    "format": "JPEG",
    "width": 800,
    "height": 600,
    "mode": "RGB"
  },
  "formats": {
    "avif": {
      "quality": 85,
      "size_bytes": 30000,
      "size_human": "29.30 KB",
      "savings": {
        "bytes": 93456,
        "percentage": "75.70%"
      }
    },
    "webp": {
      "quality": 85,
      "size_bytes": 35000,
      "size_human": "34.18 KB",
      "savings": {
        "bytes": 88456,
        "percentage": "71.65%"
      }
    },
    "png": {
      "quality": 85,
      "size_bytes": 110000,
      "size_human": "107.42 KB",
      "savings": {
        "bytes": 13456,
        "percentage": "10.90%"
      }
    },
    "jpg": {
      "quality": 85,
      "size_bytes": 40000,
      "size_human": "39.06 KB",
      "savings": {
        "bytes": 83456,
        "percentage": "67.60%"
      }
    }
  }
}
```

### 3. Resize Image
**Endpoint:** `POST /resize`

**Parameters:**
- `image`: The image file to resize (multipart/form-data)
- `width`: Target width in pixels (optional)
- `height`: Target height in pixels (optional)
- `percentage`: Scale by percentage, e.g., 50 for half size (optional)
- `maintain_aspect_ratio`: Whether to maintain aspect ratio (boolean, default is true)
- `format`: Target format (avif, webp, png, jpg)
- `quality`: Quality setting (1-100), default is 85

**Response:** The resized image file

### 4. Crop Image
**Endpoint:** `POST /crop`

**Parameters:**
- `image`: The image file to crop (multipart/form-data)
- `left`: Left coordinate for cropping
- `top`: Top coordinate for cropping
- `right`: Right coordinate for cropping
- `bottom`: Bottom coordinate for cropping
- `format`: Target format (avif, webp, png, jpg)
- `quality`: Quality setting (1-100), default is 85

**Response:** The cropped image file

### 5. Watermark Image
**Endpoint:** `POST /watermark`

**Parameters:**
- `image`: The image file to watermark (multipart/form-data)
- `text`: Text to use as watermark
- `opacity`: Watermark opacity (0.0-1.0), default is 0.5
- `density`: Controls watermark density (1-50, higher = more watermarks), default is 15
- `font_size`: Custom font size in pixels (optional, calculated automatically if not provided)
- `format`: Output format (avif, webp, png, jpg)
- `quality`: Quality setting (1-100), default is 85

**Response:** Watermarked image file

### 6. Extract Metadata
**Endpoint:** `POST /metadata`

**Parameters:**
- `image`: The image file to analyze (multipart/form-data)

**Response:** JSON with image metadata and EXIF information
```json
{
  "filename": "example.jpg",
  "format": "JPEG",
  "mode": "RGB",
  "size": {
    "width": 800,
    "height": 600
  },
  "info": {
    "jfif": "JFIF information",
    "dpi": [72, 72]
  },
  "exif": {
    "Make": "Camera Manufacturer",
    "Model": "Camera Model",
    "DateTime": "2025:02:15 12:30:45",
    "ExposureTime": "1/125",
    "FNumber": 5.6,
    "ISOSpeedRatings": 100,
    "FocalLength": 24.0
  }
}
```

### 7. Health Check
**Endpoint:** `GET /health`

**Parameters:** None

**Response:** JSON with health status and system information
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime": {
    "days": 0,
    "hours": 1,
    "minutes": 23,
    "seconds": 45,
    "total_seconds": 5025
  },
  "system_info": {
    "python_version": "3.11.0 (main, Oct 24 2022, 18:26:48) [GCC 10.2.1 20210110]",
    "platform": "Linux-5.15.0-1035-aws-x86_64-with-glibc2.31",
    "pillow_version": "10.0.0",
    "supported_formats": ["avif", "webp", "png", "jpg", "jpeg"]
  }
}
```

## Testing the API

You can test the API using the interactive Swagger documentation or with curl commands.

### Using Swagger UI
1. Open a web browser and go to http://localhost:8078/docs
2. Try out any endpoint using the interactive interface

### Using Curl

For the convert endpoint:
```bash
curl -X POST "http://localhost:8078/convert" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg" \
  -F "format=webp" \
  -F "quality=85" \
  --output image.webp
```

For the info endpoint:
```bash
curl -X POST "http://localhost:8078/info" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg" \
  -F "quality=85"
```

For the resize endpoint:
```bash
curl -X POST "http://localhost:8078/resize" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg" \
  -F "width=800" \
  -F "height=600" \
  -F "maintain_aspect_ratio=true" \
  -F "format=webp" \
  -F "quality=85" \
  --output image_resized.webp
```

For the crop endpoint:
```bash
curl -X POST "http://localhost:8078/crop" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg" \
  -F "left=100" \
  -F "top=100" \
  -F "right=500" \
  -F "bottom=400" \
  -F "format=webp" \
  -F "quality=85" \
  --output image_cropped.webp
```

For the watermark endpoint:
```bash
curl -X POST "http://localhost:8078/watermark" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg" \
  -F "text=CONFIDENTIAL" \
  -F "opacity=0.5" \
  -F "density=15" \
  -F "font_size=36" \
  -F "format=webp" \
  -F "quality=90" \
  --output image_watermarked.webp
```

For the metadata endpoint:
```bash
curl -X POST "http://localhost:8078/metadata" \
  -H "accept: application/json" \
  -H "Content-Type: multipart/form-data" \
  -F "image=@/path/to/your/image.jpg"
```

For the health endpoint:
```bash
curl -X GET "http://localhost:8078/health"
```

## Development
The app directory is mounted as a volume, so you can modify the code and see changes immediately without rebuilding the Docker image.
