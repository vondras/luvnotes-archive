#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

import entrypoint

SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
EXIFTOOL_TIMEOUT_SECONDS = int(os.environ.get("EXIFTOOL_TIMEOUT_SECONDS", "90"))
START_AFTER = os.environ.get("PLAYGROUND_START_AFTER", "").strip()


def refresh_actor_token() -> str:
    refresh_token = os.environ.get("PLAYGROUND_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        raise RuntimeError("No Playground Firebase refresh token is available")

    response = requests.post(
        SECURE_TOKEN_URL,
        params={"key": entrypoint.FIREBASE_API_KEY},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=(15, 30),
    )
    if not response.ok:
        raise RuntimeError(
            f"Playground Firebase token refresh failed (HTTP {response.status_code}): "
            f"{response.text[:1000]}"
        )

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise RuntimeError("Playground Firebase token refresh returned no id_token")

    os.environ["PLAYGROUND_TOKEN"] = str(id_token)
    if payload.get("refresh_token"):
        os.environ["PLAYGROUND_REFRESH_TOKEN"] = str(payload["refresh_token"])

    return str(id_token)


def authenticate_if_needed() -> None:
    os.environ.setdefault("PLAYGROUND_BUNDLE_LOAD_TIME", entrypoint.BUNDLE_LOAD_TIME)

    if os.environ.get("PLAYGROUND_TOKEN"):
        return

    auth = entrypoint.authenticate()
    os.environ["PLAYGROUND_TOKEN"] = auth.id_token
    if auth.refresh_token:
        os.environ["PLAYGROUND_REFRESH_TOKEN"] = auth.refresh_token

    lifetime = f"; token lifetime {auth.expires_in}s" if auth.expires_in else ""
    print(
        "Authenticated LuvNotes -> Playground actor-scoped Firebase identity"
        f"{lifetime}; starting archive.",
        flush=True,
    )


def normalize_legacy_attachments(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize both Playground attachment dialects to the crawler contract.

    Newer responses provide `path`/`source` plus a URL. Older responses provide
    only `fileName` and a stable pg-image-cache URL. The underlying archiver uses
    `path` as its durable asset identity, so derive that path from the legacy URL
    (or, as a fallback, from school + fileName) without retaining URL secrets in
    persisted post metadata.
    """
    school_id = os.environ["PLAYGROUND_SCHOOL_ID"]

    for post in payload.get("data") or []:
        for attachment in post.get("attachments") or []:
            if attachment.get("path"):
                continue

            url = str(attachment.get("url") or "").strip()
            file_name = str(attachment.get("fileName") or "").strip()
            derived_path = ""
            source = ""

            if url:
                parsed = urlsplit(url)
                candidate = unquote(parsed.path).lstrip("/")
                if "/attachments/" in f"/{candidate}":
                    derived_path = candidate
                if parsed.hostname == "pg-image-cache.com":
                    source = "pg-image-cache"

            if not derived_path and file_name:
                derived_path = f"{school_id}/attachments/{file_name}"

            if derived_path:
                attachment["path"] = derived_path
                if source and not attachment.get("source"):
                    attachment["source"] = source

    return payload


def install_exiftool_guard(archive: Any) -> None:
    original_run = archive.subprocess.run

    def bounded_run(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        is_exiftool = (
            isinstance(command, (list, tuple))
            and bool(command)
            and Path(str(command[0])).name == "exiftool"
        )
        if is_exiftool:
            kwargs.setdefault("timeout", EXIFTOOL_TIMEOUT_SECONDS)
        try:
            return original_run(*args, **kwargs)
        except archive.subprocess.TimeoutExpired as exc:
            if is_exiftool:
                target = Path(str(command[-1])).name if command else "media file"
                raise RuntimeError(
                    f"ExifTool timed out after {EXIFTOOL_TIMEOUT_SECONDS}s for {target}"
                ) from None
            raise exc

    archive.subprocess.run = bounded_run

    original_download = archive.download_raw
    original_build = archive.build_photo_copy

    def timed_download(url: str, target: Path) -> None:
        started = time.monotonic()
        print(f"    download: {target.name}", flush=True)
        try:
            original_download(url, target)
        finally:
            print(
                f"    download finished in {time.monotonic() - started:.1f}s",
                flush=True,
            )

    def timed_build(
        raw: Path,
        target: Path,
        post: dict[str, Any],
        attachment: dict[str, Any],
        dt: Any,
    ) -> None:
        started = time.monotonic()
        print(f"    metadata: {target.name}", flush=True)
        try:
            original_build(raw, target, post, attachment, dt)
        finally:
            print(
                f"    metadata finished in {time.monotonic() - started:.1f}s",
                flush=True,
            )

    archive.download_raw = timed_download
    archive.build_photo_copy = timed_build


def run_archive() -> int:
    # luvnotes_archive reads PLAYGROUND_TOKEN at import time, so authenticate first.
    archive = importlib.import_module("luvnotes_archive")
    install_exiftool_guard(archive)

    def media_gallery_session() -> requests.Session:
        session = archive.retry_session()
        headers = entrypoint.playground_headers(os.environ["PLAYGROUND_TOKEN"])
        headers.update(
            {
                "guardianId": os.environ["PLAYGROUND_GUARDIAN_ID"],
                "screen": f"/app/{os.environ['PLAYGROUND_SCHOOL_ID']}/media-gallery",
            }
        )
        session.headers.update(headers)
        return session

    def request_page(
        api: requests.Session,
        params: dict[str, str],
    ) -> requests.Response:
        url = f"{archive.API_ROOT}/{archive.SCHOOL_ID}/posts"
        response = api.get(url, params=params, timeout=(15, 60))

        if response.status_code not in (401, 403):
            return response

        if not os.environ.get("PLAYGROUND_REFRESH_TOKEN"):
            return response

        print(
            f"Playground returned HTTP {response.status_code}; refreshing actor token and retrying page once.",
            flush=True,
        )
        new_token = refresh_actor_token()
        api.headers["Authorization"] = f"Bearer {new_token}"
        return api.get(url, params=params, timeout=(15, 60))

    def media_gallery_fetch_page(
        api: requests.Session,
        cursor: int | None,
        cursor_post_id: str | None,
    ) -> dict[str, Any]:
        params = {
            "limit": str(archive.PAGE_SIZE),
            "studentId": archive.STUDENT_ID,
            "mediaOnly": "true",
            "origin": "web",
            "bundleLoadTime": entrypoint.BUNDLE_LOAD_TIME,
        }

        if cursor is None:
            params["reset"] = "true"
            if START_AFTER:
                params["startAfter"] = START_AFTER
                print(
                    f"Starting media-gallery crawl at startAfter={START_AFTER}",
                    flush=True,
                )
        else:
            params["startAfter"] = str(cursor)
            if cursor_post_id:
                params["cursorPostId"] = cursor_post_id

        response = request_page(api, params)
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Playground posts request rejected (HTTP {response.status_code}): "
                f"{response.text[:1000]}"
            )
        response.raise_for_status()
        return normalize_legacy_attachments(response.json())

    archive.api_session = media_gallery_session
    archive.fetch_page = media_gallery_fetch_page
    return int(archive.main())


def main() -> int:
    authenticate_if_needed()
    return run_archive()


if __name__ == "__main__":
    raise SystemExit(main())
