#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

FIREBASE_API_KEY = os.environ.get(
    "PLAYGROUND_FIREBASE_API_KEY",
    "AIzaSyBuxyDBBTk8f6REd_hPfpvPVFuYg3HcsXg",
)
FIREBASE_GMPID = os.environ.get(
    "PLAYGROUND_FIREBASE_GMPID",
    "1:1048128432512:web:fc9ca2c25fa4ecf4edb965",
)
LUVNOTES_ORIGIN = "https://luvnotes.littlesunshine.com"
BUNDLE_LOAD_TIME = os.environ.get(
    "PLAYGROUND_BUNDLE_LOAD_TIME", str(int(time.time() * 1000))
)
USER_AGENT = os.environ.get(
    "PLAYGROUND_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36",
)

IDENTITY_ROOT = "https://identitytoolkit.googleapis.com/v1"
PASSWORD_SIGN_IN_URL = f"{IDENTITY_ROOT}/accounts:signInWithPassword"
CUSTOM_TOKEN_SIGN_IN_URL = f"{IDENTITY_ROOT}/accounts:signInWithCustomToken"
LOOKUP_URL = f"{IDENTITY_ROOT}/accounts:lookup"
PLAYGROUND_EXCHANGE_URL = "https://auth.tryplayground.com/api/auth/exchange/customToken"
PLAYGROUND_PUBLIC_ACCOUNT_URL = "https://api.tryplayground.com/api/public/account"


@dataclass(frozen=True)
class FirebaseAuth:
    id_token: str
    refresh_token: str | None
    expires_in: int | None
    local_id: str | None = None


def _json_error(prefix: str, response: requests.Response) -> RuntimeError:
    return RuntimeError(
        f"{prefix} (HTTP {response.status_code}): {response.text[:1000]}"
    )


def firebase_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": LUVNOTES_ORIGIN,
        "Referer": f"{LUVNOTES_ORIGIN}/",
        "User-Agent": USER_AGENT,
        "X-Client-Version": "Chrome/JsCore/12.16.0/FirebaseCore-web",
        "X-Firebase-GMPID": FIREBASE_GMPID,
    }


def playground_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": LUVNOTES_ORIGIN,
        "Pragma": "no-cache",
        "Referer": f"{LUVNOTES_ORIGIN}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def web_params() -> dict[str, str]:
    return {"origin": "web", "bundleLoadTime": BUNDLE_LOAD_TIME}


def _expires(payload: dict[str, Any]) -> int | None:
    value = payload.get("expiresIn")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def luvnotes_sign_in() -> FirebaseAuth:
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
        headers=firebase_headers(),
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

    payload = response.json()
    token = payload.get("idToken")
    if not token:
        raise RuntimeError("Firebase password sign-in returned no idToken")

    return FirebaseAuth(
        id_token=str(token),
        refresh_token=(
            str(payload["refreshToken"]) if payload.get("refreshToken") else None
        ),
        expires_in=_expires(payload),
        local_id=str(payload["localId"]) if payload.get("localId") else None,
    )


