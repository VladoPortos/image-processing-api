"""
Image Processing API
"""
import os
from io import BytesIO
from typing import List, Optional
import math
import re
import platform
import sys
import time
import base64

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from PIL import Image, ImageDraw, ImageFont, ExifTags
import pillow_avif

# Track API start time for uptime reporting
START_TIME = time.time()

app = FastAPI(
    title="Image Processing API",
    description="API for processing images with various formats and quality settings",
    version="0.1.0",
)

ALLOWED_FORMATS = ["avif", "webp", "png", "jpg", "jpeg"]

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "Image Processing API is running"}

@app.get("/health")
async def health_check():
    """
    Health check endpoint
    
    Returns information about the API's health and system information
    """
    uptime_seconds = time.time() - START_TIME
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    return {
        "status": "healthy",
        "version": "0.1.0",
        "uptime": {
            "days": int(days),
            "hours": int(hours),
            "minutes": int(minutes),
            "seconds": int(seconds),
            "total_seconds": int(uptime_seconds)
        },
        "system_info": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "pillow_version": Image.__version__,
            "supported_formats": ALLOWED_FORMATS
        }
    }

@app.post("/convert")
async def convert_image(
    image: UploadFile = File(...),
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Convert an image to the specified format with the given quality
    
    - **image**: The image file to convert
    - **format**: Target format (avif, webp, png, jpg)
    - **quality**: Quality setting (1-100), default is 85
    """
    # Validate format
    if format.lower() not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of {ALLOWED_FORMATS}")
    
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        # Convert the image
        output = BytesIO()
        
        if format.lower() in ["jpg", "jpeg"]:
            # JPG doesn't support alpha channel
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                img = background
            img.save(output, format="JPEG", quality=quality, optimize=True)
        elif format.lower() == "png":
            img.save(output, format="PNG", optimize=True)
        elif format.lower() == "webp":
            img.save(output, format="WEBP", quality=quality)
        elif format.lower() == "avif":
            img.save(output, format="AVIF", quality=quality)
        
        # Get original filename and replace extension
        original_filename = image.filename
        filename_base, _ = os.path.splitext(original_filename)
        new_filename = f"{filename_base}.{format.lower()}"
        
        # Return the converted image
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type=f"image/{format.lower()}",
            headers={"Content-Disposition": f"attachment; filename={new_filename}"}
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing error: {str(e)}")
 
@app.post("/convert/base64")
async def convert_image_base64(
    image_base64: str = Form(...),
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Convert a base64-encoded image to the specified format with the given quality
    
    - **image_base64**: Base64 string of the image. Accepts raw base64 or data URI
      (e.g., "data:image/png;base64,....")
    - **format**: Target format (avif, webp, png, jpg)
    - **quality**: Quality setting (1-100), default is 85
    """
    # Validate format
    if format.lower() not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of {ALLOWED_FORMATS}")
    
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    try:
        # Handle data URI prefix if present
        b64data = image_base64
        if b64data.startswith("data:"):
            try:
                b64data = b64data.split(",", 1)[1]
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid data URI for base64 image")
        
        # Remove whitespace/newlines before decoding
        b64data = re.sub(r"\s", "", b64data)
        try:
            contents = base64.b64decode(b64data)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 image data")
        
        img = Image.open(BytesIO(contents))
        
        # Convert the image
        output = BytesIO()
        
        if format.lower() in ["jpg", "jpeg"]:
            # JPG doesn't support alpha channel
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                img = background
            img.save(output, format="JPEG", quality=quality, optimize=True)
        elif format.lower() == "png":
            img.save(output, format="PNG", optimize=True)
        elif format.lower() == "webp":
            img.save(output, format="WEBP", quality=quality)
        elif format.lower() == "avif":
            img.save(output, format="AVIF", quality=quality)
        
        # Use a default filename as base64 input doesn't include one
        new_filename = f"converted.{format.lower()}"
        
        # Return the converted image
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type=f"image/{format.lower()}",
            headers={"Content-Disposition": f"attachment; filename={new_filename}"}
        )
    
    except HTTPException:
        # Re-raise HTTPExceptions directly
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing error: {str(e)}")
 
