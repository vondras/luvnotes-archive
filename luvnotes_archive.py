#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_ROOT = "https://api.tryplayground.com/api"

TOKEN = os.environ["PLAYGROUND_TOKEN"]
SCHOOL_ID = os.environ["PLAYGROUND_SCHOOL_ID"]
STUDENT_ID = os.environ["PLAYGROUND_STUDENT_ID"]
GUARDIAN_ID = os.environ["PLAYGROUND_GUARDIAN_ID"]

ARCHIVE_ROOT = Path(os.environ.get("ARCHIVE_ROOT", "/archive"))
PHOTOS_ROOT = Path(os.environ.get("PHOTOS_ROOT", "/photos"))
LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "50"))

RAW_ROOT = ARCHIVE_ROOT / "raw"
DB_PATH = ARCHIVE_ROOT / "luvnotes.sqlite3"
POSTS_JSONL = ARCHIVE_ROOT / "posts.jsonl"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}


def retry_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def api_session() -> requests.Session:
    s = retry_session()
    s.headers.update({
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "Guardianid": GUARDIAN_ID,
        "Origin": "https://luvnotes.littlesunshine.com",
        "Referer": "https://luvnotes.littlesunshine.com/",
    })
    return s


# Keep R2 requests isolated so Playground auth is never forwarded to Cloudflare.
ASSET_SESSION = retry_session()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    PHOTOS_ROOT.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS posts (
        post_id TEXT PRIMARY KEY,
        timestamp_ms INTEGER,
        post_type TEXT,
        caption TEXT,
        author TEXT,
        json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS assets (
        object_path TEXT PRIMARY KEY,
        post_id TEXT,
        media_type TEXT,
        file_name TEXT,
        source TEXT,
        declared_width INTEGER,
        declared_height INTEGER,
        downloaded_width INTEGER,
        downloaded_height INTEGER,
        canonical_taken_at TEXT,
        canonical_taken_at_source TEXT,
        raw_path TEXT,
        photo_path TEXT,
        source_sha256 TEXT,
        enriched_sha256 TEXT,
        bytes INTEGER,
        status TEXT NOT NULL,
        error TEXT,
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        pages INTEGER NOT NULL DEFAULT 0,
        posts_seen INTEGER NOT NULL DEFAULT 0,
        assets_seen INTEGER NOT NULL DEFAULT 0,
        assets_downloaded INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL
    );
    """)
    db.commit()
    return db


def sanitize_post(post: dict[str, Any]) -> dict[str, Any]:
    clean = json.loads(json.dumps(post))
    for attachment in clean.get("attachments") or []:
        attachment.pop("url", None)
        attachment.pop("thumbnail", None)
    return clean


def author_name(post: dict[str, Any]) -> str:
    return str((post.get("author") or {}).get("name") or "").strip()


def upsert_post(db: sqlite3.Connection, post: dict[str, Any]) -> bool:
    post_id = post.get("postId")
    if not post_id:
        return False

    existed = db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone() is not None
    ts = now_iso()
    db.execute("""
        INSERT INTO posts (
            post_id, timestamp_ms, post_type, caption, author, json,
            first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            timestamp_ms = excluded.timestamp_ms,
            post_type = excluded.post_type,
            caption = excluded.caption,
            author = excluded.author,
            json = excluded.json,
            last_seen_at = excluded.last_seen_at
    """, (
        post_id,
        post.get("timestamp"),
        post.get("postType"),
        post.get("text") or "",
        author_name(post),
        json.dumps(sanitize_post(post), ensure_ascii=False, separators=(",", ":")),
        ts,
        ts,
    ))
    return not existed


def append_post_jsonl(post: dict[str, Any]) -> None:
    with POSTS_JSONL.open("a", encoding="utf-8") as f:
        json.dump(sanitize_post(post), f, ensure_ascii=False)
        f.write("\n")


def safe_component(value: str, fallback: str = "asset") -> str:
    value = value.strip().replace("/", "_").replace("\\", "_")
    value = re.sub(r"[\x00-\x1f\x7f]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def canonical_datetime(post: dict[str, Any], attachment: dict[str, Any]) -> tuple[datetime, str]:
    ms = post.get("timestamp")
    source = "playground.post.timestamp"
    if not isinstance(ms, (int, float)):
        ms = attachment.get("created")
        source = "playground.attachment.created"
    if not isinstance(ms, (int, float)):
        return datetime.now(LOCAL_TZ), "archive.import_time"
    return datetime.fromtimestamp(ms / 1000.0, tz=LOCAL_TZ), source


def target_name(post: dict[str, Any], attachment: dict[str, Any], dt: datetime) -> str:
    original = (
        attachment.get("fileName")
        or Path(str(attachment.get("path") or "")).name
        or "asset"
    )
    original = safe_component(str(original))
    post_id = safe_component(str(post.get("postId") or "unknown"))[:12]
    return f"{dt:%Y-%m-%d_%H%M%S}_{post_id}_{original}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def exiftool_json(path: Path) -> dict[str, Any]:
    p = subprocess.run(
        [
            "exiftool", "-j",
            "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
            "-ImageWidth", "-ImageHeight",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(p.stdout)
    return rows[0] if rows else {}


def dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        meta = exiftool_json(path)
        w, h = meta.get("ImageWidth"), meta.get("ImageHeight")
        return (int(w) if w is not None else None, int(h) if h is not None else None)
    except Exception:
        return None, None


def has_capture_timestamp(path: Path) -> bool:
    try:
        meta = exiftool_json(path)
        for key in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
            value = str(meta.get(key) or "").strip()
            if value and not value.startswith("0000:00:00"):
                return True
    except Exception:
        pass
    return False


def media_kind(attachment: dict[str, Any], path: Path) -> str:
    declared = str(attachment.get("type") or "").lower()
    if declared in {"image", "photo"}:
        return "image"
    if declared == "video":
        return "video"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    return declared or "unknown"


def enrich_metadata(
    path: Path,
    kind: str,
    dt: datetime,
    caption: str,
    author: str,
    post_id: str,
) -> None:
    cmd = ["exiftool", "-overwrite_original"]

    # Don't overwrite genuine surviving capture metadata.
    if not has_capture_timestamp(path):
        if kind == "image":
            local = dt.astimezone(LOCAL_TZ)
            stamp = local.strftime("%Y:%m:%d %H:%M:%S")
            offset = local.strftime("%z")
            offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
            cmd += [
                f"-DateTimeOriginal={stamp}",
                f"-CreateDate={stamp}",
                f"-ModifyDate={stamp}",
                f"-OffsetTimeOriginal={offset}",
                f"-OffsetTimeDigitized={offset}",
                f"-OffsetTime={offset}",
            ]
        elif kind == "video":
            utc = dt.astimezone(timezone.utc)
            stamp = utc.strftime("%Y:%m:%d %H:%M:%S")
            cmd += [
                f"-QuickTime:CreateDate={stamp}",
                f"-QuickTime:ModifyDate={stamp}",
                f"-TrackCreateDate={stamp}",
                f"-TrackModifyDate={stamp}",
                f"-MediaCreateDate={stamp}",
                f"-MediaModifyDate={stamp}",
            ]

    if caption:
        cmd += [f"-XMP-dc:Description={caption}", f"-IPTC:Caption-Abstract={caption}"]
    if author:
        cmd += [f"-XMP-dc:Creator={author}"]

    cmd += [
        "-XMP-dc:Source=Playground / LuvNotes",
        f"-XMP-dc:Identifier={post_id}",
        "-XMP-xmp:Label=LuvNotes",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    epoch = dt.timestamp()
    os.utime(path, (epoch, epoch))


def download_raw(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.unlink(missing_ok=True)

    with ASSET_SESSION.get(url, stream=True, timeout=(15, 180)) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    os.replace(tmp, target)


def build_photo_copy(
    raw: Path,
    target: Path,
    post: dict[str, Any],
    attachment: dict[str, Any],
    dt: datetime,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.unlink(missing_ok=True)
    shutil.copy2(raw, tmp)

    enrich_metadata(
        tmp,
        media_kind(attachment, tmp),
        dt,
        str(post.get("text") or "").strip(),
        author_name(post),
        str(post.get("postId") or ""),
    )

    # Synology Photos sees only the final, fully-written file.
    os.replace(tmp, target)


def complete(db: sqlite3.Connection, object_path: str) -> bool:
    row = db.execute(
        "SELECT status, raw_path, photo_path FROM assets WHERE object_path = ?",
        (object_path,),
    ).fetchone()
    if not row or row["status"] != "complete":
        return False
    return (
        bool(row["raw_path"]) and Path(row["raw_path"]).exists()
        and bool(row["photo_path"]) and Path(row["photo_path"]).exists()
    )


def record_error(
    db: sqlite3.Connection,
    post: dict[str, Any],
    attachment: dict[str, Any],
    exc: Exception,
) -> None:
    object_path = str(attachment.get("path") or "")
    if not object_path:
        return
    ts = now_iso()
    db.execute("""
        INSERT INTO assets (
            object_path, post_id, media_type, file_name, source,
            declared_width, declared_height,
            status, error, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'error', ?, ?, ?)
        ON CONFLICT(object_path) DO UPDATE SET
            status = 'error',
            error = excluded.error,
            updated_at = excluded.updated_at
    """, (
        object_path,
        post.get("postId"),
        attachment.get("type"),
        attachment.get("fileName"),
        attachment.get("source"),
        attachment.get("width"),
        attachment.get("height"),
        str(exc)[:2000],
        ts,
        ts,
    ))
    db.commit()


def process_asset(
    db: sqlite3.Connection,
    post: dict[str, Any],
    attachment: dict[str, Any],
) -> bool:
    object_path = str(attachment.get("path") or "").strip()
    signed_url = str(attachment.get("url") or "").strip()
    if not object_path or not signed_url:
        return False

    if complete(db, object_path):
        print(f"  ✓ {object_path}")
        return False

    dt, dt_source = canonical_datetime(post, attachment)
    year, month = dt.strftime("%Y"), dt.strftime("%m")
    filename = target_name(post, attachment, dt)

    raw = RAW_ROOT / year / month / filename
    photo = PHOTOS_ROOT / year / month / filename

    print(f"  ↓ {object_path}")

    if not raw.exists():
        download_raw(signed_url, raw)

    source_sha = sha256_file(raw)
    width, height = dimensions(raw)

    if not photo.exists():
        build_photo_copy(raw, photo, post, attachment, dt)

    enriched_sha = sha256_file(photo)
    ts = now_iso()

    db.execute("""
        INSERT INTO assets (
            object_path, post_id, media_type, file_name, source,
            declared_width, declared_height,
            downloaded_width, downloaded_height,
            canonical_taken_at, canonical_taken_at_source,
            raw_path, photo_path,
            source_sha256, enriched_sha256, bytes,
            status, error, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', NULL, ?, ?)
        ON CONFLICT(object_path) DO UPDATE SET
            post_id = excluded.post_id,
            media_type = excluded.media_type,
            file_name = excluded.file_name,
            source = excluded.source,
            declared_width = excluded.declared_width,
            declared_height = excluded.declared_height,
            downloaded_width = excluded.downloaded_width,
            downloaded_height = excluded.downloaded_height,
            canonical_taken_at = excluded.canonical_taken_at,
            canonical_taken_at_source = excluded.canonical_taken_at_source,
            raw_path = excluded.raw_path,
            photo_path = excluded.photo_path,
            source_sha256 = excluded.source_sha256,
            enriched_sha256 = excluded.enriched_sha256,
            bytes = excluded.bytes,
            status = 'complete',
            error = NULL,
            updated_at = excluded.updated_at
    """, (
        object_path,
        post.get("postId"),
        media_kind(attachment, raw),
        attachment.get("fileName"),
        attachment.get("source"),
        attachment.get("width"),
        attachment.get("height"),
        width,
        height,
        dt.isoformat(),
        dt_source,
        str(raw),
        str(photo),
        source_sha,
        enriched_sha,
        raw.stat().st_size,
        ts,
        ts,
    ))
    db.commit()
    return True


def fetch_page(
    api: requests.Session,
    cursor: int | None,
    cursor_post_id: str | None,
) -> dict[str, Any]:
    params = {
        "limit": str(PAGE_SIZE),
        "studentId": STUDENT_ID,
        "mediaOnly": "true",
        "origin": "web",
    }
    if cursor is None:
        params["reset"] = "true"
    else:
        params["startAfter"] = str(cursor)
        if cursor_post_id:
            params["cursorPostId"] = cursor_post_id

    r = api.get(f"{API_ROOT}/{SCHOOL_ID}/posts", params=params, timeout=(15, 60))
    if r.status_code == 401:
        raise RuntimeError(
            "Playground token expired (HTTP 401). Put a fresh PLAYGROUND_TOKEN "
            "into the container and rerun; completed assets will be skipped."
        )
    r.raise_for_status()
    return r.json()


def prerequisites() -> None:
    if not shutil.which("exiftool"):
        raise RuntimeError(
            "exiftool not found. Install libimage-exiftool-perl in the container."
        )
    if not 1 <= PAGE_SIZE <= 50:
        raise RuntimeError("PAGE_SIZE must be 1..50.")


def main() -> int:
    prerequisites()
    db = db_connect()
    api = api_session()

    run_id = db.execute(
        "INSERT INTO runs(started_at, status) VALUES (?, 'running')",
        (now_iso(),),
    ).lastrowid
    db.commit()

    cursor = None
    cursor_post_id = None
    seen_cursor_pairs: set[tuple[Any, Any]] = set()

    pages = posts_seen = assets_seen = assets_downloaded = 0

    try:
        while True:
            payload = fetch_page(api, cursor, cursor_post_id)
            posts = payload.get("data") or []
            if not posts:
                break

            pages += 1
            print(f"\nPage {pages}: {len(posts)} posts")

            for post in posts:
                if upsert_post(db, post):
                    append_post_jsonl(post)
                posts_seen += 1

                for attachment in post.get("attachments") or []:
                    if not attachment.get("path"):
                        continue
                    assets_seen += 1
                    try:
                        if process_asset(db, post, attachment):
                            assets_downloaded += 1
                    except Exception as exc:
                        print(f"  ! {attachment.get('path')}: {exc}", file=sys.stderr)
                        record_error(db, post, attachment, exc)

            db.commit()

            next_cursor = payload.get("cursor")
            next_post_id = payload.get("cursorPostId")
            if next_cursor is None:
                break

            pair = (next_cursor, next_post_id)
            if pair in seen_cursor_pairs:
                raise RuntimeError(f"Pagination cursor repeated: {pair}")
            seen_cursor_pairs.add(pair)

            cursor, cursor_post_id = next_cursor, next_post_id

            db.execute("""
                UPDATE runs
                SET pages=?, posts_seen=?, assets_seen=?, assets_downloaded=?
                WHERE id=?
            """, (pages, posts_seen, assets_seen, assets_downloaded, run_id))
            db.commit()

        db.execute("""
            UPDATE runs
            SET completed_at=?, pages=?, posts_seen=?, assets_seen=?,
                assets_downloaded=?, status='complete'
            WHERE id=?
        """, (
            now_iso(), pages, posts_seen, assets_seen, assets_downloaded, run_id
        ))
        db.commit()

        print("\nComplete")
        print(f"  pages:            {pages}")
        print(f"  posts seen:       {posts_seen}")
        print(f"  attachments seen: {assets_seen}")
        print(f"  newly completed:  {assets_downloaded}")
        print(f"  database:          {DB_PATH}")
        print(f"  raw archive:       {RAW_ROOT}")
        print(f"  Synology Photos:   {PHOTOS_ROOT}")
        return 0

    except KeyboardInterrupt:
        db.execute(
            "UPDATE runs SET completed_at=?, status='interrupted' WHERE id=?",
            (now_iso(), run_id),
        )
        db.commit()
        return 130
    except Exception as exc:
        db.execute(
            "UPDATE runs SET completed_at=?, status='failed' WHERE id=?",
            (now_iso(), run_id),
        )
        db.commit()
        print(f"\nFatal: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
