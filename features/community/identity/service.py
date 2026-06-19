from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
import discord

import config
from db.sqlite import execute, executeReturnId, fetchAll, fetchOne
from features.staff.sessions.Roblox import robloxGroups, robloxUsers
from runtime import taskBudgeter

log = logging.getLogger(__name__)

_authorizeEndpoint = "https://apis.roblox.com/oauth/v1/authorize"
_tokenEndpoint = "https://apis.roblox.com/oauth/v1/token"
_userinfoEndpoint = "https://apis.roblox.com/oauth/v1/userinfo"
_scope = "openid profile"
_linkAttempts: dict[str, "IdentityLinkAttempt"] = {}
_linkLock = asyncio.Lock()


@dataclass(slots=True)
class IdentityLinkAttempt:
    state: str
    discord_user_id: int
    guild_id: int
    code_verifier: str
    expires_at: datetime
    authorize_url: str


@dataclass(slots=True)
class IdentityLinkResult:
    ok: bool
    discord_user_id: int = 0
    guild_id: int = 0
    roblox_user_id: int = 0
    roblox_username: str = ""
    roblox_display_name: str = ""
    error: str = ""


def _positiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokenTtlSec() -> int:
    try:
        value = int(getattr(config, "janeIdentityLinkTtlSec", 600) or 600)
    except (TypeError, ValueError):
        value = 600
    return max(60, min(value, 3600))


def _publicBaseUrl() -> str:
    return str(getattr(config, "janeIdentityPublicBaseUrl", "") or "").strip().rstrip("/")


def relayBaseUrl() -> str:
    explicit = str(getattr(config, "janeIdentityRelayApiBaseUrl", "") or "").strip().rstrip("/")
    return explicit or _publicBaseUrl()


def relayApiToken() -> str:
    explicit = str(getattr(config, "janeIdentityRelayApiToken", "") or "").strip()
    return explicit or str(getattr(config, "janeIdentityApiToken", "") or "").strip()


def _redirectPath() -> str:
    path = str(getattr(config, "janeIdentityRedirectPath", "/identity/roblox/callback") or "").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def callbackPath() -> str:
    return _redirectPath()


def redirectUri() -> str:
    explicit = str(getattr(config, "janeIdentityRedirectUri", "") or "").strip()
    if explicit:
        return explicit
    baseUrl = _publicBaseUrl() or relayBaseUrl()
    if not baseUrl:
        return ""
    return f"{baseUrl}{_redirectPath()}"


def configurationProblem() -> str:
    if not bool(getattr(config, "janeIdentityEnabled", True)):
        return "Jane Identity is disabled."
    relayEnabled = bool(getattr(config, "janeIdentityRelayEnabled", False))
    webEnabled = bool(getattr(config, "janeIdentityWebEnabled", False))
    if not webEnabled and not relayEnabled:
        return "Jane Identity web callback/relay is disabled."
    if relayEnabled and not relayBaseUrl():
        return "Missing Jane Identity relay public base URL."
    if relayEnabled and not relayApiToken():
        return "Missing Jane Identity relay API token."
    if not str(getattr(config, "robloxOAuthClientId", "") or "").strip():
        return "Missing ROBLOX_OAUTH_CLIENT_ID."
    if not str(getattr(config, "robloxOAuthClientSecret", "") or "").strip():
        return "Missing ROBLOX_OAUTH_CLIENT_SECRET."
    if not redirectUri():
        return "Missing JANE_IDENTITY_PUBLIC_BASE_URL or JANE_IDENTITY_REDIRECT_URI."
    return ""


def isConfigured() -> bool:
    return not configurationProblem()


def _rowDict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _normalizeRankRange(minRank: object, maxRank: object) -> tuple[int, int]:
    low = max(0, min(_positiveInt(minRank, 1), 255))
    high = max(0, min(_positiveInt(maxRank, 255), 255))
    if high < low:
        low, high = high, low
    return low, high


async def listConnectedGroups(guildId: int) -> list[dict[str, Any]]:
    rows = await fetchAll(
        """
        SELECT guildId, groupId, groupName, createdAt, updatedAt
        FROM jane_identity_groups
        WHERE guildId = ?
        ORDER BY groupId ASC
        """,
        (_positiveInt(guildId),),
    )
    return [_rowDict(row) for row in rows]


async def connectGroup(guildId: int, groupId: int, groupName: str = "") -> None:
    safeGuildId = _positiveInt(guildId)
    safeGroupId = _positiveInt(groupId)
    if safeGuildId <= 0 or safeGroupId <= 0:
        raise ValueError("Guild ID and group ID are required.")
    await execute(
        """
        INSERT INTO jane_identity_groups (guildId, groupId, groupName, updatedAt)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(guildId, groupId) DO UPDATE SET
            groupName = excluded.groupName,
            updatedAt = datetime('now')
        """,
        (safeGuildId, safeGroupId, str(groupName or "").strip()[:120]),
    )


async def ensureGroup(guildId: int, groupId: int) -> None:
    safeGuildId = _positiveInt(guildId)
    safeGroupId = _positiveInt(groupId)
    if safeGuildId <= 0 or safeGroupId <= 0:
        raise ValueError("Guild ID and group ID are required.")
    await execute(
        """
        INSERT OR IGNORE INTO jane_identity_groups (guildId, groupId, groupName)
        VALUES (?, ?, '')
        """,
        (safeGuildId, safeGroupId),
    )


