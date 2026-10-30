"""Local OCR for the `ocr_text` engine: detect vertical Japanese text columns
with OpenCV, then read each with local OCR in a batched forward pass.

local OCR (and its torch dependency) is imported lazily so the app boots and
the `vision` engine works even when these heavy packages aren't installed.
"""
from __future__ import annotations

from io import BytesIO

import cv2
import numpy as np
from PIL import Image

from logging_config import get_logger

log = get_logger("ocr")

_mocr = None          # manga_ocr.MangaOcr singleton
_use_fp16 = False


def _get_ocr():
    global _mocr, _use_fp16
    if _mocr is None:
        try:
            import torch
            from manga_ocr import MangaOcr
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "The ocr_text engine needs local OCR + torch installed "
                "(uncomment them in requirements.txt). Use the 'vision' engine "
                f"if you can't install them. Original error: {e}"
            )
        _mocr = MangaOcr()
        if torch.cuda.is_available():
            _mocr.model.half()
            _use_fp16 = True
    return _mocr


def unload_ocr() -> None:
    """Free VRAM held by the OCR model so the translator LLM (if local/GPU) has room."""
    global _mocr
    if _mocr is None:
        return
    _mocr = None
    try:
        import gc

        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def _ocr_batch(mocr, pil_images: list[Image.Image]) -> list[str]:
    import torch
    from manga_ocr.ocr import post_process

    if not pil_images:
        return []
    processed = [img.convert("L").convert("RGB") for img in pil_images]
    pixel_values = mocr.processor(processed, return_tensors="pt").pixel_values
    pixel_values = pixel_values.to(mocr.model.device)
    if _use_fp16:
        pixel_values = pixel_values.half()
    with torch.inference_mode():
        out = mocr.model.generate(pixel_values, max_length=300)
    texts = []
    for ids in out.cpu():
        decoded = mocr.tokenizer.decode(ids, skip_special_tokens=True)
        texts.append(post_process(decoded))
    return texts


def detect_text_columns(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect vertical text columns; returns (x, y, w, h) boxes right-to-left."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h_img, w_img = binary.shape[:2]

    kernel_height = max(h_img // 15, 30)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_height))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 1))
    dilated = cv2.dilate(closed, kernel_h, iterations=2)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    columns = []
    min_height = h_img * 0.15
    max_width = w_img * 0.15
    min_width = 5
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < min_height or w < min_width:
            continue
        if w > max_width and h / w < 2:
            continue
        if w < 8 and h < h_img * 0.3:
            continue
        columns.append((x, y, w, h))

    columns.sort(key=lambda c: c[0], reverse=True)  # right-to-left

    merged = []
    for col in columns:
        if merged:
            prev = merged[-1]
            if abs(col[0] - prev[0]) < prev[2] * 0.5:
                x = min(col[0], prev[0])
                y = min(col[1], prev[1])
                x2 = max(col[0] + col[2], prev[0] + prev[2])
                y2 = max(col[1] + col[3], prev[1] + prev[3])
                merged[-1] = (x, y, x2 - x, y2 - y)
                continue
        merged.append(col)
    return merged


def ocr_page(image_bytes: bytes) -> list[dict]:
    """Run OCR on one page image (bytes). Returns region dicts with percentage
    bboxes: {id, order, x, y, width, height, japanese_text, confidence}."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return []

    h_img, w_img = image.shape[:2]
    mocr = _get_ocr()
    columns = detect_text_columns(image)

    if not columns:
        text = _ocr_batch(mocr, [Image.open(BytesIO(image_bytes))])[0].strip()
        if text:
            return [{
                "id": "full_page", "order": 0,
                "x": 0, "y": 0, "width": 100, "height": 100,
                "japanese_text": text, "confidence": 80.0,
            }]
        return []

    crops_meta = []
    pil_crops = []
    for i, (x, y, w, h) in enumerate(columns):
        pad = 5
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(w_img, x + w + pad), min(h_img, y + h + pad)
        crop_rgb = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        pil_crops.append(Image.fromarray(crop_rgb))
        crops_meta.append((i, x1, y1, x2, y2))

    texts = _ocr_batch(mocr, pil_crops)

    regions = []
    for (i, x1, y1, x2, y2), text in zip(crops_meta, texts):
        text = text.strip()
        if not text:
            continue
        regions.append({
            "id": f"col_{i}", "order": i,
            "x": (x1 / w_img) * 100, "y": (y1 / h_img) * 100,
            "width": ((x2 - x1) / w_img) * 100, "height": ((y2 - y1) / h_img) * 100,
            "japanese_text": text, "confidence": 80.0,
        })
    return regions
