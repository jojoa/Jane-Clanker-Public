from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from db.sqlite import execute, fetchAll, fetchOne

log = logging.getLogger(__name__)


def _safeInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed


def _normalizeFeatureKey(value: object) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_", "."})


def _commandRoot(commandName: str) -> str:
    normalized = _normalizeFeatureKey(commandName)
    if not normalized:
        return ""
    return normalized.split(" ", 1)[0].split(".", 1)[0]


class FeatureFlagService:
    def __init__(self, *, configModule: Any):
        self.config = configModule
        self._cache: dict[tuple[int, str], tuple[bool, datetime]] = {}
        self._cacheTtl = timedelta(seconds=60)
        self._backgroundRefreshes: set[tuple[int, str]] = set()

    def _defaultEnabled(self, featureKey: str) -> bool:
        defaults = getattr(self.config, "featureFlagDefaults", {}) or {}
        if isinstance(defaults, dict):
            raw = defaults.get(featureKey)
            if raw is not None:
                return bool(raw)
        return True

    def _commandFeatureKey(self, commandName: str) -> str:
        root = _commandRoot(commandName)
        if not root:
            return ""
        # Names are command-root level by default, e.g. "recruitment", "ribbons", "best-of".
        return root

    def _cacheKey(self, guildId: int, featureKey: str) -> tuple[int, str]:
        return int(guildId), featureKey

    def _readCache(self, guildId: int, featureKey: str) -> bool | None:
        key = self._cacheKey(guildId, featureKey)
        cached = self._cache.get(key)
        if cached is None:
            return None
        value, expiresAt = cached
        if datetime.now(timezone.utc) >= expiresAt:
            self._cache.pop(key, None)
            return None
        return bool(value)

    def _writeCache(self, guildId: int, featureKey: str, enabled: bool) -> None:
        key = self._cacheKey(guildId, featureKey)
        self._cache[key] = (
            bool(enabled),
            datetime.now(timezone.utc) + self._cacheTtl,
        )

    async def getFlag(self, guildId: int, featureKey: str) -> bool:
        safeGuildId = _safeInt(guildId)
        safeFeatureKey = _normalizeFeatureKey(featureKey)
        if safeGuildId <= 0 or not safeFeatureKey:
            return True

        cached = self._readCache(safeGuildId, safeFeatureKey)
        if cached is not None:
            return cached

        row = await fetchOne(
            """
            SELECT enabled
            FROM guild_feature_flags
            WHERE guildId = ? AND featureKey = ?
            """,
            (safeGuildId, safeFeatureKey),
        )
        if not row:
            value = self._defaultEnabled(safeFeatureKey)
            self._writeCache(safeGuildId, safeFeatureKey, value)
            return value

        value = bool(int(row.get("enabled") or 0))
        self._writeCache(safeGuildId, safeFeatureKey, value)
        return value

    async def setFlag(
        self,
        *,
        guildId: int,
        featureKey: str,
        enabled: bool,
        actorId: int = 0,
        note: str = "",
    ) -> None:
        safeGuildId = _safeInt(guildId)
        safeFeatureKey = _normalizeFeatureKey(featureKey)
        if safeGuildId <= 0 or not safeFeatureKey:
            return
        nowIso = datetime.now(timezone.utc).isoformat()
        await execute(
            """
            INSERT INTO guild_feature_flags (
                guildId, featureKey, enabled, updatedBy, updatedAt, note
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guildId, featureKey)
            DO UPDATE SET
                enabled = excluded.enabled,
                updatedBy = excluded.updatedBy,
                updatedAt = excluded.updatedAt,
                note = excluded.note
            """,
            (
                safeGuildId,
                safeFeatureKey,
                1 if bool(enabled) else 0,
                _safeInt(actorId),
                nowIso,
                str(note or "")[:400],
            ),
        )
        self._writeCache(safeGuildId, safeFeatureKey, bool(enabled))

    async def listFlags(self, guildId: int) -> list[dict[str, Any]]:
        safeGuildId = _safeInt(guildId)
        if safeGuildId <= 0:
            return []
        rows = await fetchAll(
            """
            SELECT guildId, featureKey, enabled, updatedBy, updatedAt, note
            FROM guild_feature_flags
            WHERE guildId = ?
            ORDER BY featureKey ASC
            """,
            (safeGuildId,),
        )
        return rows

    async def isCommandEnabled(self, guildId: int, commandName: str) -> tuple[bool, str]:
        featureKey = self._commandFeatureKey(commandName)
        if not featureKey:
            return True, ""
        enabled = await self.getFlag(guildId, featureKey)
        return enabled, featureKey

    def isCommandEnabledCached(self, guildId: int, commandName: str) -> tuple[bool, str, bool]:
        featureKey = self._commandFeatureKey(commandName)
        safeGuildId = _safeInt(guildId)
        if safeGuildId <= 0 or not featureKey:
            return True, featureKey, True
        cached = self._readCache(safeGuildId, featureKey)
        if cached is not None:
            return cached, featureKey, True
        return self._defaultEnabled(featureKey), featureKey, False

    async def _refreshFlagCache(self, guildId: int, featureKey: str) -> None:
        key = self._cacheKey(guildId, featureKey)
        try:
            await self.getFlag(guildId, featureKey)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Feature flag background refresh failed for guild=%s feature=%s: %s",
                guildId,
                featureKey,
                exc,
                extra={"skipErrorMirrorDm": True},
            )
        finally:
            self._backgroundRefreshes.discard(key)

    def refreshCommandFlagCacheSoon(self, guildId: int, commandName: str) -> None:
        safeGuildId = _safeInt(guildId)
        featureKey = self._commandFeatureKey(commandName)
        if safeGuildId <= 0 or not featureKey:
            return
        key = self._cacheKey(safeGuildId, featureKey)
        if key in self._backgroundRefreshes:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._backgroundRefreshes.add(key)
        loop.create_task(
            self._refreshFlagCache(safeGuildId, featureKey),
            name=f"feature-flag-refresh:{safeGuildId}:{featureKey}",
        )

    async def exportFlagsJson(self, guildId: int) -> str:
        rows = await self.listFlags(guildId)
        if not rows:
            return "[]"
        try:
            return json.dumps(rows, indent=2, ensure_ascii=True)
        except Exception:
            return "[]"