async def removeGroup(guildId: int, groupId: int) -> None:
    safeGuildId = _positiveInt(guildId)
    safeGroupId = _positiveInt(groupId)
    await execute(
        "DELETE FROM jane_identity_role_rules WHERE guildId = ? AND groupId = ?",
        (safeGuildId, safeGroupId),
    )
    await execute(
        "DELETE FROM jane_identity_name_rules WHERE guildId = ? AND groupId = ?",
        (safeGuildId, safeGroupId),
    )
    await execute(
        "DELETE FROM jane_identity_groups WHERE guildId = ? AND groupId = ?",
        (safeGuildId, safeGroupId),
    )


async def guildSettings(guildId: int) -> dict[str, Any]:
    safeGuildId = _positiveInt(guildId)
    row = await fetchOne(
        """
        SELECT guildId, unverifiedRoleId, createdAt, updatedAt
        FROM jane_identity_guild_settings
        WHERE guildId = ?
        """,
        (safeGuildId,),
    )
    if not row:
        return {
            "guildId": str(safeGuildId) if safeGuildId else "",
            "unverifiedRoleId": "",
            "createdAt": "",
            "updatedAt": "",
        }
    data = _rowDict(row)
    roleId = _positiveInt(data.get("unverifiedRoleId"))
    return {
        "guildId": str(_positiveInt(data.get("guildId"))) if _positiveInt(data.get("guildId")) else "",
        "unverifiedRoleId": str(roleId) if roleId else "",
        "createdAt": str(data.get("createdAt") or ""),
        "updatedAt": str(data.get("updatedAt") or ""),
    }


async def setUnverifiedRole(guildId: int, roleId: int) -> None:
    safeGuildId = _positiveInt(guildId)
    safeRoleId = _positiveInt(roleId)
    if safeGuildId <= 0 or safeRoleId <= 0:
        raise ValueError("Guild ID and role ID are required.")
    await execute(
        """
        INSERT INTO jane_identity_guild_settings (guildId, unverifiedRoleId, updatedAt)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(guildId) DO UPDATE SET
            unverifiedRoleId = excluded.unverifiedRoleId,
            updatedAt = datetime('now')
        """,
        (safeGuildId, safeRoleId),
    )


async def clearUnverifiedRole(guildId: int) -> None:
    safeGuildId = _positiveInt(guildId)
    if safeGuildId <= 0:
        return
    await execute(
        """
        INSERT INTO jane_identity_guild_settings (guildId, unverifiedRoleId, updatedAt)
        VALUES (?, 0, datetime('now'))
        ON CONFLICT(guildId) DO UPDATE SET
            unverifiedRoleId = 0,
            updatedAt = datetime('now')
        """,
        (safeGuildId,),
    )


async def listRoleRules(guildId: int) -> list[dict[str, Any]]:
    rows = await fetchAll(
        """
        SELECT ruleId, guildId, groupId, minRank, maxRank, roleId, removeWhenUnmatched, createdAt, updatedAt
        FROM jane_identity_role_rules
        WHERE guildId = ?
        ORDER BY groupId ASC, minRank ASC, maxRank ASC, roleId ASC
        """,
        (_positiveInt(guildId),),
    )
    return [_rowDict(row) for row in rows]


async def addRoleRule(
    *,
    guildId: int,
    groupId: int,
    minRank: int,
    maxRank: int,
    roleId: int,
    removeWhenUnmatched: bool = True,
) -> int:
    safeGuildId = _positiveInt(guildId)
    safeGroupId = _positiveInt(groupId)
    safeRoleId = _positiveInt(roleId)
    low, high = _normalizeRankRange(minRank, maxRank)
    if safeGuildId <= 0 or safeGroupId <= 0 or safeRoleId <= 0:
        raise ValueError("Guild ID, group ID, and role ID are required.")
    await ensureGroup(safeGuildId, safeGroupId)
    return int(
        await executeReturnId(
            """
            INSERT INTO jane_identity_role_rules
                (guildId, groupId, minRank, maxRank, roleId, removeWhenUnmatched)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (safeGuildId, safeGroupId, low, high, safeRoleId, 1 if removeWhenUnmatched else 0),
        )
    )


async def removeRoleRule(guildId: int, ruleId: int) -> None:
    await execute(
        "DELETE FROM jane_identity_role_rules WHERE guildId = ? AND ruleId = ?",
        (_positiveInt(guildId), _positiveInt(ruleId)),
    )


async def listNameRules(guildId: int) -> list[dict[str, Any]]:
    rows = await fetchAll(
        """
        SELECT ruleId, guildId, groupId, minRank, maxRank, prefix, suffix, priority, createdAt, updatedAt
        FROM jane_identity_name_rules
        WHERE guildId = ?
        ORDER BY priority DESC, groupId ASC, minRank ASC, maxRank ASC, ruleId ASC
        """,
        (_positiveInt(guildId),),
    )
    return [_rowDict(row) for row in rows]


async def addNameRule(
    *,
    guildId: int,
    groupId: int,
    minRank: int,
    maxRank: int,
    prefix: str = "",
    suffix: str = "",
    priority: int = 0,
) -> int:
    safeGuildId = _positiveInt(guildId)
    safeGroupId = _positiveInt(groupId)
    low, high = _normalizeRankRange(minRank, maxRank)
    if safeGuildId <= 0 or safeGroupId <= 0:
        raise ValueError("Guild ID and group ID are required.")
    await ensureGroup(safeGuildId, safeGroupId)
    try:
        safePriority = int(priority or 0)
    except (TypeError, ValueError):
        safePriority = 0
    return int(
        await executeReturnId(
            """
            INSERT INTO jane_identity_name_rules
                (guildId, groupId, minRank, maxRank, prefix, suffix, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safeGuildId,
                safeGroupId,
                low,
                high,
                str(prefix or "").strip()[:24],
                str(suffix or "").strip()[:24],
                safePriority,
            ),
        )
    )


