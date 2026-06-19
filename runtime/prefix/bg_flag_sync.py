from __future__ import annotations

from typing import Any

import discord

from features.staff.bgItemReview import workflow as itemReviewWorkflow
from features.staff.bgflags import historySync


def _parseHistoryLimit(router: Any, content: str) -> tuple[int | None, str | None]:
    raw = router.indexToken(content, 1)
    if not raw or raw in {"all", "full"}:
        return None, None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None, "History limit must be a number, `all`, or omitted."
    if parsed <= 0:
        return None, "History limit must be greater than 0."
    return min(parsed, 50000), None


def _formatVisualSync(value: object) -> str:
    if not isinstance(value, dict):
        return "not needed"
    categoryCounts = value.get("categoryCounts")
    categoryText = ""
    if isinstance(categoryCounts, dict) and categoryCounts:
        parts = []
        for key in sorted(categoryCounts.keys()):
            count = int(categoryCounts.get(key) or 0)
            if count <= 0:
                continue
            parts.append(f"{str(key).replace('_', '-')} `{count}`")
        if parts:
            categoryText = " | sorted " + ", ".join(parts[:6])
    return (
        f"assets `{int(value.get('assetCount') or 0)}` | "
        f"valid `{int(value.get('validatedCount') or 0)}` | "
        f"typed `{int(value.get('metadataCount') or 0)}` | "
        f"errors `{int(value.get('errorCount') or 0)}` | "
        f"checked `{int(value.get('checkedCount') or 0)}`"
        f"{categoryText}"
    )


def _formatSkippedReasons(reasons: object) -> str:
    if not isinstance(reasons, dict) or not reasons:
        return "`0`"
    parts = []
    for key in sorted(reasons.keys()):
        count = int(reasons.get(key) or 0)
        if count <= 0:
            continue
        label = str(key).replace("_", "-")
        parts.append(f"{label} `{count}`")
    return " | ".join(parts) if parts else "`0`"


def _formatSummary(result: dict[str, Any]) -> str:
    limit = result.get("historyLimit")
    limitText = "all available messages" if limit is None else f"last `{int(limit)}` messages"
    lines = [
        "**Jane flag sync complete.**",
        f"Channel: <#{int(result.get('channelId') or 0)}> | scanned {limitText}",
        (
            f"Messages `{int(result.get('scannedMessages') or 0)}` | "
            f"embeds `{int(result.get('scannedEmbeds') or 0)}` | "
            f"vote embeds `{int(result.get('parsedCandidates') or 0)}`"
        ),
        (
            f"Rules added `{int(result.get('importedRules') or 0)}` | "
            f"already known `{int(result.get('existingRules') or 0)}` | "
            f"item rules seen `{int(result.get('itemCandidates') or 0)}`"
        ),
        f"Skipped: {_formatSkippedReasons(result.get('skippedReasons'))}",
        f"Visual refs: {_formatVisualSync(result.get('visualSync'))}",
    ]

    samples = [str(value) for value in list(result.get("sampleImported") or []) if str(value).strip()]
    if samples:
        lines.append("Added samples: " + ", ".join(f"`{sample}`" for sample in samples[:5]))
    issues = [str(value) for value in list(result.get("sampleIssues") or []) if str(value).strip()]
    if issues:
        lines.append("Issues: " + "; ".join(issues[:5]))

    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[:1897] + "..."
    return content


async def handleJaneFlagSync(router: Any, message: discord.Message) -> bool:
    if message.author.bot or not message.content:
        return False

    token = router.firstLowerToken(message.content or "")
    if token != "!janeflagsync":
        return False

    if not message.guild or not isinstance(message.author, discord.Member):
        return True

    if not router._headDeveloperAllowed(int(message.author.id)):
        await router._deleteSourceIfManageable(message)
        try:
            await message.channel.send(
                "Jane developer access required.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass
        return True

    historyLimit, limitError = _parseHistoryLimit(router, message.content or "")
    await router._deleteSourceIfManageable(message)
    if limitError:
        await message.channel.send(
            f"{limitError}\nUsage: `!JaneFlagSync [all|history-limit]`",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    guildId = int(message.guild.id)
    channelId = itemReviewWorkflow._queueChannelId(guildId)
    channel = await itemReviewWorkflow._resolveChannel(router.botClient, channelId)
    if channel is None:
        await message.channel.send(
            "BG item review channel is not configured or Jane cannot resolve it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    status = await message.channel.send(
        f"Scanning <#{int(channel.id)}> for historical BG flag vote webhooks...",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    async def _progress(snapshot: dict[str, Any]) -> None:
        try:
            await status.edit(
                content=(
                    f"Scanning <#{int(channel.id)}>... "
                    f"messages `{int(snapshot.get('scannedMessages') or 0)}`, "
                    f"vote embeds `{int(snapshot.get('parsedCandidates') or 0)}`, "
                    f"added `{int(snapshot.get('importedRules') or 0)}`."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass

    try:
        result = await historySync.syncHistoricalFlagVotesFromChannel(
            channel,
            guildId=guildId,
            historyLimit=historyLimit,
            progress=_progress,
        )
    except discord.Forbidden:
        await status.edit(
            content="Jane cannot read message history in the BG item review channel.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except Exception as exc:
        await status.edit(
            content=f"Jane flag sync failed: `{exc.__class__.__name__}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        raise

    await status.edit(
        content=_formatSummary(result),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return True
