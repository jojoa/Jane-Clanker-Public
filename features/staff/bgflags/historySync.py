from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord

from features.staff.bgflags import service as flagService

_NUMERIC_RULE_TYPES = {"group", "item", "creator", "badge", "game"}
_RULE_TYPE_ALIASES = {
    "group": "group",
    "roblox group": "group",
    "keyword": "keyword",
    "global keyword": "keyword",
    "item": "item",
    "accessory": "item",
    "asset": "item",
    "roblox item": "item",
    "catalog item": "item",
    "creator": "creator",
    "roblox creator": "creator",
    "badge": "badge",
    "roblox badge": "badge",
    "game": "game",
    "favorite game": "game",
    "universe": "game",
    "favorite game keyword": "game_keyword",
    "game keyword": "game_keyword",
    "group keyword": "group_keyword",
    "item keyword": "item_keyword",
}
_SEVERITY_LABELS = {
    "light": 25,
    "medium": 50,
    "high": 75,
    "severe": 100,
}
_RULE_VALUE_FIELD_KEYS = (
    "value",
    "rule value",
    "flag value",
    "keyword",
    "item",
    "asset id",
    "group id",
    "badge id",
    "creator id",
    "game id",
    "universe id",
)
_CATALOG_ID_RE = re.compile(
    r"roblox\.com/(?:catalog|library)/(\d+)|roblox\.com/marketplace/asset/(\d+)",
    re.IGNORECASE,
)
_GROUP_ID_RE = re.compile(r"roblox\.com/groups/(\d+)", re.IGNORECASE)
_BADGE_ID_RE = re.compile(r"roblox\.com/(?:badges|communities/.+?/badges)/(\d+)", re.IGNORECASE)
_GAME_ID_RE = re.compile(r"roblox\.com/games/(\d+)", re.IGNORECASE)
_USER_ID_RE = re.compile(r"roblox\.com/users/(\d+)", re.IGNORECASE)
_DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
_INTEGER_RE = re.compile(r"\b(\d{2,})\b")


@dataclass(frozen=True)
class HistoricalFlagCandidate:
    ruleType: str
    ruleValue: str
    note: str | None
    severity: int
    proposedBy: int
    sourceMessageId: int
    sourceJumpUrl: str


def _fieldKey(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _cleanText(value: object) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1].strip()
    text = text.replace("\u200b", "").strip()
    return text


def _embedFields(embed: discord.Embed) -> dict[str, str]:
    fields: dict[str, str] = {}
    for field in list(getattr(embed, "fields", []) or []):
        key = _fieldKey(getattr(field, "name", ""))
        if not key or key in fields:
            continue
        fields[key] = str(getattr(field, "value", "") or "").strip()
    return fields


def _embedText(embed: discord.Embed) -> str:
    parts = [
        str(getattr(embed, "title", "") or ""),
        str(getattr(embed, "description", "") or ""),
    ]
    for field in list(getattr(embed, "fields", []) or []):
        parts.append(str(getattr(field, "name", "") or ""))
        parts.append(str(getattr(field, "value", "") or ""))
    footer = getattr(embed, "footer", None)
    footerText = str(getattr(footer, "text", "") or "") if footer is not None else ""
    if footerText:
        parts.append(footerText)
    return "\n".join(part for part in parts if part)


def _normalizeRuleType(value: object) -> str:
    key = _fieldKey(_cleanText(value))
    if key in _RULE_TYPE_ALIASES:
        return _RULE_TYPE_ALIASES[key]
    key = key.replace(" ", "_")
    if key in {"group", "keyword", "item", "creator", "badge", "game", "game_keyword", "group_keyword", "item_keyword"}:
        return key
    return ""


def _parseSeverity(value: object) -> int:
    text = _cleanText(value)
    lowered = text.lower()
    if lowered in _SEVERITY_LABELS:
        return _SEVERITY_LABELS[lowered]
    for label, severity in _SEVERITY_LABELS.items():
        if label in lowered:
            return severity
    match = _INTEGER_RE.search(text)
    if not match:
        return 50
    try:
        return flagService.normalizeSeverity(int(match.group(1)))
    except (TypeError, ValueError):
        return 50


