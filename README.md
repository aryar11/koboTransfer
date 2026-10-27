# kobosend

A small self-hosted relay for getting EPUB/PDF files onto a Kobo using its built-in browser.

1. On a phone or PC, open the site. You get a 4-character code. Upload a book.
2. On the Kobo, open `https://books.example.com/get`, enter the code, and tap **Download**.

There are no accounts and no third-party services. An unused code rotates every 5 minutes. After an upload, the file stays on your volume for 24 hours (or, optionally, until the first download).

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Uploader page (creates a room, remembered by cookie) |
| POST | `/new` | Discard the current room and start a new code |
| POST | `/api/rooms/{code}/upload` | Multipart upload (`file` field); only the browser that owns the room can upload |
| GET | `/api/rooms/{code}/status` | JSON status for the uploader page (owner only) |
| GET/POST | `/get` | Kobo code-entry page; `?code=ABCD` redirects to `/r/ABCD` |
| GET | `/r/{code}` | Page with one plain download link |
| GET/HEAD | `/d/{code}/{safe_filename}` | The file itself |
| GET | `/healthz` | Health check |

The download URL always ends in `.epub` or `.pdf`, uses an ASCII-only filename, and is served with `Content-Type: application/epub+zip` or `application/pdf` and a plain `Content-Disposition: attachment; filename="..."` header (no `filename*=`). These are the things the Kobo browser needs before it will offer to save a file.

## Configuration

Copy `.env.example` to `.env` and adjust. All settings:

| Variable | Default | Notes |
|----------|---------|-------|
| `DATA_PATH` | `./data` | Host path mounted at `/data` (compose only) |
| `CODE_TTL_MINUTES` | `5` | An unused code rotates to a new one after this long |
| `ROOM_TTL_HOURS` | `24` | How long a code and its file live after an upload |
| `MAX_UPLOAD_MB` | `80` | Keep the proxy body limit above this |
| `ALLOWED_EXTENSIONS` | `.epub,.pdf,.kepub.epub` | |
| `DELETE_AFTER_DOWNLOAD` | `false` | Remove the file after the first complete download |
| `LOOKUP_RATE_LIMIT_PER_MIN` | `30` | Per client IP, across `/r/` and `/d/`; `0` disables |
| `CLEANUP_INTERVAL_SECONDS` | `300` | Expired-room sweep interval |
| `COOKIE_SECURE` | `false` | Mark the room cookie Secure on HTTPS requests (ignored on plain HTTP) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs whose `X-Forwarded-For` is trusted |

Data layout inside `/data`: `app.db` (SQLite metadata), `files/` (uploaded blobs, stored under random names), `tmp/` (upload spooling).

## Deploying on TrueNAS SCALE

1. **Create a dataset**, e.g. `tank/apps/kobosend`. Give the `apps` user (UID/GID 568) write access, or set `APP_UID`/`APP_GID` in `.env` to match whatever owns it.
2. **Get the code onto the NAS** (git clone or copy this folder), then:
   ```sh
   cp .env.example .env    # edit DATA_PATH etc.
   docker compose up -d --build
   ```
   Or build the image and create a TrueNAS Custom App with the same settings: mount the dataset at `/data`, run as 568:568, port 8080.
3. **Keep it off the public internet directly.** The compose file binds `127.0.0.1:8080` only. If your reverse proxy runs in another container, remove the `ports:` block and attach `kobosend` to the proxy's Docker network instead, then use `http://kobosend:8080` as the upstream.
4. **Point the reverse proxy at it.** Nginx example:
   ```nginx
   server {
       server_name books.example.com;
       # ... your existing TLS config ...
       client_max_body_size 100m;          # must exceed MAX_UPLOAD_MB
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_request_buffering off;    # stream large uploads
       }
   }
   ```
   Nginx Proxy Manager: add `client_max_body_size 100m;` under the host's Advanced tab. Traefik has no body limit by default. Caddy: `request_body { max_size 100MB }`.
5. **Set `FORWARDED_ALLOW_IPS`** to the IP (or CIDR, e.g. `172.16.0.0/12` for Docker networks) your proxy connects from, so rate limiting sees real client IPs. Set `COOKIE_SECURE=true` once HTTPS works.

Links in the pages are relative, so nothing needs to know the public hostname.

Optional extra hardening at the proxy: IP allowlist, Authelia/forward auth, or basic auth. Note that the Kobo browser handles basic auth but not most SSO login flows, so exempt `/get`, `/r/`, `/d/` if you add SSO.

## Security model

The 4-character code (31⁴ ≈ 920k combinations) is a short-lived secret. Guessing is limited by the per-IP rate limit, the TTL, and optionally `DELETE_AFTER_DOWNLOAD`. Only the browser that created a room (via an HttpOnly cookie) can upload to it. The container runs as non-root with a read-only root filesystem; only `/data` is writable. Access logs contain URLs (including codes and filenames) but never file contents.

## Local development

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

Data goes to `./data` unless `DATA_DIR` is set.

## Kobo test checklist

1. Phone: upload an EPUB; the page shows "Ready".
2. Desktop: open `/get`, enter the code, click Download; the file saves with the right name.
3. **Kobo** (the real test): Beta Features → Web Browser, open `/get`, enter code, tap Download, confirm the prompt. The book should appear in the library after Kobo processes it.
4. Repeat with a PDF, and with an original filename containing spaces/accents; the link should still be ASCII and end in `.pdf`/`.epub`.
5. An expired or wrong code shows a clear message; a file over `MAX_UPLOAD_MB` is rejected with a message.

If the Kobo browser fails to load the HTTPS site at all, update the Kobo firmware (older firmware lacks current root certificates), or test over plain HTTP on your LAN.
