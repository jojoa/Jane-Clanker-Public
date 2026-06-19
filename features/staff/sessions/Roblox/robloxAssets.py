from __future__ import annotations

import asyncio
import colorsys
import json
from collections import deque
from io import BytesIO
from typing import Optional

from PIL import Image

import config
from features.staff.sessions.Roblox import robloxPayloads, robloxTransport
from features.staff.sessions.Roblox.robloxModels import RobloxAssetThumbnailsResult
from runtime import taskBudgeter

_utcNow = robloxTransport.utcNow
_cacheGet = robloxTransport.cacheGet
_cacheSet = robloxTransport.cacheSet
_requestJson = robloxTransport.requestJson
_optionalBool = robloxPayloads.optionalBool
_optionalInt = robloxPayloads.optionalInt
_extractRobuxPrice = robloxPayloads.extractRobuxPrice
_extractCreatorId = robloxPayloads.extractCreatorId
_extractCreatorName = robloxPayloads.extractCreatorName
_extractCreatorType = robloxPayloads.extractCreatorType

_catalogXsrfToken: str = ""


def _assetPriceLookupConcurrency() -> int:
    try:
        configured = int(getattr(config, "robloxAssetPriceLookupConcurrency", 16) or 16)
    except (TypeError, ValueError):
        configured = 16
    return max(1, min(configured, 32))


def _assetPriceFallbackMaxAssets() -> int:
    try:
        configured = int(getattr(config, "robloxAssetPriceFallbackMaxAssets", 10) or 10)
    except (TypeError, ValueError):
        configured = 10
    return max(0, min(configured, 500))


def _catalogAssetBatchSize() -> int:
    try:
        configured = int(getattr(config, "robloxAssetPriceBatchSize", 100) or 100)
    except (TypeError, ValueError):
        configured = 100
    return max(1, min(configured, 100))


def _catalogAssetBatchConcurrency() -> int:
    try:
        configured = int(getattr(config, "robloxAssetPriceBatchConcurrency", 3) or 3)
    except (TypeError, ValueError):
        configured = 3
    return max(1, min(configured, 6))


def _catalogItemEntryFromPayload(assetId: int, payload: dict) -> dict:
    restrictions = {
        str(value).replace(" ", "").strip().lower()
        for value in list(payload.get("itemRestrictions") or [])
        if str(value).strip()
    }
    offSale = payload.get("isOffSale")
    if offSale is None:
        priceStatus = str(payload.get("priceStatus") or "").replace(" ", "").strip().lower()
        offSale = priceStatus in {"offsale", "noresellers"}
    entry = {
        "id": int(assetId),
        "name": payload.get("name"),
        "price": _extractRobuxPrice(payload),
        "isForSale": _optionalBool(False if offSale is None else not bool(offSale)),
        "isLimited": bool({"limited", "limitedunique"} & restrictions),
        "isLimitedUnique": "limitedunique" in restrictions,
        "creatorId": _optionalInt(payload.get("creatorTargetId")) or _extractCreatorId(payload),
        "creatorName": payload.get("creatorName") or _extractCreatorName(payload),
        "creatorType": payload.get("creatorType") or _extractCreatorType(payload),
        "assetTypeId": _optionalInt(payload.get("assetType")),
        "assetTypeName": payload.get("assetTypeName"),
    }
    return entry