@app.post("/info")
async def image_info(
    image: UploadFile = File(...),
    quality: Optional[int] = Form(85),
):
    """
    Get information about an image converted to all supported formats with given quality
    
    - **image**: The image file to analyze
    - **quality**: Quality setting (1-100), default is 85
    
    Returns JSON with original size and size information for all supported formats
    """
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    try:
        # Read the uploaded image
        contents = await image.read()
        original_size = len(contents)
        img = Image.open(BytesIO(contents))
        
        # Results for all formats
        results = {
            "original": {
                "size_bytes": original_size,
                "size_human": f"{original_size / 1024:.2f} KB",
                "format": img.format,
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
            },
            "formats": {}
        }
        
        # Process each format
        for format in ["avif", "webp", "png", "jpg"]:
            output = BytesIO()
            
            if format.lower() in ["jpg", "jpeg"]:
                # JPG doesn't support alpha channel
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    # Create a temporary image for JPG conversion with white background
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                    temp_img = background
                else:
                    temp_img = img
                temp_img.save(output, format="JPEG", quality=quality, optimize=True)
            elif format.lower() == "png":
                img.save(output, format="PNG", optimize=True)
            elif format.lower() == "webp":
                img.save(output, format="WEBP", quality=quality)
            elif format.lower() == "avif":
                img.save(output, format="AVIF", quality=quality)
            
            # Calculate sizes
            output.seek(0)
            converted_size = len(output.getvalue())
            saved_bytes = original_size - converted_size
            saved_percentage = (saved_bytes / original_size) * 100 if original_size > 0 else 0
            
            results["formats"][format] = {
                "quality": quality,
                "size_bytes": converted_size,
                "size_human": f"{converted_size / 1024:.2f} KB",
                "savings": {
                    "bytes": saved_bytes,
                    "percentage": f"{saved_percentage:.2f}%",
                }
            }
            
            # Explicitly clear the BytesIO object to free memory
            output.close()
        
        # Make sure to explicitly delete temporary variables to free memory
        del contents
        del img
        
        return results
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing error: {str(e)}")

@app.post("/metadata")
async def extract_metadata(
    image: UploadFile = File(...),
):
    """
    Extract metadata from an image
    
    - **image**: The image file to analyze
    
    Returns JSON with all available metadata
    """
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        # Basic image info
        metadata = {
            "filename": image.filename,
            "format": img.format,
            "mode": img.mode,
            "size": {
                "width": img.width,
                "height": img.height,
            },
            "info": {}
        }
        
        # Add image info dictionary
        for key, value in img.info.items():
            if isinstance(value, (str, int, float, bool, list, dict)) and key != "exif":
                metadata["info"][key] = value
        
        # Extract EXIF data if available
        exif_data = {}
        if hasattr(img, '_getexif') and img._getexif() is not None:
            exif = img._getexif()
            for tag_id, value in exif.items():
                tag = ExifTags.TAGS.get(tag_id, tag_id)
                # Handle binary data and other non-serializable types
                if isinstance(value, (bytes, bytearray)):
                    value = "binary data"
                elif not isinstance(value, (str, int, float, bool, list, dict, tuple)) or isinstance(value, tuple) and not all(isinstance(i, (str, int, float, bool)) for i in value):
                    try:
                        value = str(value)
                    except:
                        value = "unserializable data"
                exif_data[tag] = value
                
            metadata["exif"] = exif_data
        
        # Make sure to explicitly delete temporary variables to free memory
        del contents
        del img
        
        return metadata
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Metadata extraction error: {str(e)}")

