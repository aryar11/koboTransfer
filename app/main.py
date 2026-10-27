import asyncio
import logging
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import kobo_filenames
from .cleanup import sweep_forever
from .config import BASE_DIR, load_settings
from .ratelimit import RateLimiter
from .rooms import Room, RoomNotFound, RoomStore, UploadTooLarge, normalize_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = load_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.tmp_dir.mkdir(parents=True, exist_ok=True)
# Multipart uploads spool to disk; the container's root FS (and /tmp) is read-only/small.
tempfile.tempdir = str(settings.tmp_dir)

store = RoomStore(settings.db_path, settings.files_dir, settings.room_ttl_seconds)
lookup_limiter = RateLimiter(settings.lookup_rate_limit_per_min)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

OWNER_COOKIE = "kobosend_room"
MULTIPART_OVERHEAD = 64 * 1024

UPLOAD_ERRORS = {
    "too_large": f"That file is larger than the {settings.max_upload_mb} MB limit.",
    "bad_type": "Only " + ", ".join(settings.allowed_extensions) + " files are accepted.",
    "no_file": "Choose a file first.",
    "expired": "That code expired. Here is a new one.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sweep_forever(store, settings.cleanup_interval_seconds))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="kobosend", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 KB"
    if num_bytes < 1024 * 1024:
        return f"{max(1, round(num_bytes / 1024))} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def human_remaining(expires_at: float) -> str:
    minutes = max(0, int((expires_at - time.time()) // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def room_from_cookie(request: Request) -> Room | None:
    raw = request.cookies.get(OWNER_COOKIE, "")
    code, _, token = raw.partition(":")
    code = normalize_code(code)
    if not code or not token:
        return None
    return store.get_owned(code, token)


def set_owner_cookie(response: Response, room: Room) -> None:
    response.set_cookie(
        OWNER_COOKIE,
        f"{room.code}:{room.owner_token}",
        max_age=max(60, int(room.expires_at - time.time())),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def status_payload(room: Room) -> dict:
    return {
        "code": room.code,
        "state": room.state,
        "name": room.original_name,
        "safe_name": room.safe_name,
        "size": human_size(room.size) if room.size else None,
        "downloads": room.download_count,
        "expires_in": human_remaining(room.expires_at),
    }


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


# --- Uploader (phone / PC) ---------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def uploader(request: Request, error: str | None = None):
    room = room_from_cookie(request) or store.create_room()
    response = templates.TemplateResponse(
        request,
        "upload.html",
        {
            "room": room,
            "status": status_payload(room),
            "error": UPLOAD_ERRORS.get(error or ""),
            "accept": ",".join(settings.allowed_extensions),
            "max_upload_mb": settings.max_upload_mb,
            "host": request.headers.get("host", request.url.netloc),
        },
    )
    set_owner_cookie(response, room)
    return response


@app.post("/new")
def new_room(request: Request):
    old = room_from_cookie(request)
    if old is not None:
        store.delete_room(old.code)
    room = store.create_room()
    response = RedirectResponse("/", status_code=303)
    set_owner_cookie(response, room)
    return response


def upload_error(request: Request, key: str, status_code: int) -> Response:
    if wants_json(request):
        return JSONResponse({"error": key, "message": UPLOAD_ERRORS[key]}, status_code=status_code)
    return RedirectResponse(f"/?error={key}", status_code=303)


@app.post("/api/rooms/{code}/upload")
async def upload(request: Request, code: str):
    norm = normalize_code(code)
    owner = room_from_cookie(request)
    if norm is None or owner is None or owner.code != norm:
        return upload_error(request, "expired", 404)

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_upload_bytes + MULTIPART_OVERHEAD:
        return upload_error(request, "too_large", 413)

    form = await request.form(max_files=1, max_fields=4)
    try:
        upload_file = form.get("file")
        if not isinstance(upload_file, UploadFile) or not upload_file.filename:
            return upload_error(request, "no_file", 400)

        original = kobo_filenames.basename(upload_file.filename)
        ext = kobo_filenames.match_extension(original, settings.allowed_extensions)
        if ext is None:
            return upload_error(request, "bad_type", 415)
        safe_name = kobo_filenames.safe_filename(original, ext)

        try:
            room = await run_in_threadpool(
                store.store_upload, norm, original, safe_name, upload_file.file, settings.max_upload_bytes
            )
        except UploadTooLarge:
            return upload_error(request, "too_large", 413)
        except RoomNotFound:
            return upload_error(request, "expired", 404)
    finally:
        await form.close()

    if wants_json(request):
        response: Response = JSONResponse(status_payload(room))
    else:
        response = RedirectResponse("/", status_code=303)
    set_owner_cookie(response, room)
    return response


@app.get("/api/rooms/{code}/status")
def room_status(request: Request, code: str):
    owner = room_from_cookie(request)
    if owner is None or owner.code != normalize_code(code):
        return JSONResponse({"state": "expired"}, status_code=404)
    return status_payload(owner)


# --- Receiver (Kobo) ---------------------------------------------------------


def receive_page(request: Request, message: str | None, status_code: int = 200, **extra) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "receive.html", {"message": message, **extra}, status_code=status_code
    )


def code_redirect(request: Request, raw: str | None) -> Response:
    if raw is None or raw.strip() == "":
        return receive_page(request, None)
    code = normalize_code(raw)
    if code is None:
        return receive_page(request, "Codes are 4 letters/digits, like K7QM.", 400, entered=raw[:8])
    return RedirectResponse(f"/r/{code}", status_code=303)


@app.get("/get", response_class=HTMLResponse)
def receive(request: Request, code: str | None = None):
    return code_redirect(request, code)


@app.post("/get", response_class=HTMLResponse)
async def receive_post(request: Request):
    form = await request.form(max_files=0, max_fields=2)
    raw = form.get("code")
    return code_redirect(request, raw if isinstance(raw, str) else None)


@app.get("/r/{code}", response_class=HTMLResponse)
def ready(request: Request, code: str):
    if not lookup_limiter.allow(client_ip(request)):
        return receive_page(request, "Too many attempts. Wait a minute and try again.", 429)

    norm = normalize_code(code)
    room = store.get_active(norm) if norm else None
    if room is None:
        return receive_page(request, "No book found for that code. It may have expired.", 404)
    if room.state == "consumed":
        return receive_page(request, "That book was already downloaded and has been removed.", 410)
    if room.state == "waiting":
        return receive_page(
            request, "Code found, but no book has been uploaded yet.", 200, retry_url=f"/r/{room.code}"
        )

    return templates.TemplateResponse(
        request,
        "ready.html",
        {
            "room": room,
            "download_url": f"/d/{room.code}/{room.safe_name}",
            "size": human_size(room.size),
            "expires_in": human_remaining(room.expires_at),
            "delete_after_download": settings.delete_after_download,
        },
    )


@app.api_route("/d/{code}/{safe_filename}", methods=["GET", "HEAD"])
def download(request: Request, code: str, safe_filename: str):
    if not lookup_limiter.allow(client_ip(request)):
        return PlainTextResponse("Too many attempts. Wait a minute and try again.", status_code=429)

    norm = normalize_code(code)
    room = store.get_active(norm) if norm else None
    if room is None or not room.has_file or room.safe_name != safe_filename:
        return PlainTextResponse("Not found or expired.", status_code=404)

    path = store.file_path(room)
    if not path.is_file():
        return PlainTextResponse("Not found or expired.", status_code=404)

    background = None
    if request.method == "GET":
        background = BackgroundTask(
            store.record_download, room.code, room.stored_name, settings.delete_after_download
        )

    ext = kobo_filenames.extension_of(room.safe_name)
    return FileResponse(
        path,
        media_type=kobo_filenames.media_type_for(ext),
        headers={
            # Plain ASCII filename only: Kobo's WebKit mishandles RFC 5987 filename*=.
            "Content-Disposition": f'attachment; filename="{room.safe_name}"',
            "Cache-Control": "no-store",
        },
        background=background,
    )


@app.get("/healthz")
def healthz():
    store.get_active("HEALTH")
    return {"status": "ok"}
