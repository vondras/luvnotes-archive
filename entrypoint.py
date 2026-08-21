#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys

import requests

FIREBASE_API_KEY = os.environ.get(
    "PLAYGROUND_FIREBASE_API_KEY",
    "AIzaSyBuxyDBBTk8f6REd_hPfpvPVFuYg3HcsXg",
)
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"


def firebase_id_token() -> str:
    email = os.environ.get("PLAYGROUND_EMAIL")
    password = os.environ.get("PLAYGROUND_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "Set either PLAYGROUND_TOKEN, or both PLAYGROUND_EMAIL and "
            "PLAYGROUND_PASSWORD."
        )

    response = requests.post(
        SIGN_IN_URL,
        params={"key": FIREBASE_API_KEY},
        json={
            "returnSecureToken": True,
            "email": email,
            "password": password,
            "clientType": "CLIENT_TYPE_WEB",
        },
        timeout=(15, 30),
    )

    if not response.ok:
        raise RuntimeError(
            f"Firebase sign-in failed (HTTP {response.status_code}): "
            f"{response.text[:1000]}"
        )

    token = response.json().get("idToken")
    if not token:
        raise RuntimeError("Firebase sign-in succeeded but returned no idToken")

    return str(token)


def main() -> int:
    # A manually supplied token remains supported for debugging. Otherwise,
    # obtain a fresh one at container start so Portainer never needs a copied
    # one-hour Firebase ID token.
    if not os.environ.get("PLAYGROUND_TOKEN"):
        os.environ["PLAYGROUND_TOKEN"] = firebase_id_token()
        print("Authenticated to Firebase; starting LuvNotes archive.", flush=True)

    return subprocess.call([sys.executable, "/app/luvnotes_archive.py"])


if __name__ == "__main__":
    raise SystemExit(main())
