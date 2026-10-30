import cv2
import numpy as np

from db.models import PageType

# Filenames with a dash between numbers are spreads (e.g. pa1_001-002.jpg) —
# almost always illustrations.
SPREAD_PATTERN = "-"


def _decode(image_bytes: bytes) -> np.ndarray | None:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def classify_page(image_bytes: bytes, filename: str) -> PageType:
    """Classify a scan page as text, illustration, mixed, or other.

    `filename` is used only for the spread/jpg heuristics; the pixels come from
    `image_bytes` (read from the object store).
    """
    name = filename.rsplit(".", 1)[0].lower()
    suffix = ("." + filename.rsplit(".", 1)[1].lower()) if "." in filename else ""

    if SPREAD_PATTERN in name:
        return PageType.ILLUSTRATION

    img = _decode(image_bytes)
    if img is None:
        return PageType.OTHER

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    is_jpg = suffix in (".jpg", ".jpeg")

    if len(img.shape) == 3:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1].mean()
    else:
        saturation = 0.0

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    text_ratio = np.count_nonzero(binary) / (h * w)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) == 0:
        return PageType.OTHER

    areas = [cv2.contourArea(c) for c in contours]
    page_area = h * w
    large_contours = [a for a in areas if a > page_area * 0.05]

    if saturation > 40:
        if is_jpg:
            return PageType.ILLUSTRATION
        if text_ratio > 0.08:
            return PageType.MIXED
        return PageType.ILLUSTRATION

    if text_ratio < 0.03:
        return PageType.ILLUSTRATION if is_jpg else PageType.OTHER

    if text_ratio > 0.06 and len(large_contours) <= 2:
        return PageType.TEXT

    if 0.03 <= text_ratio <= 0.06:
        if len(large_contours) > 3:
            return PageType.ILLUSTRATION
        return PageType.MIXED

    if text_ratio > 0.06:
        return PageType.TEXT

    return PageType.MIXED
