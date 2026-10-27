import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_extensions(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip() or default
    exts = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        exts.append(part if part.startswith(".") else "." + part)
    return tuple(exts)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    code_ttl_minutes: float
    room_ttl_hours: float
    max_upload_mb: int
    allowed_extensions: tuple[str, ...]
    delete_after_download: bool
    lookup_rate_limit_per_min: int
    cleanup_interval_seconds: int
    cookie_secure: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def code_ttl_seconds(self) -> int:
        return int(self.code_ttl_minutes * 60)

    @property
    def room_ttl_seconds(self) -> int:
        return int(self.room_ttl_hours * 3600)


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))),
        code_ttl_minutes=_env_float("CODE_TTL_MINUTES", 5),
        room_ttl_hours=_env_float("ROOM_TTL_HOURS", 24),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 80),
        allowed_extensions=_env_extensions("ALLOWED_EXTENSIONS", ".epub,.pdf,.kepub.epub"),
        delete_after_download=_env_bool("DELETE_AFTER_DOWNLOAD", False),
        lookup_rate_limit_per_min=_env_int("LOOKUP_RATE_LIMIT_PER_MIN", 30),
        cleanup_interval_seconds=_env_int("CLEANUP_INTERVAL_SECONDS", 300),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
    )
