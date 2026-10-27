"""Kobo's browser only saves downloads reliably when the URL path ends in the
book extension and the filename is plain ASCII, so every name we expose in a
URL or Content-Disposition header goes through here."""

import re
import unicodedata
from pathlib import PurePosixPath

MEDIA_TYPES = {
    ".epub": "application/epub+zip",
    ".kepub.epub": "application/epub+zip",
    ".pdf": "application/pdf",
}

MAX_STEM_LENGTH = 80


def match_extension(filename: str, allowed: tuple[str, ...]) -> str | None:
    lower = filename.lower()
    for ext in sorted(allowed, key=len, reverse=True):
        if lower.endswith(ext) and len(lower) > len(ext):
            return ext
    return None


def ascii_stem(stem: str) -> str:
    decomposed = unicodedata.normalize("NFKD", stem)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_only)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("._-")
    cleaned = cleaned[:MAX_STEM_LENGTH].rstrip("._-")
    return cleaned or "book"


def basename(client_filename: str) -> str:
    return PurePosixPath(client_filename.replace("\\", "/")).name


def safe_filename(original: str, ext: str) -> str:
    name = basename(original)
    return ascii_stem(name[: -len(ext)]) + ext


def media_type_for(ext: str) -> str:
    return MEDIA_TYPES.get(ext, "application/octet-stream")


def extension_of(safe_name: str) -> str:
    for ext in sorted(MEDIA_TYPES, key=len, reverse=True):
        if safe_name.lower().endswith(ext):
            return ext
    return ""
