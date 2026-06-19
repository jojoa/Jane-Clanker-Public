from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import config
from features.staff.sessions.Roblox import (
    robloxAssets,
    robloxInventoryMatching,
    robloxInventorySources,
    robloxPayloads,
    robloxTransport,
)
from features.staff.sessions.Roblox.robloxModels import RobloxInventoryResult

_cacheGet = robloxTransport.cacheGet
_cacheSet = robloxTransport.cacheSet
_requestJson = robloxTransport.requestJson
_extractAssetId = robloxPayloads.extractAssetId
_extractCreatorId = robloxPayloads.extractCreatorId
_extractCreatorType = robloxPayloads.extractCreatorType
_extractGamepassId = robloxPayloads.extractGamepassId
_extractRobuxPrice = robloxPayloads.extractRobuxPrice
_isGamepassInventoryItem = robloxPayloads.isGamepassInventoryItem
_fetchCatalogAssetPrices = robloxAssets.fetchCatalogAssetPrices
_fetchPublicInventoryAssets = robloxInventorySources.fetchPublicInventoryAssets
_inventoryFuzzyMatchingEnabled = robloxInventoryMatching.inventoryFuzzyMatchingEnabled
_inventoryFuzzyScoreCutoff = robloxInventoryMatching.inventoryFuzzyScoreCutoff
_inventoryFuzzyMinKeywordLength = robloxInventoryMatching.inventoryFuzzyMinKeywordLength
_inventoryMatchSummary = robloxInventoryMatching.inventoryMatchSummary
_appendInventoryVisualCandidate = robloxInventoryMatching.appendInventoryVisualCandidate
_applyInventoryVisualMatches = robloxInventoryMatching.applyInventoryVisualMatches
_inventoryMatchEntry = robloxInventoryMatching.inventoryMatchEntry

ProgressCallback = Callable[[str], Awaitable[Any]]


def _isSelfCreatedByUser(details: dict, ownerId: int) -> bool:
    creatorId = _extractCreatorId(details)
    if ownerId <= 0 or creatorId != ownerId:
        return False
    creatorType = str(_extractCreatorType(details) or "").strip().lower()
    return creatorType in {"", "user"}


def _inventoryHardMaxPages() -> int:
    try:
        configured = int(getattr(config, "bgIntelligenceInventoryHardMaxPages", 100) or 100)
    except (TypeError, ValueError):
        configured = 100
    return max(1, min(configured, 500))


async def _emitProgress(progressCallback: ProgressCallback | None, status: str) -> None:
    if progressCallback is None:
        return
    try:
        await progressCallback(status)
    except Exception:
        return


def _inventoryProgressStatus(detail: str) -> str:
    cleanDetail = str(detail or "").strip()
    if not cleanDetail:
        return "Reviewing inventory and item values..."
    return f"Reviewing inventory and item values [{cleanDetail}]..."


def _inventoryValueSummary(
    prices: dict[int, dict],
    *,
    ownerRobloxUserId: int,
    uniqueAssetCount: int,
) -> dict:
    totalValue = 0
    pricedCount = 0
    selfCreatedAssetCount = 0
    selfCreatedPricedCount = 0
    selfCreatedValueExcluded = 0
    ownerId = int(ownerRobloxUserId or 0)
    for details in dict(prices or {}).values():
        if not isinstance(details, dict):
            continue
        price = _extractRobuxPrice(details)
        if _isSelfCreatedByUser(details, ownerId):
            selfCreatedAssetCount += 1
            if price is not None:
                selfCreatedPricedCount += 1
                selfCreatedValueExcluded += int(price)
            continue
        if price is None:
            continue
        totalValue += int(price)
        pricedCount += 1
    return {
        "knownValueRobux": totalValue,
        "pricedAssetCount": pricedCount,
        "unpricedAssetCount": max(0, int(uniqueAssetCount or 0) - pricedCount - selfCreatedPricedCount),
        "selfCreatedAssetCount": selfCreatedAssetCount,
        "selfCreatedPricedAssetCount": selfCreatedPricedCount,
        "selfCreatedRobuxExcluded": selfCreatedValueExcluded,
    }


