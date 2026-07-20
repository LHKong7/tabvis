"""OCR fallback for non-multimodal models.

A text-only LLM cannot see an image. When the active model has no vision
(:func:`tabvis.utils.model.model.get_model_supports_vision`) tabvis extracts text from image blocks
with Tesseract and sends that text instead, so image content still reaches the model.

Cross-platform, layered engine — the first that works wins, so it runs even with only a binary:
  1. ``tesserocr``     in-process Cython binding to libtesseract (fastest; ``uv sync --extra ocr``).
  2. ``pytesseract``   the tesseract binary via Pillow (``pip install pytesseract pillow``).
  3. ``tesseract`` bin a plain subprocess to ``tesseract`` on PATH (no Python package, no Pillow).

Any one is enough. Install the engine itself per OS:
  macOS         ``brew install tesseract``       (``brew install tesseract-lang`` for more languages)
  Debian/Ubuntu ``apt-get install tesseract-ocr`` (``tesseract-ocr-chi-sim`` … for more languages)
  Fedora        ``dnf install tesseract``
  Windows       ``choco install tesseract``       (or the UB-Mannheim build; add it to PATH)

Knobs (read per call, like the rest of tabvis config):
  ``TABVIS_OCR_ENABLED``  0 disables the fallback (images become a short note instead).
  ``TABVIS_OCR_LANG``     Tesseract language(s), e.g. ``eng`` or ``eng+chi_sim``. Unavailable langs
                          are dropped with a warning (falling back to an installed one).
  ``TABVIS_OCR_ENGINE``   ``auto`` (default) | ``tesserocr`` | ``pytesseract`` | ``binary`` — force one.

Design notes:
- Tesseract/leptonica read encoded PNG/JPEG/etc. directly, so the binary + tesserocr paths need NO
  Pillow (which is not a tabvis dependency). Only the pytesseract path pulls Pillow.
- Availability + installed-language detection are memoized; OCR results are cached by content hash so
  a recurring screenshot in a long conversation is not re-OCR'd every turn.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from importlib.util import find_spec

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_defined_falsy

DEFAULT_LANG = "eng"

# media_type -> a suffix tesseract/leptonica recognizes from the file extension.
_MEDIA_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/tif": ".tiff",
}

OCR_UNAVAILABLE_NOTE = (
    "[image omitted — the active model is not multimodal and no OCR engine is installed. "
    "Install OCR: `uv sync --extra ocr` (or `pip install pytesseract pillow`), plus a tesseract "
    "binary (macOS `brew install tesseract`, Debian/Ubuntu `apt install tesseract-ocr`, Windows "
    "`choco install tesseract`).]"
)
OCR_EMPTY_NOTE = (
    "[image contained no machine-readable text (OCR found nothing). The active model has no vision, "
    "so purely visual content — photos, charts, diagrams — cannot be described.]"
)


def ocr_marker(text: str) -> str:
    """Wrap OCR-extracted text so the model knows it came from an image, not the user typing it."""
    return "[OCR text extracted from an image (the active model has no vision)]\n" + text


def ocr_enabled() -> bool:
    """OCR fallback is ON unless ``TABVIS_OCR_ENABLED`` is explicitly falsy."""
    return not is_env_defined_falsy(os.environ.get("TABVIS_OCR_ENABLED"))


# --- engine detection (memoized) ------------------------------------------------------------

_engine_lock = threading.Lock()
_engines_cache: list[str] | None = None


def _detect_engines() -> list[str]:
    forced = (os.environ.get("TABVIS_OCR_ENGINE") or "auto").strip().lower()
    have_tesserocr = find_spec("tesserocr") is not None
    # pytesseract imports PIL at import time, so both must be present for that path to work.
    have_pytesseract = find_spec("pytesseract") is not None and find_spec("PIL") is not None
    have_binary = shutil.which("tesseract") is not None

    if forced == "tesserocr":
        return ["tesserocr"] if have_tesserocr else []
    if forced == "pytesseract":
        return ["pytesseract"] if have_pytesseract else []
    if forced == "binary":
        return ["binary"] if have_binary else []

    engines: list[str] = []
    if have_tesserocr:
        engines.append("tesserocr")
    if have_pytesseract:
        engines.append("pytesseract")
    if have_binary:
        engines.append("binary")
    return engines


def available_engines() -> list[str]:
    """OCR engines usable right now, in preference order (memoized)."""
    global _engines_cache
    if _engines_cache is None:
        with _engine_lock:
            if _engines_cache is None:
                _engines_cache = _detect_engines()
    return _engines_cache


def ocr_available() -> bool:
    """True if any OCR engine is installed and usable."""
    return bool(available_engines())


# --- installed-language detection (memoized) ------------------------------------------------

_langs_cache: set[str] | None = None


def _installed_langs() -> set[str]:
    global _langs_cache
    if _langs_cache is not None:
        return _langs_cache
    langs: set[str] = set()
    try:
        if find_spec("tesserocr") is not None:
            import tesserocr  # type: ignore[import-not-found]

            langs = set(tesserocr.get_languages()[1])
        elif shutil.which("tesseract"):
            proc = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15
            )
            # First line is a header ("List of available languages ...").
            for line in proc.stdout.splitlines()[1:]:
                line = line.strip()
                if line:
                    langs.add(line)
    except Exception as e:  # noqa: BLE001 — detection is best-effort
        log_for_debugging(f"ocr: could not list tesseract languages: {e}")
    _langs_cache = langs
    return langs


def get_ocr_lang() -> str:
    """The effective tesseract language string, narrowed to what is actually installed."""
    requested = (os.environ.get("TABVIS_OCR_LANG") or DEFAULT_LANG).strip() or DEFAULT_LANG
    installed = _installed_langs()
    if not installed:  # detection failed — trust the request rather than block OCR
        return requested
    parts = [p for p in requested.split("+") if p]
    ok = [p for p in parts if p in installed]
    if ok:
        if len(ok) != len(parts):
            missing = set(parts) - set(ok)
            log_for_debugging(f"ocr: language(s) {missing} not installed; using {'+'.join(ok)}")
        return "+".join(ok)
    fallback = DEFAULT_LANG if DEFAULT_LANG in installed else sorted(installed)[0]
    log_for_debugging(f"ocr: requested language(s) {requested!r} not installed; falling back to {fallback!r}")
    return fallback


# --- the OCR call ---------------------------------------------------------------------------


def _run_engine(engine: str, path: str, lang: str) -> str | None:
    if engine == "tesserocr":
        import tesserocr  # type: ignore[import-not-found]

        with tesserocr.PyTessBaseAPI(lang=lang) as api:  # SetImageFile => leptonica decodes, no PIL
            api.SetImageFile(path)
            return api.GetUTF8Text()
    if engine == "pytesseract":
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(path) as im:
            return pytesseract.image_to_string(im, lang=lang)
    if engine == "binary":
        exe = shutil.which("tesseract")
        if not exe:
            return None
        proc = subprocess.run([exe, path, "stdout", "-l", lang], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip()[:200] or f"tesseract exit {proc.returncode}")
        return proc.stdout
    return None


def ocr_image_bytes(raw: bytes, media_type: str = "image/png", lang: str | None = None) -> str | None:
    """OCR raw image bytes. Returns the extracted text (possibly ``""`` if none found), or ``None``
    if no engine is available or every engine errored. Synchronous — call via ``asyncio.to_thread``.
    """
    engines = available_engines()
    if not engines or not raw:
        return None
    lang = lang or get_ocr_lang()
    ext = _MEDIA_EXT.get((media_type or "").lower(), ".png")
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="tabvis-ocr-")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        for engine in engines:
            try:
                text = _run_engine(engine, tmp_path, lang)
            except Exception as e:  # noqa: BLE001 — try the next engine
                log_for_debugging(f"ocr: engine {engine!r} failed: {e}")
                continue
            if text is not None:
                return text.strip()
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# --- content-hash cache (a recurring screenshot is OCR'd once, not every turn) ---------------

_cache_lock = threading.Lock()
_ocr_cache: "OrderedDict[tuple[str, str], str | None]" = OrderedDict()
_CACHE_MAX = 256


def ocr_image_base64(b64: str | None, media_type: str = "image/png", lang: str | None = None) -> str | None:
    """OCR a base64 image string, memoized by (content hash, lang). Synchronous."""
    if not b64:
        return None
    lang = lang or get_ocr_lang()
    key = (hashlib.sha1(b64.encode("utf-8", "ignore")).hexdigest()[:16], lang)
    with _cache_lock:
        if key in _ocr_cache:
            _ocr_cache.move_to_end(key)
            return _ocr_cache[key]
    import base64

    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001 — undecodable => no text
        return None
    result = ocr_image_bytes(raw, media_type, lang)
    with _cache_lock:
        _ocr_cache[key] = result
        _ocr_cache.move_to_end(key)
        while len(_ocr_cache) > _CACHE_MAX:
            _ocr_cache.popitem(last=False)
    return result


_warned = False


def warn_ocr_unavailable_once() -> None:
    """Log once when a non-vision model hits an image but no OCR engine is installed."""
    global _warned
    if not _warned:
        _warned = True
        log_for_debugging(
            "ocr: the active model has no vision and no OCR engine is installed; image content is "
            "being dropped. " + OCR_UNAVAILABLE_NOTE
        )


def _reset_caches_for_test() -> None:
    """Clear memoized detection + results (tests toggle env and installed engines)."""
    global _engines_cache, _langs_cache, _warned
    with _engine_lock:
        _engines_cache = None
    _langs_cache = None
    _warned = False
    with _cache_lock:
        _ocr_cache.clear()