async def removeNameRule(guildId: int, ruleId: int) -> None:
    await execute(
        "DELETE FROM jane_identity_name_rules WHERE guildId = ? AND ruleId = ?",
        (_positiveInt(guildId), _positiveInt(ruleId)),
    )


async def guildConfiguration(guildId: int) -> dict[str, Any]:
    return {
        "settings": await guildSettings(guildId),
        "groups": await listConnectedGroups(guildId),
        "roleRules": await listRoleRules(guildId),
        "nameRules": await listNameRules(guildId),
    }


def _base64Url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _newCodeVerifier() -> str:
    return _base64Url(secrets.token_bytes(48))


def _codeChallenge(codeVerifier: str) -> str:
    digest = hashlib.sha256(codeVerifier.encode("ascii")).digest()
    return _base64Url(digest)


def _dbTimestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parseDbTimestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _pruneExpiredAttemptsLocked() -> None:
    now = _now()
    expiredStates = [
        state for state, attempt in _linkAttempts.items()
        if attempt.expires_at <= now
    ]
    for state in expiredStates:
        _linkAttempts.pop(state, None)


async def _pruneExpiredStoredAttempts() -> None:
    try:
        await execute(
            """
            DELETE FROM jane_identity_link_attempts
            WHERE expiresAt <= ?
            """,
            (_dbTimestamp(_now()),),
        )
    except Exception:
        log.debug("Could not prune expired Jane Identity link attempts.", exc_info=True)


async def _storeLinkAttempt(attempt: IdentityLinkAttempt) -> None:
    await execute(
        """
        INSERT INTO jane_identity_link_attempts
            (state, discordUserId, guildId, codeVerifier, authorizeUrl, expiresAt)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(state) DO UPDATE SET
            discordUserId = excluded.discordUserId,
            guildId = excluded.guildId,
            codeVerifier = excluded.codeVerifier,
            authorizeUrl = excluded.authorizeUrl,
            expiresAt = excluded.expiresAt,
            createdAt = datetime('now')
        """,
        (
            attempt.state,
            int(attempt.discord_user_id),
            int(attempt.guild_id),
            attempt.code_verifier,
            attempt.authorize_url,
            _dbTimestamp(attempt.expires_at),
        ),
    )


async def _deleteStoredAttempt(state: str) -> None:
    try:
        await execute("DELETE FROM jane_identity_link_attempts WHERE state = ?", (str(state or "").strip(),))
    except Exception:
        log.debug("Could not delete consumed Jane Identity link attempt.", exc_info=True)


async def _storedAttempt(state: str) -> IdentityLinkAttempt | None:
    cleanState = str(state or "").strip()
    if not cleanState:
        return None
    try:
        row = await fetchOne(
            """
            SELECT state, discordUserId, guildId, codeVerifier, authorizeUrl, expiresAt
            FROM jane_identity_link_attempts
            WHERE state = ?
            """,
            (cleanState,),
        )
    except Exception:
        log.debug("Could not read stored Jane Identity link attempt.", exc_info=True)
        return None
    if not row:
        return None
    expiresAt = _parseDbTimestamp(row.get("expiresAt"))
    if expiresAt is None or expiresAt <= _now():
        await _deleteStoredAttempt(cleanState)
        return None
    codeVerifier = str(row.get("codeVerifier") or "").strip()
    if not codeVerifier:
        await _deleteStoredAttempt(cleanState)
        return None
    return IdentityLinkAttempt(
        state=str(row.get("state") or cleanState),
        discord_user_id=_positiveInt(row.get("discordUserId")),
        guild_id=_positiveInt(row.get("guildId")),
        code_verifier=codeVerifier,
        expires_at=expiresAt,
        authorize_url=str(row.get("authorizeUrl") or ""),
    )


async def createLinkAttempt(*, discordUserId: int, guildId: int = 0) -> IdentityLinkAttempt:
    problem = configurationProblem()
    if problem:
        raise RuntimeError(problem)
    state = secrets.token_urlsafe(32)
    codeVerifier = _newCodeVerifier()
    expiresAt = _now() + timedelta(seconds=_tokenTtlSec())
    query = urlencode(
        {
            "client_id": str(getattr(config, "robloxOAuthClientId", "") or "").strip(),
            "redirect_uri": redirectUri(),
            "scope": _scope,
            "response_type": "code",
            "state": state,
            "code_challenge": _codeChallenge(codeVerifier),
            "code_challenge_method": "S256",
        }
    )
    attempt = IdentityLinkAttempt(
        state=state,
        discord_user_id=int(discordUserId),
        guild_id=_positiveInt(guildId),
        code_verifier=codeVerifier,
        expires_at=expiresAt,
        authorize_url=f"{_authorizeEndpoint}?{query}",
    )
    async with _linkLock:
        await _pruneExpiredAttemptsLocked()
        _linkAttempts[state] = attempt
    try:
        await _pruneExpiredStoredAttempts()
        await _storeLinkAttempt(attempt)
    except Exception:
        log.exception("Could not persist Jane Identity OAuth attempt for Discord user %s.", discordUserId)
    return attempt


async def _consumeAttempt(state: str) -> Optional[IdentityLinkAttempt]:
    cleanState = str(state or "").strip()
    if not cleanState:
        return None
    attempt: IdentityLinkAttempt | None = None
    async with _linkLock:
        await _pruneExpiredAttemptsLocked()
        attempt = _linkAttempts.pop(cleanState, None)
    if attempt is not None:
        await _deleteStoredAttempt(cleanState)
        return attempt
    attempt = await _storedAttempt(cleanState)
    if attempt is not None:
        await _deleteStoredAttempt(cleanState)
    return attempt


