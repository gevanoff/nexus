"""Image storage and URL generation for payload policy enforcement."""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Dict, Any

from app.config import S, logger


def store_image_and_get_url(b64_data: str, mime_type: str = "image/png") -> str:
    """Store a base64-encoded image and return a URL.
    
    This enforces the payload policy: images are stored on disk and
    served via URL rather than returned as large base64 blobs.
    
    Args:
        b64_data: Base64-encoded image data
        mime_type: MIME type of the image
        
    Returns:
        URL path to the stored image (relative to gateway)
    """
    try:
        # Decode and store
        image_bytes = base64.b64decode(b64_data)
        
        # Generate a content-addressed filename
        content_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        timestamp = int(time.time())
        
        # Determine file extension from MIME type
        ext = "png"
        if "jpeg" in mime_type or "jpg" in mime_type:
            ext = "jpg"
        elif "svg" in mime_type:
            ext = "svg"
        elif "webp" in mime_type:
            ext = "webp"
        
        filename = f"{timestamp}_{content_hash}.{ext}"
        
        # Store in configured directory
        image_dir = Path(getattr(S, "UI_IMAGE_DIR", "/var/lib/gateway/data/ui_images"))
        image_dir.mkdir(parents=True, exist_ok=True)
        
        image_path = image_dir / filename
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        
        # Return URL path (served by the gateway)
        # The UI routes already handle serving from UI_IMAGE_DIR.
        url_path = f"/ui/images/{filename}"

        public_base = (getattr(S, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if public_base:
            url = f"{public_base}{url_path}"
        else:
            url = url_path
        
        logger.debug(f"Stored image: {filename} ({len(image_bytes)} bytes)")
        return url
        
    except Exception as e:
        logger.error(f"Failed to store image: {e}")
        # Fallback: return a data URL (not ideal but better than failing)
        return f"data:{mime_type};base64,{b64_data}"


def convert_response_to_urls(response: Dict[str, Any]) -> Dict[str, Any]:
    """Convert base64 image data in response to URLs.
    
    This enforces the default payload policy: images are served via URL
    rather than base64 blobs.
    
    Args:
        response: OpenAI-style images response with b64_json data
        
    Returns:
        Modified response with URLs instead of base64 data
    """
    if not isinstance(response, dict):
        return response
    
    data = response.get("data")
    if not isinstance(data, list):
        return response
    
    # Determine MIME type from gateway metadata
    mime_type = "image/png"
    gateway_meta = response.get("_gateway", {})
    if isinstance(gateway_meta, dict):
        mime_type = gateway_meta.get("mime", "image/png")
    
    # Convert each b64_json to a URL
    new_data = []
    for item in data:
        if not isinstance(item, dict):
            new_data.append(item)
            continue
        
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            url = store_image_and_get_url(b64, mime_type)
            new_item = {k: v for k, v in item.items() if k != "b64_json"}
            new_item["url"] = url
            new_data.append(new_item)
        else:
            new_data.append(item)
    
    response["data"] = new_data
    return response