async def _fetchCatalogAssetPricesBatch(assetIds: list[int], headers: dict[str, str]) -> tuple[dict[int, dict], list[int], Optional[str]]:
    uniqueIds = sorted({int(assetId) for assetId in assetIds if int(assetId or 0) > 0})
    if not uniqueIds:
        return {}, [], None

    session = await robloxTransport.getHttpSession()
    url = "https://catalog.roblox.com/v1/catalog/items/details"
    maxRetryCount = max(0, int(getattr(config, "robloxApi429MaxRetries", 2) or 2))
    baseDelaySec = max(0.1, float(getattr(config, "robloxApi429RetryDelaySec", 1.0) or 1.0))
    batchSize = _catalogAssetBatchSize()
    semaphore = asyncio.Semaphore(_catalogAssetBatchConcurrency())
    errors: list[str] = []
    rowsById: dict[int, dict] = {}
    tokenLock = asyncio.Lock()

    async def _sendBatch(batchIds: list[int]) -> tuple[list[dict], Optional[str]]:
        global _catalogXsrfToken
        body = {
            "items": [
                {"itemType": "Asset", "id": int(assetId)}
                for assetId in list(batchIds or [])
                if int(assetId or 0) > 0
            ]
        }
        if not body["items"]:
            return [], None
        token = str(_catalogXsrfToken or "").strip()
        xsrfRetried = False
        async with semaphore:
            for attempt in range(maxRetryCount + 1):
                requestHeaders = dict(headers)
                if token:
                    requestHeaders["x-csrf-token"] = token

                async def _runRequest() -> tuple[int, object, dict[str, str]]:
                    async with session.post(url, headers=requestHeaders, json=body) as response:
                        try:
                            payload = await response.json(content_type=None)
                        except Exception:
                            payload = None
                        return int(response.status or 0), payload, dict(response.headers or {})

                status, payload, responseHeaders = await taskBudgeter.runRoblox(_runRequest)
                refreshedToken = str(responseHeaders.get("x-csrf-token") or "").strip()
                if refreshedToken:
                    async with tokenLock:
                        _catalogXsrfToken = refreshedToken
                    token = refreshedToken
                if status == 403 and refreshedToken and not xsrfRetried:
                    xsrfRetried = True
                    continue
                if status == 429 and attempt < maxRetryCount:
                    retryAfterSec = 0.0
                    retryAfterHeader = responseHeaders.get("Retry-After")
                    if retryAfterHeader:
                        try:
                            retryAfterSec = float(retryAfterHeader)
                        except (TypeError, ValueError):
                            retryAfterSec = 0.0
                    if isinstance(payload, dict):
                        payloadRetry = payload.get("retry_after") or payload.get("retryAfter")
                        if payloadRetry is not None:
                            try:
                                retryAfterSec = max(retryAfterSec, float(payloadRetry))
                            except (TypeError, ValueError):
                                pass
                    await asyncio.sleep(max(baseDelaySec * (attempt + 1), retryAfterSec))
                    continue
                if status != 200 or not isinstance(payload, dict):
                    return [], f"Catalog asset batch lookup failed ({status})."
                data = payload.get("data")
                if not isinstance(data, list):
                    return [], "Catalog asset batch lookup returned invalid data."
                return data, None
        return [], "Catalog asset batch lookup failed (429)."

    batchResults = await asyncio.gather(
        *[
            _sendBatch(uniqueIds[start : start + batchSize])
            for start in range(0, len(uniqueIds), batchSize)
        ],
        return_exceptions=True,
    )
    for result in batchResults:
        if isinstance(result, Exception):
            errors.append(str(result))
            continue
        rows, error = result
        if error:
            errors.append(error)
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            assetId = _optionalInt(row.get("id"))
            if assetId is None or int(assetId) <= 0:
                continue
            rowsById[int(assetId)] = row

    prices: dict[int, dict] = {}
    resolvedIds: set[int] = set()
    for assetId, row in rowsById.items():
        entry = _catalogItemEntryFromPayload(int(assetId), row)
        prices[int(assetId)] = entry
        resolvedIds.add(int(assetId))
        _cacheSet(
            "asset_prices",
            int(assetId),
            dict(entry),
            ttlName="robloxAssetPriceCacheTtlSec",
            defaultTtlSec=86400,
        )

    missingIds = [int(assetId) for assetId in uniqueIds if int(assetId) not in resolvedIds]
    return prices, missingIds, "; ".join(errors[:3]) or None


