"""
Image Processing API
"""
import os
from io import BytesIO
from typing import List, Optional
import math
import re

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from PIL import Image, ImageDraw, ImageFont, ExifTags
import pillow_avif

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
    opacity: Optional[float] = Form(0.3),
    density: Optional[int] = Form(20),  # Controls how many watermarks to place
    format: str = Form(...),
    quality: Optional[int] = Form(85),
):
    """
    Add a repeating text watermark across the image at 45 degrees
    
    - **image**: The image file to watermark
    - **text**: Text to use as watermark
    - **opacity**: Watermark opacity (0.0-1.0), default is 0.3
    - **density**: Controls watermark density (higher = more watermarks), default is 20
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
    
    try:
        # Read the uploaded image
        contents = await image.read()
        img = Image.open(BytesIO(contents))
        
        # Create a transparent overlay for the watermarks
        txt_overlay = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_overlay)
        
        # Calculate diagonal length of the image
        diagonal = int(math.sqrt(img.width**2 + img.height**2))
        
        # Calculate the size of the font based on the image size
        font_size = max(img.width, img.height) // 20
        
        # Use a default font
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except IOError:
            font = ImageFont.load_default()
        
        # Calculate the space between watermarks based on density
        spacing = diagonal // density
        
        # Calculate text size for positioning
        textbbox = draw.textbbox((0, 0), text, font=font)
        text_width = textbbox[2] - textbbox[0]
        text_height = textbbox[3] - textbbox[1]
        
        # Create multiple watermarks across the image
        for i in range(-diagonal, diagonal * 2, spacing):
            # Position text along the specified offset lines
            position = (i - text_width // 2, (i * img.height // img.width) - text_height // 2)
            
            # Draw the text at 45 degrees
            draw.text(position, text, fill=(0, 0, 0, int(255 * opacity)), font=font, angle=45)
        
        # Composite the overlay with the original image
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        watermarked = Image.alpha_composite(img, txt_overlay)
        
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
