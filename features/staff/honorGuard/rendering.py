from __future__ import annotations

from typing import Any, Mapping

import discord


def statusIcon(status: str, approver: str) -> str:
    normalized = str(status or "").strip().upper()
    if normalized == "APPROVED":
        return f":white_check_mark: Approved by {approver}"
    if normalized == "REJECTED":
        return f":x: Rejected by {approver}"
    if normalized == "NEEDS_INFO":
        return f":warning: Needs clarification by {approver}"
    return f":o: Pending"


def _mentionUser(userId: object) -> str:
    try:
        parsed = int(userId or 0)
    except (TypeError, ValueError):
        parsed = 0
    return f"<@{parsed}>" if parsed > 0 else "`unknown user`"


def _formatPoints(value: object) -> str:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if numeric.is_integer():
        return str(int(numeric))
    return str(round(numeric, 3))


def _clip(text: str, limit: int = 1024) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 3].rstrip() + "..."


def buildPointAwardEmbed(submission: Mapping[str, Any]) -> discord.Embed:
    awardedUserId = submission.get("targetUserId") or 0
    reason = str(submission.get("reason") or "").strip() or "_No reason provided._"
    embed = discord.Embed(
        title="Honor Guard Point Award",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Awarder", value=_mentionUser(submission.get("submitterId")), inline=False)
    embed.add_field(name="Awarded User", value=_mentionUser(awardedUserId), inline=False)
    embed.add_field(
        name="Awarded Promotion Points",
        value=_formatPoints(submission.get("promotionAwardedPoints")),
        inline=True,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Status", value=statusIcon(str(submission.get("status") or "")), inline=False)
    return embed

def buildSoloSentrySubmissionEmbed(submission: Mapping[str, Any]) -> discord.Embed:
    targetUserId = submission.get("targetUserId") or 0
    dutyDate = str(submission.get("eventDate") or "").strip() or "Unknown"
    minutes = int(submission.get("minutes") or 0)
    imageUrls = submission.get("imageUrls") or []

    embed = discord.Embed(
        title="Honor Guard Solo Sentry",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Submitter", value=_mentionUser(submission.get("submitterId")), inline=False)
    embed.add_field(name="Member", value=_mentionUser(targetUserId), inline=False)
    embed.add_field(name="Duty Date", value=f"`{dutyDate}`", inline=True)
    embed.add_field(name="Minutes", value=f"`{minutes}`", inline=True)
    embed.add_field(
        name="Promotion Event Points",
        value=_formatPoints(submission.get("promotionEventPoints")),
        inline=True,
    )
    if isinstance(imageUrls, list) and imageUrls:
        preview = "\n".join(
            f"[Screenshot {index + 1}]({str(url).strip()})"
            for index, url in enumerate(imageUrls[:6])
            if str(url).strip()
        )
        if preview:
            embed.add_field(name="Evidence", value=_clip(preview), inline=False)
    embed.add_field(name="Status", value=statusIcon(str(submission.get("status") or "")), inline=False)
    return embed

def buildEventReviewEmbed(submission: dict, event: dict, allAttendees: list[dict]) -> discord.Embed:
    eventId = event.get("eventId")
    eventTitle = str(event.get("eventTitle") or "").strip() or f"Event {eventId}"
    eventType = str(event.get("eventType") or "").strip() or "Unknown"
    eventDate = str(event.get("eventDate") or "").strip() or "Unknown"
    imageUrls = submission.get("imageUrls") or []
    embed = discord.Embed(
        title=f"Review for {eventType}",
        description=f"**Event Title:** `{eventTitle}`",
        color=discord.Color.blue(),
    )
    attendees = filter(lambda x: str(x.get("participantRole")).upper() == "ATTENDEE", allAttendees)
    supervisors = filter(lambda x: str(x.get("participantRole")).upper() == "SUPERVISOR", allAttendees)
    cohosts = filter(lambda x: str(x.get("participantRole")).upper() == "COHOST", allAttendees)

    attendeeMentions = [
        f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
        for index, row in enumerate(attendees)
        if int(row.get("userId") or 0) > 0
    ]
    supervisorMentions = [
        f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
        for index, row in enumerate(supervisors)
        if int(row.get("userId") or 0) > 0
    ]
    cohostMentions = [
        f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
        for index, row in enumerate(cohosts)
        if int(row.get("userId") or 0) > 0
    ]

    host = next((row for row in allAttendees if int(row.get("userId") or 0) == event.get("hostId")), None)
    hostMention = f"<@{int(host.get('userId') or 0)}> - {host.get("quotaPoints")}E{host.get("promotionEventPoints")} points"

    embed.add_field(name="Duration", value=f"`{event.get("durationMinutes") or 0} minutes`", inline=True)
    embed.add_field(name="Date", value=f"`{eventDate}`", inline=True)
    embed.add_field(name="Submitter", value=_mentionUser(submission.get("submitterId")), inline=False)
    embed.add_field(name="Host", value=hostMention, inline=False)
    embed.add_field(
        name=f"Supervisors ({len(supervisorMentions)}):\n",
        value="\n".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
        inline=False)
    embed.add_field(
        name=f"Cohosts ({len(cohostMentions)}):\n",
        value="\n".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
        inline=False)
    embed.add_field(
        name=f"Attendees ({len(attendeeMentions)}):\n",
        value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
        inline=False,
    )
    if isinstance(imageUrls, list) and imageUrls:
        preview = "\n".join(
            f"[Screenshot {index + 1}]({str(url).strip()})"
            for index, url in enumerate(imageUrls[:6])
            if str(url).strip()
        )
        if preview:
            embed.add_field(name="Evidence", value=_clip(preview), inline=False)
    reviewerMention = "N/A"
    if submission.get("status") != "PENDING":
        reviewerMention = _mentionUser(submission.get("reviewerId"))
    embed.add_field(name="Status", value=statusIcon(str(submission.get("status") or ""), reviewerMention), inline=False)
    return embed