async def _fetchCatalogAssetPrices(assetIds: list[int]) -> tuple[dict[int, dict], Optional[str]]:
    uniqueIds = sorted({int(assetId) for assetId in assetIds if int(assetId or 0) > 0})
    if not uniqueIds:
        return {}, None

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": str(
            getattr(
                config,
                "robloxPublicApiUserAgent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jane-Clanker/1.0",
            )
        ),
    }
    prices: dict[int, dict] = {}
    missingIds: list[int] = []
    for assetId in uniqueIds:
        cached = _cacheGet(
            "asset_prices",
            int(assetId),
            ttlName="robloxAssetPriceCacheTtlSec",
            defaultTtlSec=86400,
        )
        if isinstance(cached, dict):
            if (
                cached.get("creatorId") is not None
                and (cached.get("assetTypeId") is not None or cached.get("assetTypeName") is not None)
                and "creatorType" in cached
            ):
                prices[int(assetId)] = dict(cached)
            else:
                missingIds.append(int(assetId))
        else:
            missingIds.append(int(assetId))
    if not missingIds:
        return prices, None

    batchPrices, missingIds, batchError = await _fetchCatalogAssetPricesBatch(missingIds, headers)
    prices.update(batchPrices)
    if not missingIds:
        return prices, batchError

    errors: list[str] = []
    if batchError:
        errors.append(batchError)
    fallbackMaxAssets = _assetPriceFallbackMaxAssets()
    skippedFallbackCount = max(0, len(missingIds) - fallbackMaxAssets)
    if fallbackMaxAssets <= 0:
        if missingIds:
            errors.append(f"Skipped {len(missingIds)} individual asset price lookup(s) after catalog batch lookup.")
        return prices, "; ".join(errors[:3]) or None
    if skippedFallbackCount > 0:
        missingIds = missingIds[:fallbackMaxAssets]
        errors.append(
            f"Skipped {skippedFallbackCount} individual asset price lookup(s) after catalog batch lookup."
        )
    semaphore = asyncio.Semaphore(_assetPriceLookupConcurrency())

    async def _fetchAsset(assetId: int) -> tuple[int, dict | None, Optional[str]]:
        url = f"https://economy.roblox.com/v2/assets/{int(assetId)}/details"
        async with semaphore:
            try:
                status, data = await _requestJson("GET", url, headers=headers, timeoutSec=10)
            except Exception as exc:
                return int(assetId), None, str(exc)
        if status != 200 or not isinstance(data, dict):
            return int(assetId), None, f"Asset price lookup failed ({status})."
        entry = {
            "id": int(assetId),
            "name": data.get("Name") or data.get("name"),
            "price": _extractRobuxPrice(data),
            "isForSale": _optionalBool(
                data.get("IsForSale") if data.get("IsForSale") is not None else data.get("isForSale")
            ),
            "isLimited": _optionalBool(
                data.get("IsLimited") if data.get("IsLimited") is not None else data.get("isLimited")
            ),
            "isLimitedUnique": _optionalBool(
                data.get("IsLimitedUnique") if data.get("IsLimitedUnique") is not None else data.get("isLimitedUnique")
            ),
            "creatorId": _extractCreatorId(data),
            "creatorName": _extractCreatorName(data),
            "creatorType": _extractCreatorType(data),
            "assetTypeId": _optionalInt(data.get("AssetTypeId") or data.get("assetTypeId")),
            "assetTypeName": data.get("AssetType") or data.get("assetType") or data.get("assetTypeName"),
        }
        return int(assetId), entry, None

    results = await asyncio.gather(
        *[_fetchAsset(assetId) for assetId in missingIds],
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result))
            continue
        assetId, entry, error = result
        if error:
            errors.append(error)
            continue
        if not isinstance(entry, dict):
            continue
        prices[int(assetId)] = entry
        _cacheSet(
            "asset_prices",
            int(assetId),
            dict(entry),
            ttlName="robloxAssetPriceCacheTtlSec",
            defaultTtlSec=86400,
        )

    return prices, "; ".join(errors[:3]) or None
def _inventoryVisualHashSize() -> int:
    try:
        configured = int(getattr(config, "bgIntelligenceInventoryVisualHashSize", 16) or 16)
    except (TypeError, ValueError):
        configured = 16
    return max(4, min(configured, 16))


def _assetThumbnailHashConcurrency() -> int:
    try:
        configured = int(getattr(config, "robloxAssetThumbnailHashConcurrency", 10) or 10)
    except (TypeError, ValueError):
        configured = 10
    return max(1, min(configured, 24))


def _visualSignatureVersion() -> int:
    return 3


def _thumbnailHashCacheKey(assetId: int, hashSize: int) -> tuple[int, int, int]:
    return int(assetId), int(hashSize), _visualSignatureVersion()


def _colorSignatureVersion() -> int:
    return _visualSignatureVersion()


def _thumbnailColorSignatureCacheKey(assetId: int, hashSize: int) -> tuple[int, int, int]:
    return int(assetId), int(hashSize), _colorSignatureVersion()


def _rgbDistance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return (
        (int(left[0]) - int(right[0])) ** 2
        + (int(left[1]) - int(right[1])) ** 2
        + (int(left[2]) - int(right[2])) ** 2
    ) ** 0.5