async def failRobloxOAuthAttempt(*, state: str, error: str) -> IdentityLinkResult:
    attempt = await _consumeAttempt(state)
    if attempt is None:
        return IdentityLinkResult(
            ok=False,
            error=str(error or "").strip() or "This verification link is expired or invalid.",
        )
    return IdentityLinkResult(
        ok=False,
        discord_user_id=attempt.discord_user_id,
        guild_id=attempt.guild_id,
        error=str(error or "").strip() or "Roblox authorization was cancelled.",
    )


async def _readJsonResponse(response: aiohttp.ClientResponse) -> object:
    try:
        return await response.json(content_type=None)
    except Exception:
        text = await response.text()
        return {"message": text[:300]}


def _oauthError(status: int, payload: object, default: str) -> str:
    if isinstance(payload, dict):
        for key in ("error_description", "message", "error"):
            value = str(payload.get(key) or "").strip()
            if value:
                return f"{default} ({status}): {value}"
    return f"{default} ({status})."


async def _exchangeCodeForUserInfo(
    *,
    code: str,
    codeVerifier: str,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15)
    data = {
        "grant_type": "authorization_code",
        "code": str(code or "").strip(),
        "redirect_uri": redirectUri(),
        "client_id": str(getattr(config, "robloxOAuthClientId", "") or "").strip(),
        "client_secret": str(getattr(config, "robloxOAuthClientSecret", "") or "").strip(),
        "code_verifier": codeVerifier,
    }
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _tokenEndpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            tokenPayload = await _readJsonResponse(response)
            if response.status != 200 or not isinstance(tokenPayload, dict):
                raise RuntimeError(_oauthError(response.status, tokenPayload, "Roblox token exchange failed"))
        accessToken = str(tokenPayload.get("access_token") or "").strip()
        if not accessToken:
            raise RuntimeError("Roblox token exchange did not return an access token.")

        async with session.get(
            _userinfoEndpoint,
            headers={"Authorization": f"Bearer {accessToken}"},
        ) as response:
            userPayload = await _readJsonResponse(response)
            if response.status != 200 or not isinstance(userPayload, dict):
                raise RuntimeError(_oauthError(response.status, userPayload, "Roblox userinfo lookup failed"))
            return userPayload


async def completeRobloxOAuth(*, code: str, state: str) -> IdentityLinkResult:
    attempt = await _consumeAttempt(state)
    if attempt is None:
        return IdentityLinkResult(ok=False, error="This verification link is expired or invalid.")
    cleanCode = str(code or "").strip()
    if not cleanCode:
        return IdentityLinkResult(
            ok=False,
            discord_user_id=attempt.discord_user_id,
            guild_id=attempt.guild_id,
            error="Roblox did not return an authorization code.",
        )

    try:
        userInfo = await _exchangeCodeForUserInfo(
            code=cleanCode,
            codeVerifier=attempt.code_verifier,
        )
    except Exception as exc:
        log.exception("Jane Identity OAuth exchange failed for Discord user %s.", attempt.discord_user_id)
        return IdentityLinkResult(
            ok=False,
            discord_user_id=attempt.discord_user_id,
            guild_id=attempt.guild_id,
            error=str(exc) or "Roblox verification failed.",
        )

    robloxUserId = _positiveInt(userInfo.get("sub"))
    robloxUsername = str(userInfo.get("preferred_username") or "").strip()
    robloxDisplayName = str(userInfo.get("name") or userInfo.get("nickname") or "").strip()
    if robloxUserId <= 0 or not robloxUsername:
        return IdentityLinkResult(
            ok=False,
            discord_user_id=attempt.discord_user_id,
            guild_id=attempt.guild_id,
            error="Roblox did not return a complete user profile.",
        )

    stored = await robloxUsers.rememberKnownRobloxIdentity(
        attempt.discord_user_id,
        robloxUsername,
        robloxId=robloxUserId,
        source="jane-identity:oauth",
        guildId=attempt.guild_id,
        confidence=100,
    )
    if not stored:
        return IdentityLinkResult(
            ok=False,
            discord_user_id=attempt.discord_user_id,
            guild_id=attempt.guild_id,
            roblox_user_id=robloxUserId,
            roblox_username=robloxUsername,
            roblox_display_name=robloxDisplayName,
            error="Jane could not save the linked Roblox account.",
        )

    return IdentityLinkResult(
        ok=True,
        discord_user_id=attempt.discord_user_id,
        guild_id=attempt.guild_id,
        roblox_user_id=robloxUserId,
        roblox_username=robloxUsername,
        roblox_display_name=robloxDisplayName,
    )


def _configuredRoleIds(name: str) -> list[int]:
    raw = getattr(config, name, []) or []
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        roleId = _positiveInt(value)
        if roleId <= 0 or roleId in seen:
            continue
        seen.add(roleId)
        out.append(roleId)
    return out


def _groupRoleRules() -> list[dict[str, int | bool]]:
    rawRules = getattr(config, "janeIdentityGroupRoleRules", []) or []
    if not isinstance(rawRules, (list, tuple)):
        return []
    rules: list[dict[str, int | bool]] = []
    for rawRule in rawRules:
        if not isinstance(rawRule, dict):
            continue
        groupId = _positiveInt(rawRule.get("groupId"))
        roleId = _positiveInt(rawRule.get("roleId"))
        if groupId <= 0 or roleId <= 0:
            continue
        rules.append(
            {
                "groupId": groupId,
                "roleId": roleId,
                "minRank": max(0, _positiveInt(rawRule.get("minRank"), 1)),
                "maxRank": min(255, _positiveInt(rawRule.get("maxRank"), 255)),
                "removeWhenUnmatched": bool(rawRule.get("removeWhenUnmatched", False)),
            }
        )
    return rules