async def _buildPublicInventoryResult(
    robloxUserId: int,
    *,
    targetItemIds: Optional[set[int]],
    targetCreatorIds: Optional[set[int]],
    targetKeywords: Optional[list[str]],
    visualReferenceHashes: Optional[dict[int, str]],
    visualReferenceColorSignatures: Optional[dict[int, str]],
    visualReferenceMetadata: Optional[dict[int, dict[str, object]]],
    maxPages: int,
    progressCallback: ProgressCallback | None = None,
) -> RobloxInventoryResult:
    remaining = set(targetItemIds) if targetItemIds else None
    creatorIds = set(targetCreatorIds) if targetCreatorIds else set()
    keywords = [
        str(value).strip().lower()
        for value in (targetKeywords or [])
        if str(value).strip()
    ]
    try:
        pagesPerType = int(maxPages or 0)
    except (TypeError, ValueError):
        pagesPerType = 0
    if pagesPerType <= 0:
        pagesPerType = max(1, min(int(getattr(config, "bgIntelligencePublicInventoryMaxPagesPerType", 10) or 10), 100))
    await _emitProgress(progressCallback, _inventoryProgressStatus("scanning public inventory pages"))
    rawRows, status, error, complete = await _fetchPublicInventoryAssets(
        int(robloxUserId),
        maxPagesPerType=pagesPerType,
    )
    assetIds: list[int] = []
    gamepassIds: list[int] = []
    itemsById: dict[int, dict] = {}
    visualCandidates: list[dict] = []
    visualCandidateIds: set[int] = set()
    for raw in rawRows:
        if not isinstance(raw, dict):
            continue
        _appendInventoryVisualCandidate(visualCandidates, visualCandidateIds, raw)
        entry, assetId, gamepassId = _inventoryMatchEntry(
            raw,
            remaining=remaining,
            creatorIds=creatorIds,
            keywords=keywords,
        )
        if assetId is not None:
            assetIds.append(int(assetId))
        if gamepassId is not None:
            gamepassIds.append(int(gamepassId))
        if entry is not None:
            itemsById[int(entry.get("id") or 0)] = entry

    await _emitProgress(progressCallback, _inventoryProgressStatus("comparing inventory thumbnails"))
    visualSummary = await _applyInventoryVisualMatches(
        flaggedItemsById=itemsById,
        candidateItems=visualCandidates,
        referenceItemIds=set(targetItemIds or set()),
        referenceHashes=visualReferenceHashes,
        referenceColorSignatures=visualReferenceColorSignatures,
        referenceMetadata=visualReferenceMetadata,
    )
    items = list(itemsById.values())

    await _emitProgress(progressCallback, _inventoryProgressStatus("pricing owned assets"))
    prices, priceError = await _fetchCatalogAssetPrices(assetIds)
    uniqueAssetCount = len(set(assetIds))
    valueSummary = _inventoryValueSummary(
        prices,
        ownerRobloxUserId=int(robloxUserId),
        uniqueAssetCount=uniqueAssetCount,
    )
    matchSummary = _inventoryMatchSummary(items)
    summary = {
        "status": "OK" if rawRows or not error else "ERROR",
        "itemsScanned": len(rawRows),
        "pagesScanned": pagesPerType,
        "assetCount": len(assetIds),
        "uniqueAssetCount": uniqueAssetCount,
        "gamepassCount": len(gamepassIds),
        "uniqueGamepassCount": len(set(gamepassIds)),
        "ownedGamepassIds": sorted(set(gamepassIds)),
        "gamepassesExcluded": True,
        "complete": complete,
        "requestedAllPages": maxPages <= 0,
        "priceError": priceError,
        "valueSource": "Roblox public inventory and economy asset details current price",
        "visualCandidateCount": int(visualSummary.get("candidateCount") or 0),
        "visualComparedCandidateCount": int(visualSummary.get("comparedCandidateCount") or 0),
        "visualReferenceCount": int(visualSummary.get("referenceCount") or 0),
        "visualMatchedCount": int(visualSummary.get("matchedCount") or 0),
        "visualTypeMismatchSkippedCount": int(visualSummary.get("skippedTypeMismatchCount") or 0),
        "visualUnknownTypeSkippedCount": int(visualSummary.get("skippedUnknownTypeCount") or 0),
        "visualColorMismatchSkippedCount": int(visualSummary.get("skippedColorMismatchCount") or 0),
        "visualDetailMismatchSkippedCount": int(visualSummary.get("skippedDetailMismatchCount") or 0),
        "visualError": visualSummary.get("error"),
    }
    summary.update(valueSummary)
    summary.update(matchSummary)
    finalError = error if not rawRows else None
    return RobloxInventoryResult(items, status or 200, error=finalError, summary=summary)