def _thumbnailBackgroundColor(pixels: list[tuple[int, int, int, int]], width: int, height: int) -> tuple[int, int, int]:
    samples: list[tuple[int, int, int]] = []
    for y in range(height):
        for x in range(width):
            if x not in {0, 1, width - 2, width - 1} and y not in {0, 1, height - 2, height - 1}:
                continue
            red, green, blue, alpha = pixels[y * width + x]
            if int(alpha) >= 32:
                samples.append((int(red), int(green), int(blue)))
    if not samples:
        return (0, 0, 0)
    return (
        int(round(sum(sample[0] for sample in samples) / len(samples))),
        int(round(sum(sample[1] for sample in samples) / len(samples))),
        int(round(sum(sample[2] for sample in samples) / len(samples))),
    )


def _backgroundMask(
    pixels: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    backgroundColor: tuple[int, int, int],
) -> bytearray:
    threshold = 42.0
    backgroundLike = bytearray(width * height)
    for index, (red, green, blue, alpha) in enumerate(pixels):
        if int(alpha) < 32 or _rgbDistance((int(red), int(green), int(blue)), backgroundColor) <= threshold:
            backgroundLike[index] = 1

    seen = bytearray(width * height)
    queue: deque[int] = deque()
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            if backgroundLike[index] and not seen[index]:
                seen[index] = 1
                queue.append(index)
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            if backgroundLike[index] and not seen[index]:
                seen[index] = 1
                queue.append(index)

    while queue:
        index = queue.popleft()
        x = index % width
        y = index // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            nextIndex = ny * width + nx
            if seen[nextIndex] or not backgroundLike[nextIndex]:
                continue
            seen[nextIndex] = 1
            queue.append(nextIndex)
    return seen


def _thumbnailContentMask(image: Image.Image, *, removeLargeNeutral: bool = True) -> tuple[Image.Image, list[bool]]:
    resized = image.convert("RGBA").resize((96, 96), Image.Resampling.LANCZOS)
    width, height = resized.size
    pixels = list(resized.getdata())
    backgroundColor = _thumbnailBackgroundColor(pixels, width, height)
    background = _backgroundMask(pixels, width, height, backgroundColor)
    foreground = bytearray(width * height)
    neutralBodyLike = bytearray(width * height)
    for index, (red, green, blue, alpha) in enumerate(pixels):
        if background[index] or int(alpha) < 32:
            continue
        x = index % width
        y = index // width
        redFloat = int(red) / 255.0
        greenFloat = int(green) / 255.0
        blueFloat = int(blue) / 255.0
        _, saturation, value = colorsys.rgb_to_hsv(redFloat, greenFloat, blueFloat)
        if saturation < 0.12 and value < 0.35 and (width * 0.30) <= x <= (width * 0.70) and y <= (height * 0.34):
            continue
        foreground[index] = 1
        if saturation < 0.12 and value > 0.22:
            neutralBodyLike[index] = 1

    removedNeutral = bytearray(width * height)
    if removeLargeNeutral:
        seen = bytearray(width * height)
        for startIndex in range(width * height):
            if seen[startIndex] or not neutralBodyLike[startIndex]:
                continue
            component: list[int] = []
            queue: deque[int] = deque([startIndex])
            seen[startIndex] = 1
            while queue:
                index = queue.popleft()
                component.append(index)
                x = index % width
                y = index // width
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    nextIndex = ny * width + nx
                    if seen[nextIndex] or not neutralBodyLike[nextIndex]:
                        continue
                    seen[nextIndex] = 1
                    queue.append(nextIndex)
            if len(component) >= 80:
                for index in component:
                    removedNeutral[index] = 1

    mask = [bool(foreground[index] and not removedNeutral[index]) for index in range(width * height)]
    return resized, mask


