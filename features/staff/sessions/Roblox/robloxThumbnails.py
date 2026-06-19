from __future__ import annotations

from typing import Optional

import config
from features.staff.sessions.Roblox import robloxPayloads, robloxTransport

_cacheGet = robloxTransport.cacheGet
_cacheSet = robloxTransport.cacheSet
_requestJson = robloxTransport.requestJson
_optionalInt = robloxPayloads.optionalInt

_THUMBNAIL_ENDPOINTS = {
    "group": ("https://thumbnails.roblox.com/v1/groups/icons", "groupIds", "robloxGroupThumbnailCacheTtlSec"),
    "badge": ("https://thumbnails.roblox.com/v1/badges/icons", "badgeIds", "robloxBadgeThumbnailCacheTtlSec"),
    "game": ("https://thumbnails.roblox.com/v1/games/icons", "universeIds", "robloxGameThumbnailCacheTtlSec"),
}


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": str(
            getattr(
                config,
                "robloxPublicApiUserAgent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jane-Clanker/1.0",
            )
        ),
    }


def _thumbnailCacheKey(kind: str, targetId: int, size: str, imageFormat: str) -> tuple[str, int, str, str]:
    return str(kind), int(targetId), str(size), str(imageFormat)


def _defaultThumbnailSize(kind: str) -> str:
    if str(kind or "").strip().lower() == "badge":
        return "150x150"
    return "420x420"


async def fetchRobloxThumbnails(
    kind: str,
    targetIds: list[int],
    *,
    size: str | None = None,
    imageFormat: str = "Png",
    isCircular: bool = False,
    returnPolicy: str = "PlaceHolder",
) -> list[dict]:
    normalizedKind = str(kind or "").strip().lower()
    endpointConfig = _THUMBNAIL_ENDPOINTS.get(normalizedKind)
    if endpointConfig is None:
        return []
    url, idParam, ttlName = endpointConfig
    uniqueIds = sorted({int(value) for value in list(targetIds or []) if int(value or 0) > 0})
    if not uniqueIds:
        return []
    normalizedSize = str(size or _defaultThumbnailSize(normalizedKind))

    rows: list[dict] = []
    missingIds: list[int] = []
    for targetId in uniqueIds:
        cached = _cacheGet(
            "thumbnail_metadata",
            _thumbnailCacheKey(normalizedKind, int(targetId), normalizedSize, imageFormat),
            ttlName=ttlName,
            defaultTtlSec=86400,
        )
        if isinstance(cached, dict):
            rows.append(dict(cached))
        else:
            missingIds.append(int(targetId))

    for start in range(0, len(missingIds), 100):
        batch = missingIds[start : start + 100]
        params = {
            idParam: ",".join(str(targetId) for targetId in batch),
            "size": normalizedSize,
            "format": str(imageFormat),
            "isCircular": "true" if isCircular else "false",
        }
        if returnPolicy:
            params["returnPolicy"] = str(returnPolicy)
        try:
            status, payload = await _requestJson(
                "GET",
                url,
                headers=_headers(),
                params=params,
                timeoutSec=10,
            )
            if status == 400 and "returnPolicy" in params:
                retryParams = dict(params)
                retryParams.pop("returnPolicy", None)
                status, payload = await _requestJson(
                    "GET",
                    url,
                    headers=_headers(),
                    params=retryParams,
                    timeoutSec=10,
                )
        except Exception:
            continue
        if status != 200 or not isinstance(payload, dict):
            continue
        rawRows = payload.get("data")
        if not isinstance(rawRows, list):
            continue
        for entry in rawRows:
            if not isinstance(entry, dict):
                continue
            targetId = _optionalInt(entry.get("targetId"))
            if targetId is None:
                continue
            row = {
                "id": int(targetId),
                "imageUrl": entry.get("imageUrl"),
                "state": entry.get("state"),
            }
            rows.append(row)
            _cacheSet(
                "thumbnail_metadata",
                _thumbnailCacheKey(normalizedKind, int(targetId), normalizedSize, imageFormat),
                dict(row),
                ttlName=ttlName,
                defaultTtlSec=86400,
            )
    return rows


async def fetchRobloxThumbnailUrl(
    kind: str,
    targetId: int,
    *,
    size: str | None = None,
    imageFormat: str = "Png",
    isCircular: bool = False,
) -> Optional[str]:
    rows = await fetchRobloxThumbnails(
        kind,
        [int(targetId)],
        size=size,
        imageFormat=imageFormat,
        isCircular=isCircular,
    )
    for row in rows:
        if int(row.get("id") or 0) != int(targetId):
            continue
        imageUrl = str(row.get("imageUrl") or "").strip()
        state = str(row.get("state") or "").strip().lower()
        if imageUrl and state in {"", "completed"}:
            return imageUrl
    return None