async def unverifiedRoleIds(guildId: int) -> list[int]:
    ids = _configuredRoleIds("janeIdentityUnverifiedRoleIds")
    row = await guildSettings(guildId)
    roleId = _positiveInt(row.get("unverifiedRoleId"))
    if roleId > 0 and roleId not in ids:
        ids.append(roleId)
    return ids


async def managedRoleIds(guildId: int) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for roleId in _configuredRoleIds("janeIdentityVerifiedRoleIds"):
        if roleId > 0 and roleId not in seen:
            seen.add(roleId)
            ids.append(roleId)
    for rule in _groupRoleRules():
        roleId = _positiveInt(rule.get("roleId"))
        if roleId > 0 and roleId not in seen:
            seen.add(roleId)
            ids.append(roleId)
    for rule in await listRoleRules(guildId):
        roleId = _positiveInt(rule.get("roleId"))
        if roleId > 0 and roleId not in seen:
            seen.add(roleId)
            ids.append(roleId)
    return ids


def resultFromStoredIdentity(
    *,
    discordUserId: int,
    guildId: int,
    lookup: Any,
) -> IdentityLinkResult:
    return IdentityLinkResult(
        ok=lookup is not None and bool(str(getattr(lookup, "robloxUsername", "") or "").strip()),
        discord_user_id=_positiveInt(discordUserId),
        guild_id=_positiveInt(guildId),
        roblox_user_id=_positiveInt(getattr(lookup, "robloxId", 0)),
        roblox_username=str(getattr(lookup, "robloxUsername", "") or "").strip(),
    )


def _identityPayload(row: Any) -> dict[str, Any]:
    data = _rowDict(row)
    discordUserId = _positiveInt(data.get("discordUserId"))
    robloxUserId = _positiveInt(data.get("robloxUserId"))
    payload: dict[str, Any] = {
        "discordId": str(discordUserId) if discordUserId else "",
        "robloxId": str(robloxUserId) if robloxUserId else "",
        "username": str(data.get("robloxUsername") or "").strip(),
        "robloxUsername": str(data.get("robloxUsername") or "").strip(),
        "source": str(data.get("source") or "").strip(),
        "guildId": str(_positiveInt(data.get("guildId"))) if _positiveInt(data.get("guildId")) else "",
        "confidence": _positiveInt(data.get("confidence")),
        "createdAt": str(data.get("createdAt") or ""),
        "updatedAt": str(data.get("updatedAt") or ""),
        "lastUsedAt": str(data.get("lastUsedAt") or ""),
    }
    return payload


def _verifiedIdentityWhereClause() -> str:
    return "(source = 'rover' OR source LIKE 'jane-identity:oauth%')"


async def apiIdentityByDiscordId(discordUserId: int, guildId: int = 0) -> dict[str, Any] | None:
    safeDiscordUserId = _positiveInt(discordUserId)
    if safeDiscordUserId <= 0:
        return None

    lookup = await robloxUsers.getVerifiedRobloxIdentity(safeDiscordUserId)
    if lookup is None:
        return None

    row = await fetchOne(
        f"""
        SELECT discordUserId, robloxUserId, robloxUsername, source, guildId,
               confidence, createdAt, updatedAt, lastUsedAt
        FROM roblox_identity_links
        WHERE discordUserId = ?
          AND {_verifiedIdentityWhereClause()}
        """,
        (safeDiscordUserId,),
    )
    if row:
        return _identityPayload(row)

    robloxId = _positiveInt(getattr(lookup, "robloxId", 0))
    username = str(getattr(lookup, "robloxUsername", "") or "").strip()
    if not username:
        return None
    return {
        "discordId": str(safeDiscordUserId),
        "robloxId": str(robloxId) if robloxId else "",
        "username": username,
        "robloxUsername": username,
        "source": "internal",
        "guildId": str(_positiveInt(guildId)) if _positiveInt(guildId) else "",
        "confidence": 0,
        "createdAt": "",
        "updatedAt": "",
        "lastUsedAt": "",
    }


async def apiDiscordIdentitiesByRobloxId(robloxUserId: int, guildId: int = 0) -> list[dict[str, Any]]:
    safeRobloxUserId = _positiveInt(robloxUserId)
    safeGuildId = _positiveInt(guildId)
    if safeRobloxUserId <= 0:
        return []

    rows = await fetchAll(
        f"""
        SELECT discordUserId, robloxUserId, robloxUsername, source, guildId,
               confidence, createdAt, updatedAt, lastUsedAt
        FROM roblox_identity_links
        WHERE robloxUserId = ?
          AND {_verifiedIdentityWhereClause()}
        ORDER BY
            CASE WHEN guildId = ? THEN 0 ELSE 1 END,
            datetime(COALESCE(lastUsedAt, updatedAt, createdAt)) DESC,
            discordUserId ASC
        """,
        (safeRobloxUserId, safeGuildId),
    )
    identities = [_identityPayload(row) for row in rows]
    if identities:
        await execute(
            """
            UPDATE roblox_identity_links
            SET lastUsedAt = datetime('now')
            WHERE robloxUserId = ?
              AND (source = 'rover' OR source LIKE 'jane-identity:oauth%')
            """,
            (safeRobloxUserId,),
        )
    return identities