def _contentStats(pixels: list[tuple[int, int, int, int]], contentIndexes: list[int]) -> dict[str, object]:
    lumas: list[int] = []
    saturatedPixels = 0
    darkPixels = 0
    for index in contentIndexes:
        red, green, blue, _ = pixels[index]
        redInt = int(red)
        greenInt = int(green)
        blueInt = int(blue)
        luma = int(round((0.299 * redInt) + (0.587 * greenInt) + (0.114 * blueInt)))
        lumas.append(luma)
        _, saturation, value = colorsys.rgb_to_hsv(redInt / 255.0, greenInt / 255.0, blueInt / 255.0)
        if saturation >= 0.18:
            saturatedPixels += 1
        if value <= 0.45:
            darkPixels += 1

    sortedLumas = sorted(lumas)
    low = sortedLumas[max(0, int(len(sortedLumas) * 0.10) - 1)]
    high = sortedLumas[min(len(sortedLumas) - 1, int(len(sortedLumas) * 0.90))]
    contrast = high - low
    total = max(1, len(contentIndexes))
    return {
        "lumas": lumas,
        "contrast": int(contrast),
        "saturatedRatio": saturatedPixels / total,
        "darkRatio": darkPixels / total,
    }


def _hasDetailedContent(stats: dict[str, object]) -> bool:
    try:
        contrast = int(stats.get("contrast") or 0)
        saturatedRatio = float(stats.get("saturatedRatio") or 0.0)
        darkRatio = float(stats.get("darkRatio") or 0.0)
    except (TypeError, ValueError):
        return False
    return contrast >= 16 or saturatedRatio >= 0.08 or darkRatio >= 0.10


