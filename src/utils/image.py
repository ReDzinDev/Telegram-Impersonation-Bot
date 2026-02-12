
import imagehash
from PIL import Image
from io import BytesIO
import requests
import logging

logger = logging.getLogger(__name__)

def get_image_as_pil(url: str) -> Image.Image:
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(BytesIO(response.content))
    except Exception as e:
        logger.error(f"Failed to fetch image from {url}: {e}")
        return None


def compute_pfp_hash_bytes(image_data: bytes) -> str:
    """
    Computes hash from image bytes.
    """
    if not image_data:
        return None
    try:
        img = Image.open(BytesIO(image_data))
        h = imagehash.phash(img)
        return str(h)
    except Exception as e:
        logger.error(f"Error computing hash from bytes: {e}")
        return None

def compute_pfp_hash(image_url: str) -> str:
    """
    Computes the perceptual hash of an image from a URL.
    Returns the hash as a hexadecimal string.
    """
    try:
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        return compute_pfp_hash_bytes(response.content)
    except Exception as e:
        logger.error(f"Error computing hash from URL {image_url}: {e}")
        return None


def check_pfp_similarity(target_hash_hex: str, stored_hashes: list[str], threshold: int = 10) -> tuple[bool, str, int]:
    """
    Checks if a target hash is similar to any stored hashes.
    Returns: (Match Found?, Matched Hash, Distance)
    Lower distance = more similar.
    """
    if not target_hash_hex:
        return False, None, 100

    target_hash = imagehash.hex_to_hash(target_hash_hex)
    
    best_match = None
    min_distance = 100 # Arbitrary high value
    
    for stored_hex in stored_hashes:
        if not stored_hex:
            continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hex)
            distance = target_hash - stored_hash
            
            if distance < min_distance:
                min_distance = distance
                best_match = stored_hex
        except ValueError: 
            continue # Invalid hash string in DB
            
    if min_distance <= threshold:
        return True, best_match, min_distance
        
    return False, None, min_distance
