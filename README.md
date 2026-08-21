# LuvNotes Archive

Archive media visible to an authenticated LuvNotes / Playground guardian account, preserve the exact downloaded bytes, reconstruct useful media metadata where the delivered rendition has lost it, and publish enriched copies into a Synology Photos filesystem.

## Data flow

```text
Playground /posts?mediaOnly=true
        |
        | cursor + cursorPostId pagination
        v
Cloudflare R2 signed attachment URLs
        |
        +--> /archive/raw/YYYY/MM/   exact downloaded bytes
        |         |
        |         +--> SHA-256 + SQLite manifest
        |
        +--> metadata-enriched copy
                  |
                  v
            /photos/YYYY/MM/
                  |
                  v
            Synology Photos
```

The archive never persists expiring R2 signed URLs. The Playground bearer token is used only for the Playground API and is never forwarded to R2.

## What is preserved

For every attachment, SQLite records the Playground object path, post ID, declared dimensions, actual downloaded dimensions, source and enriched hashes, local paths, and the provenance of the selected timestamp.

The raw asset is never modified. A separate Synology Photos copy receives EXIF/XMP/IPTC or QuickTime metadata. Existing capture metadata is retained when present; otherwise the importer uses the Playground post timestamp, then attachment-created time as a fallback.

## Requirements

The Docker image contains Python 3.12, `requests`, and ExifTool. No inbound service or port is exposed.

## Configure

Copy `.env.example` to `.env` for local Compose use, or enter the same variables in Portainer.

```dotenv
PLAYGROUND_TOKEN=<fresh Firebase ID token>
PLAYGROUND_SCHOOL_ID=<school id>
PLAYGROUND_STUDENT_ID=<student id>
PLAYGROUND_GUARDIAN_ID=<guardian id>

PUID=<Synology user uid>
PGID=<Synology user gid>

LUVNOTES_ARCHIVE_PATH=/volume1/docker/luvnotes/archive
SYNOLOGY_PHOTOS_PATH=/volume1/homes/<username>/Photos/LuvNotes

TZ=America/Chicago
PAGE_SIZE=50
```

Do **not** commit `.env` or bearer tokens.

Find the UID/GID of the Synology account that owns the Photos library:

```bash
id <synology-username>
```

## Build and run on the NAS

If the repository is checked out on the NAS:

```bash
docker compose build
docker compose run --rm luvnotes
```

The container exits after synchronizing the currently visible gallery. Re-running is safe: completed attachment object paths are skipped using the SQLite manifest.

### Portainer

The easiest setup is **Stacks -> Add stack -> Repository** and point Portainer at this Git repository. Configure the environment variables in Portainer rather than committing secrets.

The stack intentionally has:

- no published ports;
- a read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- writable access only to the archive and Synology Photos bind mounts plus `/tmp`.

## Authentication lifecycle

The LuvNotes browser currently supplies a short-lived Firebase bearer token. When the importer receives HTTP 401 it exits without losing completed work. Replace `PLAYGROUND_TOKEN` with a fresh token and run it again.

A future improvement can reproduce the legitimate token-refresh flow so recurring runs do not require manual token replacement.

## Archive layout

```text
/volume1/docker/luvnotes/archive/
├── luvnotes.sqlite3
├── posts.jsonl
└── raw/
    └── YYYY/
        └── MM/

/volume1/homes/<user>/Photos/LuvNotes/
└── YYYY/
    └── MM/
```

`luvnotes.sqlite3` is the authoritative synchronization state. `posts.jsonl` is a human-readable secondary record.

## Security

This project processes private family media. Keep the repository free of exported API responses, signed R2 URLs, bearer tokens, cookies, IDs you do not need to publish, and downloaded media. The default ignore rules exclude local state and archive data.