def _extractFirstRegexInt(pattern: re.Pattern[str], text: str) -> int:
    match = pattern.search(text)
    if not match:
        return 0
    for group in match.groups():
        if not group:
            continue
        try:
            parsed = int(group)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _extractNumericRuleValue(ruleType: str, rawValue: str, fullText: str) -> str:
    source = "\n".join(value for value in [rawValue, fullText] if value)
    if ruleType == "item":
        parsed = _extractFirstRegexInt(_CATALOG_ID_RE, source)
        if parsed > 0:
            return str(parsed)
    elif ruleType == "group":
        parsed = _extractFirstRegexInt(_GROUP_ID_RE, source)
        if parsed > 0:
            return str(parsed)
    elif ruleType == "badge":
        parsed = _extractFirstRegexInt(_BADGE_ID_RE, source)
        if parsed > 0:
            return str(parsed)
    elif ruleType == "game":
        parsed = _extractFirstRegexInt(_GAME_ID_RE, source)
        if parsed > 0:
            return str(parsed)
    elif ruleType == "creator":
        parsed = _extractFirstRegexInt(_USER_ID_RE, source)
        if parsed > 0:
            return str(parsed)

    match = _INTEGER_RE.search(rawValue) or _INTEGER_RE.search(fullText)
    if not match:
        return ""
    try:
        parsed = int(match.group(1))
    except (TypeError, ValueError):
        return ""
    return str(parsed) if parsed > 0 else ""