@app.post("/watermark")
async def add_watermark(
    image: UploadFile = File(...),
    text: str = Form(...),
    opacity: Optional[float] = Form(0.5),
    density: Optional[int] = Form(15),
    font_size: Optional[int] = Form(None),
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Add a repeating text watermark across the image with horizontal lines rotated 45 degrees
    
    - **image**: The image file to watermark
    - **text**: Text to use as watermark
    - **opacity**: Watermark opacity (0.0-1.0), default is 0.5
    - **density**: Controls watermark density (1-50, higher = more watermarks), default is 15
    - **font_size**: Custom font size in pixels, if not provided, calculated automatically based on image size
    - **format**: Output format (avif, webp, png, jpg)
    - **quality**: Quality setting (1-100), default is 85
    """
    # Validate format
    if format.lower() not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of {ALLOWED_FORMATS}")
    
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    # Validate opacity
    if not 0.0 <= opacity <= 1.0:
        raise HTTPException(status_code=400, detail="Opacity must be between 0.0 and 1.0")
    
    # Validate density
    if not 1 <= density <= 50:
        raise HTTPException(status_code=400, detail="Density must be between 1 and 50")
    
    # Validate font size if provided
    if font_size is not None and font_size <= 0:
        raise HTTPException(status_code=400, detail="Font size must be greater than 0")
    
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        # Create a canvas larger than the image to account for rotation
        # The canvas needs to be large enough so when rotated, it still covers the entire image
        diagonal = int(math.sqrt(img.width**2 + img.height**2))
        canvas_size = (diagonal * 2, diagonal * 2)
        
        # Create a transparent canvas
        canvas = Image.new('RGBA', canvas_size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(canvas)
        
        # Use custom font size if provided, otherwise calculate based on image size
        if font_size is None:
            font_size = max(img.width, img.height) // 20
        
        print(f"Using font size: {font_size}")  # Debug output
        
        # Try multiple fonts in order
        font = None
        for font_name in ["LiberationSans-Regular", "DejaVuSans", "arial"]:
            try:
                font = ImageFont.truetype(f"{font_name}.ttf", size=font_size)
                print(f"Successfully loaded font: {font_name}")
                break
            except IOError:
                continue
        
        # If all truetype fonts fail, use the default font as fallback
        if font is None:
            print("Warning: Using default font, size control may be limited")
            font = ImageFont.load_default()
            
        # Calculate text size for positioning
        textbbox = draw.textbbox((0, 0), text, font=font)
        text_width = textbbox[2] - textbbox[0]
        text_height = textbbox[3] - textbbox[1]
        
        # Calculate spacing based on density - higher density means smaller gaps
        # Inverse relationship: spacing_factor = base_spacing / density
        base_spacing = 45  # Base value for spacing
        spacing_factor = base_spacing / density
        
        # Calculate gaps - ensure they're never smaller than the text itself
        horizontal_gap = max(text_width * spacing_factor, text_width * 1.2)
        vertical_gap = max(text_height * spacing_factor, text_height * 1.2)
        
        # Draw horizontal lines of text across the canvas
        start_y = 0
        while start_y < canvas_size[1]:
            # Offset every other line for a more distributed pattern
            offset = horizontal_gap / 2 if (start_y // vertical_gap) % 2 == 0 else 0
            start_x = offset
            
            while start_x < canvas_size[0]:
                draw.text((start_x, start_y), text, fill=(255, 255, 255, int(255 * opacity)), font=font)
                start_x += horizontal_gap
            
            start_y += vertical_gap
        
        # Rotate the canvas by 45 degrees
        rotated_canvas = canvas.rotate(45, resample=Image.BICUBIC, expand=False)
        
        # Calculate the position to paste the rotated canvas so it's centered on the image
        paste_position = (
            (img.width - rotated_canvas.width) // 2 + diagonal // 2,
            (img.height - rotated_canvas.height) // 2 + diagonal // 2
        )
        
        # Create a final transparent overlay the size of the original image
        overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
        
        # Crop the relevant part of the rotated canvas and paste it onto the overlay
        region = rotated_canvas.crop((
            diagonal - img.width // 2,
            diagonal - img.height // 2,
            diagonal + img.width // 2,
            diagonal + img.height // 2
        ))
        overlay.paste(region, (0, 0), region)
        
        # Composite the overlay with the original image
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        watermarked = Image.alpha_composite(img, overlay)
        
        # Convert back to original mode if needed for the output format
        if format.lower() in ["jpg", "jpeg"]:
            watermarked = watermarked.convert('RGB')
        
        # Save the watermarked image
        output = BytesIO()
        
        if format.lower() in ["jpg", "jpeg"]:
            watermarked.save(output, format="JPEG", quality=quality, optimize=True)
        elif format.lower() == "png":
            watermarked.save(output, format="PNG", optimize=True)
        elif format.lower() == "webp":
            watermarked.save(output, format="WEBP", quality=quality)
        elif format.lower() == "avif":
            watermarked.save(output, format="AVIF", quality=quality)
        
        # Get original filename and replace extension
        original_filename = image.filename
        filename_base, _ = os.path.splitext(original_filename)
        new_filename = f"{filename_base}_watermarked.{format.lower()}"
        
        # Return the watermarked image
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type=f"image/{format.lower()}",
            headers={"Content-Disposition": f"attachment; filename={new_filename}"}
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Watermarking error: {str(e)}")

@app.post("/resize")
async def resize_image(
    image: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    percentage: Optional[float] = Form(None),
    maintain_aspect_ratio: Optional[bool] = Form(True),
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Resize an image to the specified dimensions or by percentage
    
    - **image**: The image file to resize
    - **width**: Target width in pixels (optional)
    - **height**: Target height in pixels (optional)
    - **percentage**: Scale by percentage, e.g., 50 for half size (optional)
    - **maintain_aspect_ratio**: Whether to maintain aspect ratio, default is True
    - **format**: Output format (avif, webp, png, jpg)
    - **quality**: Quality setting (1-100), default is 85
    """
    # Validate format
    if format.lower() not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of {ALLOWED_FORMATS}")
    
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    # Validate resize parameters - at least one must be provided
    if width is None and height is None and percentage is None:
        raise HTTPException(
            status_code=400, 
            detail="At least one of width, height, or percentage must be provided"
        )
    
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        original_width, original_height = img.size
        
        # Calculate new dimensions
        if percentage is not None:
            # Resize by percentage
            new_width = int(original_width * percentage / 100)
            new_height = int(original_height * percentage / 100)
        elif width is not None and height is not None:
            # Use both width and height
            if maintain_aspect_ratio:
                # Calculate which dimension to fit within while maintaining aspect ratio
                width_ratio = width / original_width
                height_ratio = height / original_height
                ratio = min(width_ratio, height_ratio)
                new_width = int(original_width * ratio)
                new_height = int(original_height * ratio)
            else:
                # Use exact dimensions
                new_width = width
                new_height = height
        elif width is not None:
            # Only width specified, maintain aspect ratio
            ratio = width / original_width
            new_width = width
            new_height = int(original_height * ratio) if maintain_aspect_ratio else original_height
        elif height is not None:
            # Only height specified, maintain aspect ratio
            ratio = height / original_height
            new_height = height
            new_width = int(original_width * ratio) if maintain_aspect_ratio else original_width
        
        # Apply resize
        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Save the resized image
        output = BytesIO()
        
        if format.lower() in ["jpg", "jpeg"]:
            # JPG doesn't support alpha channel
            if resized_img.mode in ('RGBA', 'LA') or (resized_img.mode == 'P' and 'transparency' in resized_img.info):
                background = Image.new('RGB', resized_img.size, (255, 255, 255))
                background.paste(resized_img, mask=resized_img.split()[3] if resized_img.mode == 'RGBA' else None)
                resized_img = background
            resized_img.save(output, format="JPEG", quality=quality, optimize=True)
        elif format.lower() == "png":
            resized_img.save(output, format="PNG", optimize=True)
        elif format.lower() == "webp":
            resized_img.save(output, format="WEBP", quality=quality)
        elif format.lower() == "avif":
            resized_img.save(output, format="AVIF", quality=quality)
        
        # Get original filename and replace extension
        original_filename = image.filename
        filename_base, _ = os.path.splitext(original_filename)
        new_filename = f"{filename_base}_resized.{format.lower()}"
        
        # Return the resized image
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type=f"image/{format.lower()}",
            headers={"Content-Disposition": f"attachment; filename={new_filename}"}
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resize error: {str(e)}")

@app.post("/crop")
async def crop_image(
    image: UploadFile = File(...),
    left: int = Form(...),
    top: int = Form(...),
    right: int = Form(...),
    bottom: int = Form(...),
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Crop an image to the specified region
    
    - **image**: The image file to crop
    - **left**: Left coordinate for cropping
    - **top**: Top coordinate for cropping
    - **right**: Right coordinate for cropping
    - **bottom**: Bottom coordinate for cropping
    - **format**: Output format (avif, webp, png, jpg)
    - **quality**: Quality setting (1-100), default is 85
    """
    # Validate format
    if format.lower() not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Format must be one of {ALLOWED_FORMATS}")
    
    # Validate quality
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=400, detail="Quality must be between 1 and 100")
    
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        # Validate crop coordinates
        if left < 0 or top < 0 or right > img.width or bottom > img.height or left >= right or top >= bottom:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid crop coordinates. Image dimensions are {img.width}x{img.height}."
            )
        
        # Apply the crop
        cropped_img = img.crop((left, top, right, bottom))
        
        # Save the cropped image
        output = BytesIO()
        
        if format.lower() in ["jpg", "jpeg"]:
            # JPG doesn't support alpha channel
            if cropped_img.mode in ('RGBA', 'LA') or (cropped_img.mode == 'P' and 'transparency' in cropped_img.info):
                background = Image.new('RGB', cropped_img.size, (255, 255, 255))
                background.paste(cropped_img, mask=cropped_img.split()[3] if cropped_img.mode == 'RGBA' else None)
                cropped_img = background
            cropped_img.save(output, format="JPEG", quality=quality, optimize=True)
        elif format.lower() == "png":
            cropped_img.save(output, format="PNG", optimize=True)
        elif format.lower() == "webp":
            cropped_img.save(output, format="WEBP", quality=quality)
        elif format.lower() == "avif":
            cropped_img.save(output, format="AVIF", quality=quality)
        
        # Get original filename and replace extension
        original_filename = image.filename
        filename_base, _ = os.path.splitext(original_filename)
        new_filename = f"{filename_base}_cropped.{format.lower()}"
        
        # Return the cropped image
        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type=f"image/{format.lower()}",
            headers={"Content-Disposition": f"attachment; filename={new_filename}"}
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Crop error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