async def apiDiscordIdentitiesByRobloxUsername(robloxUsername: str, guildId: int = 0) -> list[dict[str, Any]]:
    cleanUsername = "".join(ch for ch in str(robloxUsername or "").strip() if not ch.isspace())
    safeGuildId = _positiveInt(guildId)
    if not cleanUsername:
        return []

    rows = await fetchAll(
        f"""
        SELECT discordUserId, robloxUserId, robloxUsername, source, guildId,
               confidence, createdAt, updatedAt, lastUsedAt
        FROM roblox_identity_links
        WHERE lower(robloxUsername) = lower(?)
          AND {_verifiedIdentityWhereClause()}
        ORDER BY
            CASE WHEN guildId = ? THEN 0 ELSE 1 END,
            datetime(COALESCE(lastUsedAt, updatedAt, createdAt)) DESC,
            discordUserId ASC
        """,
        (cleanUsername, safeGuildId),
    )
    identities = [_identityPayload(row) for row in rows]
    if identities:
        await execute(
            """
            UPDATE roblox_identity_links
            SET lastUsedAt = datetime('now')
            WHERE lower(robloxUsername) = lower(?)
              AND (source = 'rover' OR source LIKE 'jane-identity:oauth%')
            """,
            (cleanUsername,),
        )
    return identities


async def listStoredIdentityLinks(*, verifiedOnly: bool = False) -> list[dict[str, Any]]:
    whereSql = f"WHERE {_verifiedIdentityWhereClause()}" if verifiedOnly else ""
    rows = await fetchAll(
        f"""
        SELECT discordUserId, robloxUserId, robloxUsername, source, guildId,
               confidence, createdAt, updatedAt, lastUsedAt
        FROM roblox_identity_links
        {whereSql}
        ORDER BY discordUserId ASC
        """
    )
    return [_identityPayload(row) for row in rows]


def _ruleMatches(rule: dict[str, Any], ranksByGroupId: dict[int, int]) -> bool:
    groupId = _positiveInt(rule.get("groupId"))
    if groupId <= 0:
        return False
    rank = int(ranksByGroupId.get(groupId, 0))
    return int(rule.get("minRank") or 0) <= rank <= int(rule.get("maxRank") or 255)


def _formatNickname(username: str, nameRule: dict[str, Any] | None) -> str:
    cleanUsername = str(username or "").strip()
    if not cleanUsername:
        return ""
    if not nameRule:
        return cleanUsername[:32]
    prefix = str(nameRule.get("prefix") or "").strip()
    suffix = str(nameRule.get("suffix") or "").strip()
    parts = [part for part in (prefix, cleanUsername, suffix) if part]
    return " ".join(parts)[:32]