async def fetchRobloxInventory(
    robloxUserId: int,
    targetItemIds: Optional[set[int]] = None,
    targetCreatorIds: Optional[set[int]] = None,
    targetKeywords: Optional[list[str]] = None,
    visualReferenceHashes: Optional[dict[int, str]] = None,
    visualReferenceColorSignatures: Optional[dict[int, str]] = None,
    visualReferenceMetadata: Optional[dict[int, dict[str, object]]] = None,
    maxPages: int = 5,
    includeValue: bool = False,
    progressCallback: ProgressCallback | None = None,
) -> RobloxInventoryResult:
    apiKey = getattr(config, "robloxInventoryApiKey", "") or getattr(config, "robloxOpenCloudApiKey", "")
    cacheKey = None
    if includeValue:
        normalizedKeywords = tuple(
            sorted(str(value).strip().lower() for value in (targetKeywords or []) if str(value).strip())
        )
        matchVersion = (
            "v3",
            int(_inventoryFuzzyMatchingEnabled()),
            int(round(_inventoryFuzzyScoreCutoff())),
            int(_inventoryFuzzyMinKeywordLength()),
        )
        cacheKey = (
            "opencloud" if apiKey else "public",
            int(robloxUserId or 0),
            int(maxPages or 0),
            tuple(sorted(int(value) for value in (targetItemIds or set()))),
            tuple(sorted(int(value) for value in (targetCreatorIds or set()))),
            normalizedKeywords,
            tuple(
                sorted(
                    (int(assetId), str(hashValue).strip())
                    for assetId, hashValue in dict(visualReferenceHashes or {}).items()
                    if int(assetId or 0) > 0 and str(hashValue).strip()
                )
            ),
            tuple(
                sorted(
                    (int(assetId), str(colorSignature).strip())
                    for assetId, colorSignature in dict(visualReferenceColorSignatures or {}).items()
                    if int(assetId or 0) > 0 and str(colorSignature).strip()
                )
            ),
            tuple(
                sorted(
                    (
                        int(assetId),
                        int((details or {}).get("assetTypeId") or 0),
                        str((details or {}).get("visualCategory") or "").strip(),
                    )
                    for assetId, details in dict(visualReferenceMetadata or {}).items()
                    if int(assetId or 0) > 0 and isinstance(details, dict)
                )
            ),
            matchVersion,
        )
        cached = _cacheGet(
            "inventory_value",
            cacheKey,
            ttlName="robloxInventoryValueCacheTtlSec",
            defaultTtlSec=21600,
        )
        if isinstance(cached, RobloxInventoryResult):
            return cached
    if not apiKey:
        if includeValue:
            result = await _buildPublicInventoryResult(
                int(robloxUserId),
                targetItemIds=targetItemIds,
                targetCreatorIds=targetCreatorIds,
                targetKeywords=targetKeywords,
                visualReferenceHashes=visualReferenceHashes,
                visualReferenceColorSignatures=visualReferenceColorSignatures,
                visualReferenceMetadata=visualReferenceMetadata,
                maxPages=maxPages,
                progressCallback=progressCallback,
            )
            if cacheKey is not None and not result.error:
                _cacheSet(
                    "inventory_value",
                    cacheKey,
                    result,
                    ttlName="robloxInventoryValueCacheTtlSec",
                    defaultTtlSec=21600,
                )
            return result
        return RobloxInventoryResult([], 0, error="Missing Roblox Open Cloud API key for inventory.")

    url = f"https://apis.roblox.com/cloud/v2/users/{robloxUserId}/inventory-items"
    headers = {"x-api-key": apiKey}
    params = {"maxPageSize": "100"}
    items: list[dict] = []
    itemsById: dict[int, dict] = {}
    pageCount = 0
    remaining = set(targetItemIds) if targetItemIds else None
    creatorIds = set(targetCreatorIds) if targetCreatorIds else set()
    keywords = [
        str(value).strip().lower()
        for value in (targetKeywords or [])
        if str(value).strip()
    ]
    probeOnly = remaining is None and not creatorIds and not keywords and not includeValue
    try:
        normalizedMaxPages = int(maxPages or 0)
    except (TypeError, ValueError):
        normalizedMaxPages = 5
    if normalizedMaxPages <= 0:
        pageLimit = _inventoryHardMaxPages()
        requestedAllPages = True
    else:
        pageLimit = max(1, normalizedMaxPages)
        requestedAllPages = False

    totalItemsScanned = 0
    assetIds: list[int] = []
    gamepassIds: list[int] = []
    visualCandidates: list[dict] = []
    visualCandidateIds: set[int] = set()
    lastStatus = 200
    nextToken: Optional[str] = None

    def _summary(*, status: str = "OK", priceError: Optional[str] = None) -> dict:
        uniqueAssetIds = sorted(set(assetIds))
        uniqueGamepassIds = sorted(set(gamepassIds))
        summary = {
            "status": status,
            "itemsScanned": totalItemsScanned,
            "pagesScanned": pageCount,
            "assetCount": len(assetIds),
            "uniqueAssetCount": len(uniqueAssetIds),
            "gamepassCount": len(gamepassIds),
            "uniqueGamepassCount": len(uniqueGamepassIds),
            "ownedGamepassIds": uniqueGamepassIds,
            "knownValueRobux": 0,
            "pricedAssetCount": 0,
            "unpricedAssetCount": len(uniqueAssetIds),
            "selfCreatedAssetCount": 0,
            "selfCreatedPricedAssetCount": 0,
            "selfCreatedRobuxExcluded": 0,
            "gamepassesExcluded": True,
            "complete": not bool(nextToken),
            "nextPageToken": nextToken,
            "requestedAllPages": requestedAllPages,
            "priceError": priceError,
            "valueSource": "Roblox economy asset details current price",
            "visualCandidateCount": 0,
            "visualComparedCandidateCount": 0,
            "visualReferenceCount": 0,
            "visualMatchedCount": 0,
            "visualTypeMismatchSkippedCount": 0,
            "visualUnknownTypeSkippedCount": 0,
            "visualColorMismatchSkippedCount": 0,
            "visualDetailMismatchSkippedCount": 0,
            "visualError": None,
        }
        summary.update(_inventoryMatchSummary(list(itemsById.values())))
        return summary

    try:
        await _emitProgress(progressCallback, _inventoryProgressStatus("scanning inventory pages"))
        while True:
            if pageCount >= pageLimit:
                break
            status, data = await _requestJson("GET", url, headers=headers, params=params, timeoutSec=10)
            lastStatus = int(status or 0)
            if status != 200 or not isinstance(data, dict):
                detail = None
                if isinstance(data, dict):
                    detail = data.get("message") or data.get("error")
                    if not detail and isinstance(data.get("errors"), list) and data["errors"]:
                        first = data["errors"][0]
                        if isinstance(first, dict):
                            detail = first.get("message") or first.get("error")
                        elif isinstance(first, str):
                            detail = first
                if detail:
                    return RobloxInventoryResult(
                        items,
                        status,
                        error=f"Inventory lookup failed ({status}): {detail}",
                        summary=_summary(status="ERROR"),
                    )
                return RobloxInventoryResult(
                    items,
                    status,
                    error=f"Inventory lookup failed ({status}).",
                    summary=_summary(status="ERROR"),
                )

            rawItems = data.get("inventoryItems") or data.get("items") or []
            if probeOnly:
                return RobloxInventoryResult([], status)
            if isinstance(rawItems, list):
                for raw in rawItems:
                    if not isinstance(raw, dict):
                        continue
                    totalItemsScanned += 1
                    _appendInventoryVisualCandidate(visualCandidates, visualCandidateIds, raw)
                    if _isGamepassInventoryItem(raw):
                        gamepassId = _extractGamepassId(raw)
                        if includeValue and gamepassId is not None:
                            gamepassIds.append(int(gamepassId))
                        continue
                    assetId = _extractAssetId(raw)
                    if assetId is None:
                        continue
                    if includeValue:
                        assetIds.append(int(assetId))
                    entry, _, _ = _inventoryMatchEntry(
                        raw,
                        remaining=remaining,
                        creatorIds=creatorIds,
                        keywords=keywords,
                    )
                    if entry is None:
                        continue
                    itemsById[int(entry.get("id") or 0)] = entry

            nextToken = data.get("nextPageToken")
            pageCount += 1
            if not includeValue and remaining is not None and not remaining and not creatorIds and not keywords:
                break
            if not nextToken:
                break
            params["pageToken"] = nextToken
    except Exception as exc:
        return RobloxInventoryResult(list(itemsById.values()), 0, error=str(exc), summary=_summary(status="ERROR"))

    await _emitProgress(progressCallback, _inventoryProgressStatus("comparing inventory thumbnails"))
    visualSummary = await _applyInventoryVisualMatches(
        flaggedItemsById=itemsById,
        candidateItems=visualCandidates,
        referenceItemIds=set(targetItemIds or set()),
        referenceHashes=visualReferenceHashes,
        referenceColorSignatures=visualReferenceColorSignatures,
        referenceMetadata=visualReferenceMetadata,
    )
    items = list(itemsById.values())

    if not includeValue:
        return RobloxInventoryResult(items, lastStatus or 200)

    summary = _summary(status="OK")
    summary["visualCandidateCount"] = int(visualSummary.get("candidateCount") or 0)
    summary["visualComparedCandidateCount"] = int(visualSummary.get("comparedCandidateCount") or 0)
    summary["visualReferenceCount"] = int(visualSummary.get("referenceCount") or 0)
    summary["visualMatchedCount"] = int(visualSummary.get("matchedCount") or 0)
    summary["visualTypeMismatchSkippedCount"] = int(visualSummary.get("skippedTypeMismatchCount") or 0)
    summary["visualUnknownTypeSkippedCount"] = int(visualSummary.get("skippedUnknownTypeCount") or 0)
    summary["visualColorMismatchSkippedCount"] = int(visualSummary.get("skippedColorMismatchCount") or 0)
    summary["visualDetailMismatchSkippedCount"] = int(visualSummary.get("skippedDetailMismatchCount") or 0)
    summary["visualError"] = visualSummary.get("error")
    await _emitProgress(progressCallback, _inventoryProgressStatus("pricing owned assets"))
    prices, priceError = await _fetchCatalogAssetPrices(assetIds)
    if priceError:
        summary["priceError"] = priceError
    if prices:
        summary.update(
            _inventoryValueSummary(
                prices,
                ownerRobloxUserId=int(robloxUserId),
                uniqueAssetCount=int(summary.get("uniqueAssetCount") or 0),
            )
        )
    result = RobloxInventoryResult(items, lastStatus or 200, summary=summary)
    if cacheKey is not None:
        _cacheSet(
            "inventory_value",
            cacheKey,
            result,
            ttlName="robloxInventoryValueCacheTtlSec",
            defaultTtlSec=21600,
        )
    return result