def _extractProposedBy(value: object) -> int:
    text = str(value or "").strip()
    mention = _DISCORD_MENTION_RE.search(text)
    if mention:
        try:
            return int(mention.group(1))
        except (TypeError, ValueError):
            return 0
    match = _INTEGER_RE.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _extractVoteCount(label: str, text: str) -> int:
    pattern = re.compile(rf"(?:^|\n)\s*{re.escape(label)}\s*:\s*`?(\d+)", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _isRejectedVote(embed: discord.Embed, fields: dict[str, str]) -> bool:
    statusText = "\n".join(
        [
            str(getattr(embed, "description", "") or ""),
            str(fields.get("status") or ""),
            str(fields.get("rule") or ""),
        ]
    ).lower()
    if "rejected" in statusText or "was removed" in statusText:
        return True

    votesText = fields.get("votes") or ""
    if votesText:
        flagVotes = _extractVoteCount("Flag", votesText)
        notFlagVotes = _extractVoteCount("Not a flag", votesText)
        if notFlagVotes > flagVotes:
            return True
    return False


def _looksLikeFlagVote(embed: discord.Embed, fields: dict[str, str]) -> bool:
    title = str(getattr(embed, "title", "") or "").lower()
    if "bg flag vote" in title or "background check flag vote" in title:
        return True
    return bool(fields.get("rule type") and fields.get("votes"))


def parseHistoricalFlagVoteEmbed(
    message: discord.Message,
    embed: discord.Embed,
) -> tuple[HistoricalFlagCandidate | None, str]:
    fields = _embedFields(embed)
    if not _looksLikeFlagVote(embed, fields):
        return None, "not_flag_vote"
    if _isRejectedVote(embed, fields):
        return None, "rejected"

    fullText = _embedText(embed)
    ruleType = _normalizeRuleType(fields.get("rule type") or fields.get("flag type") or fields.get("type"))
    rawValue = ""
    for key in _RULE_VALUE_FIELD_KEYS:
        if fields.get(key):
            rawValue = fields[key]
            break

    if not ruleType:
        if _extractFirstRegexInt(_CATALOG_ID_RE, fullText) > 0:
            ruleType = "item"
        else:
            return None, "missing_rule_type"

    if ruleType in _NUMERIC_RULE_TYPES:
        ruleValue = _extractNumericRuleValue(ruleType, rawValue, fullText)
    else:
        ruleValue = _cleanText(rawValue).lower()

    if not ruleValue:
        return None, "missing_rule_value"

    note = _cleanText(fields.get("note") or "") or None
    severity = _parseSeverity(fields.get("severity") or 50)
    proposedBy = _extractProposedBy(fields.get("proposed by") or fields.get("proposer") or fields.get("created by"))

    return (
        HistoricalFlagCandidate(
            ruleType=ruleType,
            ruleValue=ruleValue,
            note=note,
            severity=severity,
            proposedBy=proposedBy,
            sourceMessageId=int(getattr(message, "id", 0) or 0),
            sourceJumpUrl=str(getattr(message, "jump_url", "") or ""),
        ),
        "",
    )


def parseHistoricalFlagVoteMessage(message: discord.Message) -> tuple[list[HistoricalFlagCandidate], dict[str, int]]:
    candidates: list[HistoricalFlagCandidate] = []
    skipped: dict[str, int] = {}
    for embed in list(getattr(message, "embeds", []) or []):
        candidate, reason = parseHistoricalFlagVoteEmbed(message, embed)
        if candidate is not None:
            candidates.append(candidate)
            continue
        if reason and reason != "not_flag_vote":
            skipped[reason] = int(skipped.get(reason, 0) or 0) + 1
    return candidates, skipped


async def syncHistoricalFlagVotesFromChannel(
    channel: discord.TextChannel | discord.Thread,
    *,
    guildId: int,
    historyLimit: int | None = None,
    progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    scannedMessages = 0
    scannedEmbeds = 0
    parsedCandidates = 0
    importedRules = 0
    existingRules = 0
    itemCandidates = 0
    skippedReasons: dict[str, int] = {}
    sampleImported: list[str] = []
    sampleIssues: list[str] = []

    async for message in channel.history(limit=historyLimit, oldest_first=True):
        scannedMessages += 1
        scannedEmbeds += len(list(getattr(message, "embeds", []) or []))
        candidates, skipped = parseHistoricalFlagVoteMessage(message)
        for reason, count in skipped.items():
            skippedReasons[reason] = int(skippedReasons.get(reason, 0) or 0) + int(count or 0)

        for candidate in candidates:
            parsedCandidates += 1
            if candidate.ruleType == "item":
                itemCandidates += 1
            try:
                ruleId, created = await flagService.upsertRule(
                    candidate.ruleType,
                    candidate.ruleValue,
                    candidate.note,
                    candidate.proposedBy or None,
                    candidate.severity,
                )
            except Exception as exc:
                key = "db_error"
                skippedReasons[key] = int(skippedReasons.get(key, 0) or 0) + 1
                if len(sampleIssues) < 5:
                    sampleIssues.append(
                        f"{candidate.ruleType}:{candidate.ruleValue} failed ({exc.__class__.__name__})"
                    )
                continue

            if created:
                importedRules += 1
                if len(sampleImported) < 5:
                    sampleImported.append(f"#{ruleId} {candidate.ruleType}:{candidate.ruleValue}")
            else:
                existingRules += 1

        if progress is not None and scannedMessages % 250 == 0:
            await progress(
                {
                    "scannedMessages": scannedMessages,
                    "scannedEmbeds": scannedEmbeds,
                    "parsedCandidates": parsedCandidates,
                    "importedRules": importedRules,
                    "existingRules": existingRules,
                    "itemCandidates": itemCandidates,
                    "skippedReasons": dict(skippedReasons),
                }
            )

    visualSync = await flagService.syncItemVisualReferences(force=True)

    result = {
        "guildId": int(guildId or 0),
        "channelId": int(getattr(channel, "id", 0) or 0),
        "historyLimit": historyLimit,
        "scannedMessages": scannedMessages,
        "scannedEmbeds": scannedEmbeds,
        "parsedCandidates": parsedCandidates,
        "importedRules": importedRules,
        "existingRules": existingRules,
        "itemCandidates": itemCandidates,
        "skippedReasons": skippedReasons,
        "sampleImported": sampleImported,
        "sampleIssues": sampleIssues,
        "visualSync": visualSync,
    }
    if progress is not None:
        await progress(dict(result))
    return result