async def _resolveMember(
    bot: discord.Client,
    guildId: int,
    discordUserId: int,
) -> tuple[discord.Guild | None, discord.Member | None, str]:
    if guildId <= 0 or discordUserId <= 0:
        return None, None, "missing-target"
    guild = bot.get_guild(guildId)
    if guild is None:
        return None, None, "guild-not-cached"
    member = guild.get_member(discordUserId)
    if member is None:
        try:
            member = await taskBudgeter.runDiscord(lambda: guild.fetch_member(discordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return guild, None, "member-not-found"
    if member is None:
        return guild, None, "member-not-found"
    return guild, member, ""


async def applyUnverifiedMemberState(
    bot: discord.Client,
    guildId: int,
    discordUserId: int,
    *,
    unverifiedRoleIdList: list[int] | None = None,
    managedRoleIdList: list[int] | None = None,
    member: discord.Member | None = None,
) -> dict[str, Any]:
    safeGuildId = _positiveInt(guildId)
    safeDiscordUserId = _positiveInt(discordUserId)
    if member is not None:
        guild = member.guild
    else:
        guild, member, reason = await _resolveMember(bot, safeGuildId, safeDiscordUserId)
        if guild is None or member is None:
            return {"updated": False, "reason": reason}

    unverifiedIds = list(unverifiedRoleIdList) if unverifiedRoleIdList is not None else await unverifiedRoleIds(safeGuildId)
    managedIds = list(managedRoleIdList) if managedRoleIdList is not None else await managedRoleIds(safeGuildId)
    currentRoleIds = {int(role.id) for role in member.roles}
    unverifiedSet = {roleId for roleId in unverifiedIds if roleId > 0}
    managedSet = {roleId for roleId in managedIds if roleId > 0}
    rolesToAdd = [
        role for roleId in sorted(unverifiedSet)
        if roleId not in currentRoleIds
        if (role := guild.get_role(roleId)) is not None
    ]
    rolesToRemove = [
        role for roleId in sorted(managedSet - unverifiedSet)
        if roleId in currentRoleIds
        if (role := guild.get_role(roleId)) is not None
    ]
    changes: dict[str, Any] = {"updated": False, "rolesAdded": [], "rolesRemoved": []}
    if rolesToAdd:
        try:
            await taskBudgeter.runDiscord(
                lambda: member.add_roles(*rolesToAdd, reason="Jane Identity unverified member state")
            )
            changes["updated"] = True
            changes["rolesAdded"] = [int(role.id) for role in rolesToAdd]
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not add Jane Identity unverified roles for user %s.", safeDiscordUserId)
    if rolesToRemove:
        try:
            await taskBudgeter.runDiscord(
                lambda: member.remove_roles(*rolesToRemove, reason="Jane Identity unverified member state")
            )
            changes["updated"] = True
            changes["rolesRemoved"] = [int(role.id) for role in rolesToRemove]
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not remove Jane Identity managed roles for unverified user %s.", safeDiscordUserId)
    return changes


async def applyMemberVerification(
    bot: discord.Client,
    result: IdentityLinkResult,
    *,
    member: discord.Member | None = None,
) -> dict[str, Any]:
    if not result.ok or result.guild_id <= 0 or result.discord_user_id <= 0:
        return {"updated": False}
    if member is not None:
        guild = member.guild
    else:
        guild, member, reason = await _resolveMember(bot, result.guild_id, result.discord_user_id)
        if guild is None or member is None:
            return {"updated": False, "reason": reason}

    changes: dict[str, Any] = {
        "updated": False,
        "rolesAdded": [],
        "rolesRemoved": [],
        "unverifiedRolesRemoved": [],
        "nickname": False,
        "nicknameFailed": False,
        "rolesAddFailed": [],
        "rolesRemoveFailed": [],
        "permissionIssues": [],
    }

    roleRules = _groupRoleRules()
    roleRules.extend(await listRoleRules(result.guild_id))
    nameRules = await listNameRules(result.guild_id)
    ranksByGroupId: dict[int, int] = {}
    if result.roblox_user_id > 0 and (roleRules or nameRules):
        groupResult = await robloxGroups.fetchRobloxGroups(result.roblox_user_id)
        if groupResult.status == 200:
            for entry in groupResult.groups:
                groupId = _positiveInt(entry.get("id"))
                rank = _positiveInt(entry.get("rank"))
                if groupId > 0:
                    ranksByGroupId[groupId] = rank

    if bool(getattr(config, "janeIdentityUpdateNickname", True)):
        matchedNameRules = [
            rule for rule in nameRules
            if _ruleMatches(rule, ranksByGroupId)
        ]
        matchedNameRules.sort(
            key=lambda rule: (
                int(rule.get("priority") or 0),
                int(rule.get("minRank") or 0),
                -int(rule.get("ruleId") or 0),
            ),
            reverse=True,
        )
        targetNick = _formatNickname(
            result.roblox_username,
            matchedNameRules[0] if matchedNameRules else None,
        )
        if targetNick and member.nick != targetNick:
            try:
                await taskBudgeter.runDiscord(
                    lambda: member.edit(nick=targetNick, reason="Jane Identity Roblox verification")
                )
                changes["updated"] = True
                changes["nickname"] = True
            except (discord.Forbidden, discord.HTTPException):
                changes["nicknameFailed"] = True
                changes["permissionIssues"].append(
                    "Nickname: Jane could not update this member's nickname. "
                    "Check Manage Nicknames and Jane's role hierarchy."
                )
                log.info(
                    "Could not update nickname for verified user %s in guild %s.",
                    result.discord_user_id,
                    result.guild_id,
                )

    addRoleIds = set(_configuredRoleIds("janeIdentityVerifiedRoleIds"))
    removeRoleIds: set[int] = set()
    for rule in roleRules:
        roleId = _positiveInt(rule.get("roleId"))
        if roleId <= 0:
            continue
        if _ruleMatches(rule, ranksByGroupId):
            addRoleIds.add(roleId)
        elif bool(rule.get("removeWhenUnmatched")):
            removeRoleIds.add(roleId)

    unverifiedIds = set(await unverifiedRoleIds(result.guild_id))
    removeRoleIds.update(unverifiedIds)
    currentRoleIds = {int(role.id) for role in member.roles}
    rolesToAdd = [
        role for roleId in sorted(addRoleIds)
        if roleId not in currentRoleIds
        if (role := guild.get_role(roleId)) is not None
    ]
    rolesToRemove = [
        role for roleId in sorted(removeRoleIds - addRoleIds)
        if roleId in currentRoleIds
        if (role := guild.get_role(roleId)) is not None
    ]
    if rolesToAdd:
        try:
            await taskBudgeter.runDiscord(
                lambda: member.add_roles(*rolesToAdd, reason="Jane Identity Roblox verification")
            )
            changes["updated"] = True
            changes["rolesAdded"] = [int(role.id) for role in rolesToAdd]
        except (discord.Forbidden, discord.HTTPException):
            failedIds = [int(role.id) for role in rolesToAdd]
            changes["rolesAddFailed"] = failedIds
            changes["permissionIssues"].append(
                "Roles added: Jane could not add one or more roles. "
                "Check Manage Roles and Jane's role hierarchy."
            )
            log.info("Could not add Jane Identity roles for user %s.", result.discord_user_id)
    if rolesToRemove:
        try:
            await taskBudgeter.runDiscord(
                lambda: member.remove_roles(*rolesToRemove, reason="Jane Identity Roblox verification")
            )
            changes["updated"] = True
            changes["rolesRemoved"] = [int(role.id) for role in rolesToRemove]
            changes["unverifiedRolesRemoved"] = [
                int(role.id) for role in rolesToRemove
                if int(role.id) in unverifiedIds
            ]
        except (discord.Forbidden, discord.HTTPException):
            failedIds = [int(role.id) for role in rolesToRemove]
            changes["rolesRemoveFailed"] = failedIds
            changes["permissionIssues"].append(
                "Roles removed: Jane could not remove one or more roles. "
                "Check Manage Roles and Jane's role hierarchy."
            )
            log.info("Could not remove Jane Identity roles for user %s.", result.discord_user_id)
    return changes


def _roleSummary(guild: discord.Guild | None, roleIds: object) -> str:
    labels: list[str] = []
    if not isinstance(roleIds, (list, tuple, set)):
        return ""
    for rawRoleId in roleIds:
        roleId = _positiveInt(rawRoleId)
        if roleId <= 0:
            continue
        role = guild.get_role(roleId) if guild is not None else None
        labels.append(role.mention if role is not None else f"`{roleId}`")
    return ", ".join(labels)


def formatApplySummary(
    changes: dict[str, Any],
    *,
    guild: discord.Guild | None = None,
    emptyText: str = "No nickname or role changes were needed.",
) -> str:
    lines: list[str] = []
    if bool(changes.get("nickname")):
        lines.append("Nickname: updated")
    addedText = _roleSummary(guild, changes.get("rolesAdded"))
    if addedText:
        lines.append(f"Roles added: {addedText}")
    removedText = _roleSummary(guild, changes.get("rolesRemoved"))
    if removedText:
        lines.append(f"Roles removed: {removedText}")
    issues: list[str] = []
    if bool(changes.get("nicknameFailed")):
        issues.append("Nickname failed. Check Manage Nicknames and Jane's role hierarchy.")
    addFailedText = _roleSummary(guild, changes.get("rolesAddFailed"))
    if addFailedText:
        issues.append(
            f"Role add failed for {addFailedText}. Check Manage Roles and Jane's role hierarchy."
        )
    removeFailedText = _roleSummary(guild, changes.get("rolesRemoveFailed"))
    if removeFailedText:
        issues.append(
            f"Role remove failed for {removeFailedText}. Check Manage Roles and Jane's role hierarchy."
        )
    if not issues:
        issues = [
            str(issue or "").strip()
            for issue in list(changes.get("permissionIssues") or [])
            if str(issue or "").strip()
        ]
    if issues:
        lines.append("Permission issues: " + " ".join(issues))
    if not lines:
        lines.append(str(emptyText or "No nickname or role changes were needed."))
    return "\n".join(lines)


def htmlPage(title: str, body: str) -> str:
    safeTitle = html.escape(title)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{safeTitle}</title>"
        "<style>"
        ":root{color-scheme:light;--text:#172033;--muted:#5f6b7a;--line:#dce2ea;"
        "--panel:#ffffff;--page:#f5f7fb;--accent:#285bb8;--soft:#f8fafc}"
        "*{box-sizing:border-box}"
        "body{margin:0;min-height:100vh;background:var(--page);color:var(--text);"
        "font-family:system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;"
        "line-height:1.5;display:flex;align-items:center;justify-content:center;padding:32px 16px}"
        ".shell{width:min(760px,100%)}"
        ".brand{display:flex;align-items:center;gap:12px;margin:0 0 16px;color:var(--muted)}"
        ".brand-mark{width:40px;height:40px;border-radius:8px;background:#172033;color:#fff;"
        "display:grid;place-items:center;font-weight:800;font-size:.95rem}"
        ".brand-name{font-weight:800;color:var(--text);font-size:1rem}"
        ".brand-subtitle{font-size:.92rem;color:var(--muted)}"
        "main{background:var(--panel);border:1px solid var(--line);border-radius:8px;"
        "box-shadow:0 18px 45px rgba(15,23,42,.1);padding:28px}"
        "h1{font-size:1.55rem;line-height:1.2;margin:0 0 14px;color:var(--text)}"
        "h2{font-size:1.05rem;margin:24px 0 8px;color:var(--text)}"
        "p{margin:0 0 12px}ul{margin:8px 0 14px;padding-left:22px}li{margin:6px 0}"
        "a{color:var(--accent);font-weight:650;text-decoration:none}a:hover{text-decoration:underline}"
        "code{font-family:ui-monospace,SFMono-Regular,Consolas,\"Liberation Mono\",monospace;"
        "font-size:.92em;background:#eef2f7;border:1px solid #dbe3ee;border-radius:6px;padding:1px 5px}"
        ".summary{color:#334155;font-size:1rem}"
        ".result-list{display:grid;gap:8px;list-style:none;margin:18px 0;padding:0}"
        ".result-list li{margin:0;padding:10px 12px;background:#fbfdff;border:1px solid #e4e9f0;border-radius:8px}"
        ".notice{margin:18px 0 0;padding:13px 14px;background:var(--soft);border:1px solid #e1e7ef;"
        "border-radius:8px;color:#465465}"
        ".notice p{margin:4px 0 0}.muted{color:var(--muted)}"
        ".page-links{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 2px 0;"
        "font-size:.92rem;color:var(--muted)}"
        "@media (max-width:560px){body{align-items:flex-start;padding:20px 12px}"
        "main{padding:22px 18px}h1{font-size:1.35rem}.brand-mark{width:36px;height:36px}}"
        "</style>"
        "</head><body><div class=\"shell\">"
        "<header class=\"brand\">"
        "<div class=\"brand-mark\" aria-hidden=\"true\">JC</div>"
        "<div><div class=\"brand-name\">Jane Identity</div>"
        "<div class=\"brand-subtitle\">Discord to Roblox verification</div></div>"
        "</header>"
        f"<main><h1>{safeTitle}</h1>{body}"
        "<div class=\"notice\"><strong>Account safety</strong>"
        "<p>Jane uses Roblox OAuth for authorization. Jane never receives or stores your Roblox password.</p>"
        "</div></main>"
        "<nav class=\"page-links\" aria-label=\"Jane Identity links\">"
        "<a href=\"/\">Home</a><span>&middot;</span>"
        "<a href=\"/privacy\">Privacy Policy</a><span>&middot;</span>"
        "<a href=\"/terms\">Terms of Service</a>"
        "</nav></div></body></html>"
    )