def _maskedContentImageHash(
    contentImage: Image.Image,
    mask: list[bool],
    hashSize: int,
    *,
    requireDetailedContent: bool,
) -> str:
    width, height = contentImage.size
    pixels = list(contentImage.getdata())
    contentIndexes = [index for index, keep in enumerate(mask) if keep]
    if len(contentIndexes) < max(160, int(width * height * 0.02)):
        return ""

    stats = _contentStats(pixels, contentIndexes)
    if requireDetailedContent and not _hasDetailedContent(stats):
        return ""

    xs = [index % width for index in contentIndexes]
    ys = [index // width for index in contentIndexes]
    left = max(0, min(xs) - 2)
    right = min(width - 1, max(xs) + 2)
    top = max(0, min(ys) - 2)
    bottom = min(height - 1, max(ys) + 2)
    cropWidth = max(1, right - left + 1)
    cropHeight = max(1, bottom - top + 1)
    lumas = list(stats.get("lumas") or [])
    if not lumas:
        return ""
    sortedLumas = sorted(int(value) for value in lumas)
    fill = sortedLumas[len(sortedLumas) // 2]
    grayscale = Image.new("L", (cropWidth, cropHeight), color=int(fill))
    for index in contentIndexes:
        x = index % width
        y = index // width
        if x < left or x > right or y < top or y > bottom:
            continue
        red, green, blue, _ = pixels[index]
        luma = int(round((0.299 * int(red)) + (0.587 * int(green)) + (0.114 * int(blue))))
        grayscale.putpixel((x - left, y - top), luma)

    resized = grayscale.resize((hashSize + 1, hashSize), Image.Resampling.LANCZOS)
    smallPixels = list(resized.getdata())
    bits = 0
    for y in range(hashSize):
        rowStart = y * (hashSize + 1)
        for x in range(hashSize):
            bits = (bits << 1) | (1 if int(smallPixels[rowStart + x]) > int(smallPixels[rowStart + x + 1]) else 0)
    hexLength = max(1, (hashSize * hashSize + 3) // 4)
    return format(bits, f"0{hexLength}x")


def _contentImageHash(image: Image.Image, hashSize: int) -> str:
    detailedImage, detailedMask = _thumbnailContentMask(image, removeLargeNeutral=True)
    detailedHash = _maskedContentImageHash(
        detailedImage,
        detailedMask,
        hashSize,
        requireDetailedContent=True,
    )
    if detailedHash:
        return detailedHash

    neutralImage, neutralMask = _thumbnailContentMask(image, removeLargeNeutral=False)
    return _maskedContentImageHash(
        neutralImage,
        neutralMask,
        hashSize,
        requireDetailedContent=False,
    )


def _imageColorSignature(image: Image.Image) -> str:
    resized, mask = _thumbnailContentMask(image, removeLargeNeutral=True)
    pixels = list(resized.getdata())
    contentIndexes = [index for index, keep in enumerate(mask) if keep]
    mode = "detailed"
    if len(contentIndexes) < 160 or not _hasDetailedContent(_contentStats(pixels, contentIndexes)):
        resized, mask = _thumbnailContentMask(image, removeLargeNeutral=False)
        pixels = list(resized.getdata())
        contentIndexes = [index for index, keep in enumerate(mask) if keep]
        mode = "low_neutral"

    bins = [0.0] * 15
    includedPixels = 0
    saturatedPixels = 0
    darkPixels = 0
    for keep, (red, green, blue, alpha) in zip(mask, pixels):
        if not keep or int(alpha) < 32:
            continue
        redFloat = int(red) / 255.0
        greenFloat = int(green) / 255.0
        blueFloat = int(blue) / 255.0
        hue, saturation, value = colorsys.rgb_to_hsv(redFloat, greenFloat, blueFloat)
        includedPixels += 1
        if saturation >= 0.18:
            saturatedPixels += 1
        if value <= 0.45:
            darkPixels += 1
        if saturation < 0.18:
            if value < 0.33:
                bins[12] += 1.0
            elif value < 0.68:
                bins[13] += 1.0
            else:
                bins[14] += 1.0
            continue
        hueBin = int(hue * 12.0) % 12
        bins[hueBin] += 0.70
        bins[(hueBin - 1) % 12] += 0.15
        bins[(hueBin + 1) % 12] += 0.15
    total = sum(bins)
    if includedPixels < 160 or total <= 0:
        return ""
    scaled = [int(round((value / total) * 1000.0)) for value in bins]
    stats = _contentStats(pixels, contentIndexes) if contentIndexes else {}
    payload = {
        "v": _colorSignatureVersion(),
        "bins": scaled,
        "mode": mode,
        "contrast": int(stats.get("contrast") or 0),
        "saturation": int(round((saturatedPixels / max(1, includedPixels)) * 1000)),
        "dark": int(round((darkPixels / max(1, includedPixels)) * 1000)),
    }
    return json.dumps(payload, separators=(",", ":"))


def _parseColorSignaturePayload(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return value


def _parseColorSignature(value: object) -> list[float]:
    payload = _parseColorSignaturePayload(value)
    if isinstance(payload, dict):
        payload = payload.get("bins")
    if not isinstance(payload, list) or len(payload) != 15:
        return []
    vector: list[float] = []
    for raw in payload:
        try:
            vector.append(max(0.0, float(raw)))
        except (TypeError, ValueError):
            return []
    total = sum(vector)
    if total <= 0:
        return []
    return [value / total for value in vector]


def _colorSignatureMode(value: object) -> str:
    payload = _parseColorSignaturePayload(value)
    if not isinstance(payload, dict):
        return "detailed" if _parseColorSignature(payload) else ""
    mode = str(payload.get("mode") or "").strip().lower()
    return mode if mode in {"detailed", "low_neutral"} else ""


def _visualSignatureDetailCompatible(left: object, right: object) -> bool:
    leftMode = _colorSignatureMode(left)
    rightMode = _colorSignatureMode(right)
    if not leftMode or not rightMode:
        return True
    return leftMode == rightMode


def _colorSignatureDistance(left: object, right: object) -> Optional[float]:
    leftVector = _parseColorSignature(left)
    rightVector = _parseColorSignature(right)
    if not leftVector or not rightVector or len(leftVector) != len(rightVector):
        return None
    overlap = sum(min(leftValue, rightValue) for leftValue, rightValue in zip(leftVector, rightVector))
    return max(0.0, min(1.0, 1.0 - overlap))


def _imageHashDistance(left: str, right: str) -> Optional[int]:
    if not left or not right:
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


async def _fetchThumbnailImageBytes(url: str) -> bytes:
    return await robloxTransport.fetchBytes(url, timeoutSec=10, errorPrefix="Thumbnail fetch failed")


async def fetchRobloxAssetThumbnails(
    assetIds: list[int],
    *,
    size: str = "420x420",
    imageFormat: str = "Png",
    returnPolicy: str = "PlaceHolder",
) -> RobloxAssetThumbnailsResult:
    uniqueIds = sorted({int(assetId) for assetId in list(assetIds or []) if int(assetId or 0) > 0})
    if not uniqueIds:
        return RobloxAssetThumbnailsResult([], 200)

    cachedRows: list[dict] = []
    missingIds: list[int] = []
    for assetId in uniqueIds:
        cached = _cacheGet(
            "asset_thumbnail",
            int(assetId),
            ttlName="robloxAssetThumbnailCacheTtlSec",
            defaultTtlSec=86400,
        )
        if isinstance(cached, dict):
            cachedRows.append(dict(cached))
        else:
            missingIds.append(int(assetId))

    rows = list(cachedRows)
    if not missingIds:
        return RobloxAssetThumbnailsResult(rows, 200)

    url = "https://thumbnails.roblox.com/v1/assets"
    headers = {
        "Accept": "application/json",
        "User-Agent": str(
            getattr(
                config,
                "robloxPublicApiUserAgent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jane-Clanker/1.0",
            )
        ),
    }

    errors: list[str] = []
    status = 200
    for start in range(0, len(missingIds), 100):
        batch = missingIds[start : start + 100]
        params = {
            "assetIds": ",".join(str(assetId) for assetId in batch),
            "returnPolicy": returnPolicy,
            "size": size,
            "format": imageFormat,
            "isCircular": "false",
        }
        try:
            batchStatus, data = await _requestJson("GET", url, headers=headers, params=params, timeoutSec=10)
        except Exception as exc:
            return RobloxAssetThumbnailsResult(rows, 0, error=str(exc))
        status = int(batchStatus or status or 0)
        if batchStatus != 200 or not isinstance(data, dict):
            return RobloxAssetThumbnailsResult(rows, status, error=f"Asset thumbnail lookup failed ({batchStatus}).")
        rawRows = data.get("data")
        if not isinstance(rawRows, list):
            return RobloxAssetThumbnailsResult(rows, status, error="Asset thumbnail lookup returned invalid data.")
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
                "asset_thumbnail",
                int(targetId),
                dict(row),
                ttlName="robloxAssetThumbnailCacheTtlSec",
                defaultTtlSec=86400,
    )
    return RobloxAssetThumbnailsResult(rows, status, error="; ".join(errors[:3]) or None)


async def validateRobloxAssetVisualReferences(assetIds: list[int]) -> list[dict]:
    uniqueIds = sorted({int(assetId) for assetId in list(assetIds or []) if int(assetId or 0) > 0})
    if not uniqueIds:
        return []

    thumbnailResult = await fetchRobloxAssetThumbnails(uniqueIds)
    thumbnailRows = {
        int(row.get("id")): row
        for row in list(thumbnailResult.thumbnails or [])
        if isinstance(row, dict) and _optionalInt(row.get("id")) is not None
    }
    hashSize = _inventoryVisualHashSize()
    semaphore = asyncio.Semaphore(_assetThumbnailHashConcurrency())

    async def _validateAsset(assetId: int) -> dict:
        row = thumbnailRows.get(int(assetId))
        base = {
            "assetId": int(assetId),
            "thumbnailUrl": None,
            "thumbnailState": None,
            "thumbnailHash": None,
            "colorSignature": None,
            "colorSignatureVersion": _colorSignatureVersion(),
            "hashSize": int(hashSize),
            "validationState": "PENDING",
            "validationError": None,
            "lastValidatedAt": _utcNow().replace(microsecond=0).isoformat(),
        }
        if not isinstance(row, dict):
            base["validationState"] = "INVALID"
            base["validationError"] = "Thumbnail metadata missing."
            return base

        imageUrl = str(row.get("imageUrl") or "").strip()
        state = str(row.get("state") or "").strip().lower()
        base["thumbnailUrl"] = imageUrl or None
        base["thumbnailState"] = state or None

        if not imageUrl:
            base["validationState"] = "INVALID"
            base["validationError"] = "Thumbnail image URL missing."
            return base
        if state not in {"completed", ""}:
            base["validationState"] = "INVALID"
            base["validationError"] = f"Thumbnail state was `{state or 'unknown'}`."
            return base

        cachedHash = _cacheGet(
            "asset_thumbnail_hash",
            _thumbnailHashCacheKey(int(assetId), int(hashSize)),
            ttlName="robloxAssetThumbnailHashCacheTtlSec",
            defaultTtlSec=86400,
        )
        cachedColorSignature = _cacheGet(
            "asset_thumbnail_color_signature",
            _thumbnailColorSignatureCacheKey(int(assetId), int(hashSize)),
            ttlName="robloxAssetThumbnailHashCacheTtlSec",
            defaultTtlSec=86400,
        )
        if isinstance(cachedHash, str) and cachedHash and isinstance(cachedColorSignature, str) and cachedColorSignature:
            base["thumbnailHash"] = cachedHash
            base["colorSignature"] = cachedColorSignature
            base["validationState"] = "VALID"
            return base

        try:
            async with semaphore:
                imageBytes = await _fetchThumbnailImageBytes(imageUrl)
            with Image.open(BytesIO(imageBytes)) as image:
                hashValue = cachedHash if isinstance(cachedHash, str) and cachedHash else _contentImageHash(image, hashSize)
                colorSignature = _imageColorSignature(image)
        except Exception as exc:
            base["validationState"] = "ERROR"
            base["validationError"] = str(exc)
            return base

        if not hashValue:
            base["validationState"] = "INVALID"
            base["validationError"] = "Thumbnail hash was empty."
            return base

        _cacheSet(
            "asset_thumbnail_hash",
            _thumbnailHashCacheKey(int(assetId), int(hashSize)),
            hashValue,
            ttlName="robloxAssetThumbnailHashCacheTtlSec",
            defaultTtlSec=86400,
        )
        if colorSignature:
            _cacheSet(
                "asset_thumbnail_color_signature",
                _thumbnailColorSignatureCacheKey(int(assetId), int(hashSize)),
                colorSignature,
                ttlName="robloxAssetThumbnailHashCacheTtlSec",
                defaultTtlSec=86400,
            )
        base["thumbnailHash"] = hashValue
        base["colorSignature"] = colorSignature or None
        base["validationState"] = "VALID"
        return base

    results = await asyncio.gather(*[_validateAsset(assetId) for assetId in uniqueIds], return_exceptions=True)
    rows: list[dict] = []
    for assetId, result in zip(uniqueIds, results):
        if isinstance(result, Exception):
            rows.append(
                {
                    "assetId": int(assetId),
                    "thumbnailUrl": None,
                    "thumbnailState": None,
                    "thumbnailHash": None,
                    "colorSignature": None,
                    "colorSignatureVersion": _colorSignatureVersion(),
                    "hashSize": int(hashSize),
                    "validationState": "ERROR",
                    "validationError": str(result),
                    "lastValidatedAt": _utcNow().replace(microsecond=0).isoformat(),
                }
            )
            continue
        rows.append(dict(result))
    return rows
async def fetchRobloxAssetThumbnailHashes(assetIds: list[int]) -> tuple[dict[int, str], Optional[str]]:
    rows = await validateRobloxAssetVisualReferences(assetIds)
    hashes: dict[int, str] = {}
    errors: list[str] = []
    for row in rows:
        assetId = _optionalInt(row.get("assetId"))
        thumbnailHash = str(row.get("thumbnailHash") or "").strip()
        if assetId is not None and thumbnailHash:
            hashes[int(assetId)] = thumbnailHash
            continue
        state = str(row.get("validationState") or "").strip().upper() or "UNKNOWN"
        errorText = str(row.get("validationError") or "").strip() or state.title()
        if assetId is not None:
            errors.append(f"{int(assetId)}: {errorText}")
        else:
            errors.append(errorText)
    return hashes, "; ".join(errors[:3]) or None

async def fetchRobloxAssetVisualSignatures(assetIds: list[int]) -> tuple[dict[int, dict[str, object]], Optional[str]]:
    rows = await validateRobloxAssetVisualReferences(assetIds)
    signatures: dict[int, dict[str, object]] = {}
    errors: list[str] = []
    for row in rows:
        assetId = _optionalInt(row.get("assetId"))
        thumbnailHash = str(row.get("thumbnailHash") or "").strip()
        if assetId is not None and thumbnailHash:
            signatures[int(assetId)] = {
                "thumbnailHash": thumbnailHash,
                "colorSignature": str(row.get("colorSignature") or "").strip(),
                "colorSignatureVersion": int(row.get("colorSignatureVersion") or 0),
            }
            continue
        state = str(row.get("validationState") or "").strip().upper() or "UNKNOWN"
        errorText = str(row.get("validationError") or "").strip() or state.title()
        if assetId is not None:
            errors.append(f"{int(assetId)}: {errorText}")
        else:
            errors.append(errorText)
    return signatures, "; ".join(errors[:3]) or None

fetchCatalogAssetPrices = _fetchCatalogAssetPrices
imageHashDistance = _imageHashDistance
colorSignatureDistance = _colorSignatureDistance
visualSignatureDetailCompatible = _visualSignatureDetailCompatible
colorSignatureVersion = _colorSignatureVersion
fetchRobloxAssetVisualSignatures = fetchRobloxAssetVisualSignatures