def firebase_lookup(auth: FirebaseAuth) -> FirebaseAuth:
    response = requests.post(
        LOOKUP_URL,
        params={"key": FIREBASE_API_KEY},
        headers=firebase_headers(),
        json={"idToken": auth.id_token},
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("Firebase account lookup failed", response)

    users = response.json().get("users") or []
    local_id = (
        users[0].get("localId")
        if users and isinstance(users[0], dict)
        else None
    )
    return FirebaseAuth(
        id_token=auth.id_token,
        refresh_token=auth.refresh_token,
        expires_in=auth.expires_in,
        local_id=str(local_id) if local_id else auth.local_id,
    )


def playground_custom_token(
    bearer_token: str,
    *,
    actor: dict[str, str] | None = None,
) -> str:
    response = requests.post(
        PLAYGROUND_EXCHANGE_URL,
        params=web_params(),
        headers=playground_headers(bearer_token),
        json={"actor": actor} if actor else None,
        timeout=(15, 30),
    )
    if not response.ok:
        stage = "actor-scoped " if actor else ""
        raise _json_error(
            f"Playground {stage}custom-token exchange failed",
            response,
        )

    token = response.json().get("token")
    if not token:
        raise RuntimeError("Playground custom-token exchange returned no token")
    return str(token)


def sign_in_custom(custom_token: str) -> FirebaseAuth:
    response = requests.post(
        CUSTOM_TOKEN_SIGN_IN_URL,
        params={"key": FIREBASE_API_KEY},
        headers=firebase_headers(),
        json={"token": custom_token, "returnSecureToken": True},
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("Firebase custom-token sign-in failed", response)

    payload = response.json()
    token = payload.get("idToken")
    if not token:
        raise RuntimeError("Firebase custom-token sign-in returned no idToken")
    return FirebaseAuth(
        id_token=str(token),
        refresh_token=(
            str(payload["refreshToken"]) if payload.get("refreshToken") else None
        ),
        expires_in=_expires(payload),
        local_id=str(payload["localId"]) if payload.get("localId") else None,
    )


def _account_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "accountId" and isinstance(child, str) and child:
                found.append(child)
            found.extend(_account_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_account_ids(child))
    return found


def resolve_account_id(auth: FirebaseAuth) -> str:
    override = os.environ.get("PLAYGROUND_ACCOUNT_ID")
    if override:
        return override

    if not auth.local_id:
        raise RuntimeError(
            "First Playground Firebase identity has no localId; "
            "set PLAYGROUND_ACCOUNT_ID explicitly."
        )

    response = requests.post(
        PLAYGROUND_PUBLIC_ACCOUNT_URL,
        params=web_params(),
        headers=playground_headers(auth.id_token),
        json={"authId": auth.local_id},
        timeout=(15, 30),
    )
    if not response.ok:
        raise _json_error("Playground public-account lookup failed", response)

    payload = response.json()
    ids = list(dict.fromkeys(_account_ids(payload)))
    if len(ids) == 1:
        return ids[0]
    if not ids:
        keys = (
            sorted(payload)
            if isinstance(payload, dict)
            else type(payload).__name__
        )
        raise RuntimeError(
            "Playground public-account lookup returned no accountId; "
            "set PLAYGROUND_ACCOUNT_ID explicitly. "
            f"Top-level response keys: {keys}"
        )
    raise RuntimeError(
        "Playground public-account lookup returned multiple accountIds; "
        "set PLAYGROUND_ACCOUNT_ID explicitly."
    )


def authenticate() -> FirebaseAuth:
    # Browser HAR:
    # password sign-in -> lookup -> exchange -> custom sign-in -> lookup
    # -> /public/account -> actor-scoped exchange -> custom sign-in -> lookup
    initial = firebase_lookup(luvnotes_sign_in())

    first_custom = playground_custom_token(initial.id_token)
    bridge = firebase_lookup(sign_in_custom(first_custom))

    school_id = os.environ["PLAYGROUND_SCHOOL_ID"]
    account_id = resolve_account_id(bridge)

    second_custom = playground_custom_token(
        bridge.id_token,
        actor={"schoolId": school_id, "accountId": account_id},
    )
    return firebase_lookup(sign_in_custom(second_custom))


def main() -> int:
    if not os.environ.get("PLAYGROUND_TOKEN"):
        auth = authenticate()
        os.environ["PLAYGROUND_TOKEN"] = auth.id_token
        if auth.refresh_token:
            os.environ["PLAYGROUND_REFRESH_TOKEN"] = auth.refresh_token

        lifetime = (
            f"; token lifetime {auth.expires_in}s" if auth.expires_in else ""
        )
        print(
            "Authenticated LuvNotes -> Playground actor-scoped Firebase identity"
            f"{lifetime}; starting archive.",
            flush=True,
        )

    return subprocess.call([sys.executable, "/app/luvnotes_archive.py"])


if __name__ == "__main__":
    raise SystemExit(main())
