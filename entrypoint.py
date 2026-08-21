#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

import requests

FIREBASE_API_KEY = os.environ.get(
    "PLAYGROUND_FIREBASE_API_KEY",
    "AIzaSyBuxyDBBTk8f6REd_hPfpvPVFuYg3HcsXg",
)
LUVNOTES_ORIGIN = "https://luvnotes.littlesunshine.com"
PASSWORD_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
PLAYGROUND_EXCHANGE_URL = (
    "https://auth.tryplayground.com/api/auth/exchange/customToken"
)
CUSTOM_TOKEN_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
)


@dataclass(frozen=True)
class PlaygroundAuth:
    id_token: str
    refresh_token: str | None
    expires_in: int | None


def _json_error(prefix: str, response: requests.Response) -> RuntimeError:
    return RuntimeError(
        f"{prefix} (HTTP {response.status_code}): {response.text[:1000]}"
    )


def luvnotes_id_token() -> str:
    """Authenticate the LuvNotes account with Firebase email/password auth."""
    email = os.environ.get("PLAYGROUND_EMAIL")
    password = os.environ.get("PLAYGROUND_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "Set either PLAYGROUND_TOKEN, or both PLAYGROUND_EMAIL and "
            "PLAYGROUND_PASSWORD."
        )

    response = requests.post(
        PASSWORD_SIGN_IN_URL,
        params={"key": FIREBASE_API_KEY},
        headers={"Content-Type": "application/json"},
        json={
            "returnSecureToken": True,
            "email": email,
            "password": password,
            "clientType": "CLIENT_TYPE_WEB",
        },
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("LuvNotes Firebase password sign-in failed", response)

    token = response.json().get("idToken")
    if not token:
        raise RuntimeError(
            "LuvNotes Firebase password sign-in succeeded but returned no idToken"
        )
    return str(token)


def playground_custom_token(luvnotes_token: str) -> str:
    """Exchange the LuvNotes Firebase ID token for a Playground custom token."""
    response = requests.post(
        PLAYGROUND_EXCHANGE_URL,
        params={
            "origin": "web",
            "bundleLoadTime": str(int(time.time() * 1000)),
        },
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {luvnotes_token}",
            "Content-Type": "application/json",
            "Origin": LUVNOTES_ORIGIN,
            "Referer": f"{LUVNOTES_ORIGIN}/",
        },
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("Playground custom-token exchange failed", response)

    token = response.json().get("token")
    if not token:
        raise RuntimeError(
            "Playground custom-token exchange succeeded but returned no token"
        )
    return str(token)


def playground_id_token(custom_token: str) -> PlaygroundAuth:
    """Turn Playground's custom Auth token into the bearer token used by its API."""
    response = requests.post(
        CUSTOM_TOKEN_SIGN_IN_URL,
        params={"key": FIREBASE_API_KEY},
        headers={"Content-Type": "application/json"},
        json={
            "token": custom_token,
            "returnSecureToken": True,
        },
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("Playground Firebase custom-token sign-in failed", response)

    payload = response.json()
    token = payload.get("idToken")
    if not token:
        raise RuntimeError(
            "Playground Firebase custom-token sign-in succeeded but returned no idToken"
        )

    expires_raw = payload.get("expiresIn")
    try:
        expires_in = int(expires_raw) if expires_raw is not None else None
    except (TypeError, ValueError):
        expires_in = None

    return PlaygroundAuth(
        id_token=str(token),
        refresh_token=(
            str(payload["refreshToken"]) if payload.get("refreshToken") else None
        ),
        expires_in=expires_in,
    )


def authenticate() -> PlaygroundAuth:
    first_stage_token = luvnotes_id_token()
    custom_token = playground_custom_token(first_stage_token)
    return playground_id_token(custom_token)


def main() -> int:
    # A manually supplied final Playground ID token remains supported for
    # debugging. Normally Portainer should provide email/password instead.
    if not os.environ.get("PLAYGROUND_TOKEN"):
        auth = authenticate()
        os.environ["PLAYGROUND_TOKEN"] = auth.id_token
        if auth.refresh_token:
            # Expose to the child process for future transparent refresh logic.
            os.environ["PLAYGROUND_REFRESH_TOKEN"] = auth.refresh_token

        lifetime = (
            f"; token lifetime {auth.expires_in}s" if auth.expires_in else ""
        )
        print(
            "Authenticated LuvNotes -> Playground via Firebase custom token"
            f"{lifetime}; starting archive.",
            flush=True,
        )

    return subprocess.call([sys.executable, "/app/luvnotes_archive.py"])


if __name__ == "__main__":
    raise SystemExit(main())
