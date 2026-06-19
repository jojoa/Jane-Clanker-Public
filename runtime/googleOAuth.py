from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import config


DRIVE_AND_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def repoRoot() -> Path:
    return Path(__file__).resolve().parents[1]


def resolveLocalPath(rawPath: str) -> Path:
    rawText = str(rawPath or "").strip()
    if not rawText:
        return Path()
    raw = Path(rawText).expanduser()
    if raw.is_absolute():
        return raw
    return (repoRoot() / raw).resolve()


def defaultClientSecretsPath() -> Path:
    configured = (
        os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS_PATH")
        or str(getattr(config, "googleOauthClientSecretsPath", "") or "").strip()
    )
    if configured:
        return resolveLocalPath(configured)

    return repoRoot() / "localOnly" / "credentials" / "google-oauth-client-secret.json"


def defaultTokenPath() -> Path:
    configured = (
        os.getenv("GOOGLE_OAUTH_TOKEN_PATH")
        or str(getattr(config, "googleOauthTokenPath", "") or "").strip()
    )
    if configured:
        return resolveLocalPath(configured)
    return repoRoot() / "localOnly" / "credentials" / "google-oauth-token.json"


def loadClientSecrets(path: str | Path | None = None) -> dict:
    secretsPath = Path(path) if path else defaultClientSecretsPath()
    if not secretsPath.exists():
        raise FileNotFoundError(f"Google OAuth client secret JSON not found: {secretsPath}")

    data = json.loads(secretsPath.read_text(encoding="utf-8"))
    client = data.get("installed") or data.get("web")
    if not isinstance(client, dict):
        raise ValueError("Google OAuth client secret JSON must contain an 'installed' or 'web' client.")
    return client


def loadCredentials(
    *,
    tokenPath: str | Path | None = None,
    scopes: Iterable[str] = DRIVE_AND_SHEETS_SCOPES,
):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    resolvedTokenPath = Path(tokenPath) if tokenPath else defaultTokenPath()
    if not resolvedTokenPath.exists():
        raise FileNotFoundError(
            "Google OAuth token file is missing. Set GOOGLE_OAUTH_TOKEN_PATH to an authorized "
            f"Google OAuth user token JSON, or deploy the token to {resolvedTokenPath}."
        )

    scopeList = list(scopes)
    credentials = Credentials.from_authorized_user_file(str(resolvedTokenPath), scopes=scopeList)
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            resolvedTokenPath.parent.mkdir(parents=True, exist_ok=True)
            resolvedTokenPath.write_text(credentials.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "Google OAuth token is invalid and cannot be refreshed. "
                "Replace the configured Google OAuth token JSON."
            )

    if scopeList and hasattr(credentials, "has_scopes") and not credentials.has_scopes(scopeList):
        raise RuntimeError(
            "Google OAuth token is missing required scopes. "
            "Replace it with a token authorized for Drive and Sheets."
        )
    return credentials


def buildService(
    apiName: str,
    apiVersion: str,
    *,
    tokenPath: str | Path | None = None,
    scopes: Iterable[str] = DRIVE_AND_SHEETS_SCOPES,
):
    from googleapiclient.discovery import build

    credentials = loadCredentials(tokenPath=tokenPath, scopes=scopes)
    return build(apiName, apiVersion, credentials=credentials, cache_discovery=False)
