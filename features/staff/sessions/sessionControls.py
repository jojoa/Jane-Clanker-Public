from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

import discord
from discord import ui

from runtime import permissions as runtimePermissions

log = logging.getLogger(__name__)

_service: Any = None
_canClockIn: Optional[Callable[[discord.Member], bool]] = None
_clockInDeniedMessage: Optional[Callable[[], str]] = None
_parseSessionId: Optional[Callable[[str], int]] = None
_safeInteractionReply: Optional[Callable[..., Awaitable[None]]] = None
_safeInteractionDefer: Optional[Callable[..., Awaitable[None]]] = None
_safeInteractionEditMessage: Optional[Callable[..., Awaitable[None]]] = None
_safeInteractionSendModal: Optional[Callable[..., Awaitable[None]]] = None
_requestSessionMessageUpdate: Optional[Callable[..., Awaitable[None]]] = None
_updateSessionMessage: Optional[Callable[..., Awaitable[None]]] = None
_buildGradingEmbed: Optional[Callable[..., discord.Embed]] = None
_setPendingBgRole: Optional[Callable[..., Awaitable[None]]] = None
_postOrientationResults: Optional[Callable[..., Awaitable[None]]] = None
_deleteSessionMessage: Optional[Callable[..., Awaitable[None]]] = None
_routeBgcSpreadsheet: Optional[Callable[..., Awaitable[Any]]] = None
_finishingSessionIds: set[int] = set()
_mentionPattern = re.compile(r"<@!?(\d+)>")
_attendeeLimitPattern = re.compile(r"attendee\s+limit\s+of\s+(\d+)", re.IGNORECASE)


def configure(
    *,
    serviceModule: Any,
    canClockIn: Callable[[discord.Member], bool],
    clockInDeniedMessage: Callable[[], str],
    parseSessionId: Callable[[str], int],
    safeInteractionReply: Callable[..., Awaitable[None]],
    safeInteractionDefer: Callable[..., Awaitable[None]],
    safeInteractionEditMessage: Callable[..., Awaitable[None]],
    safeInteractionSendModal: Callable[..., Awaitable[None]],
    requestSessionMessageUpdate: Callable[..., Awaitable[None]],
    updateSessionMessage: Callable[..., Awaitable[None]],
    buildGradingEmbed: Callable[..., discord.Embed],
    setPendingBgRole: Callable[..., Awaitable[None]],
    postOrientationResults: Callable[..., Awaitable[None]],
    deleteSessionMessage: Callable[..., Awaitable[None]],
    routeBgcSpreadsheet: Callable[..., Awaitable[Any]],
) -> None:
    global _service
    global _canClockIn
    global _clockInDeniedMessage
    global _parseSessionId
    global _safeInteractionReply
    global _safeInteractionDefer
    global _safeInteractionEditMessage
    global _safeInteractionSendModal
    global _requestSessionMessageUpdate
    global _updateSessionMessage
    global _buildGradingEmbed
    global _setPendingBgRole
    global _postOrientationResults
    global _deleteSessionMessage
    global _routeBgcSpreadsheet

    _service = serviceModule
    _canClockIn = canClockIn
    _clockInDeniedMessage = clockInDeniedMessage
    _parseSessionId = parseSessionId
    _safeInteractionReply = safeInteractionReply
    _safeInteractionDefer = safeInteractionDefer
    _safeInteractionEditMessage = safeInteractionEditMessage
    _safeInteractionSendModal = safeInteractionSendModal
    _requestSessionMessageUpdate = requestSessionMessageUpdate
    _updateSessionMessage = updateSessionMessage
    _buildGradingEmbed = buildGradingEmbed
    _setPendingBgRole = setPendingBgRole
    _postOrientationResults = postOrientationResults
    _deleteSessionMessage = deleteSessionMessage
    _routeBgcSpreadsheet = routeBgcSpreadsheet


def _isSpreadsheetRoutingResult(result: object) -> bool:
    return all(hasattr(result, attr) for attr in ("url", "expected_channel_ids", "posted_channel_ids", "skipped_reason"))


def _claimFinishingSession(sessionId: int) -> bool:
    normalizedSessionId = _positiveInt(sessionId)
    if normalizedSessionId <= 0:
        return False
    if normalizedSessionId in _finishingSessionIds:
        return False
    _finishingSessionIds.add(normalizedSessionId)
    return True


def _releaseFinishingSession(sessionId: int) -> None:
    normalizedSessionId = _positiveInt(sessionId)
    if normalizedSessionId <= 0:
        return
    _finishingSessionIds.discard(normalizedSessionId)


def _spreadsheetRoutingSucceeded(result: object) -> bool:
    if not _isSpreadsheetRoutingResult(result):
        return False
    url = str(getattr(result, "url", "") or "").strip()
    skippedReason = str(getattr(result, "skipped_reason", "") or "").strip().casefold()
    if not url:
        return skippedReason == "no passing attendees need a bgc spreadsheet."
    return True


def _positiveInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _interactionUserId(interaction: discord.Interaction) -> int:
    return _positiveInt(getattr(getattr(interaction, "user", None), "id", 0))


def _canManageSessionControls(
    interaction: discord.Interaction,
    session: dict,
    *,
    activeGraderId: int = 0,
) -> bool:
    userId = _interactionUserId(interaction)
    if userId > 0 and userId in {
        _positiveInt(session.get("hostId")),
        _positiveInt(activeGraderId),
    }:
        return True

    member = getattr(interaction, "user", None)
    if isinstance(member, discord.Member):
        return runtimePermissions.hasAdminOrManageGuild(member)
    return False


async def _replyAfterControlError(
    interaction: discord.Interaction,
    message: str = "This orientation control hit an internal error. Please try again.",
) -> None:
    if _safeInteractionReply is None:
        return
    try:
        await _safeInteractionReply(interaction, message, ephemeral=True)
    except Exception:
        log.exception("Failed to send orientation control error reply.")


def _sessionControlCustomId(interaction: discord.Interaction) -> str:
    data = getattr(interaction, "data", None)
    if not isinstance(data, dict):
        return ""
    return str(data.get("custom_id") or "").strip()


def _firstMentionId(value: object) -> int:
    match = _mentionPattern.search(str(value or ""))
    if not match:
        return 0
    return _positiveInt(match.group(1))


def _gradeFromAttendeeLine(value: object) -> str:
    lowered = str(value or "").lower()
    if "passed" in lowered or "white_check_mark" in lowered:
        return "PASS"
    if "failed" in lowered or ":x:" in lowered:
        return "FAIL"
    return "NOT_GRADED"


def _embedFieldValue(embed: object, fieldName: str) -> str:
    target = str(fieldName or "").strip().casefold()
    for field in list(getattr(embed, "fields", []) or []):
        name = str(getattr(field, "name", "") or "").strip().casefold()
        if name == target:
            return str(getattr(field, "value", "") or "")
    return ""


def _recoverableOrientationSnapshotFromMessage(
    interaction: discord.Interaction,
    sessionId: int,
) -> Optional[dict[str, Any]]:
    message = getattr(interaction, "message", None)
    if message is None:
        return None
    embeds = list(getattr(message, "embeds", []) or [])
    if not embeds:
        return None
    embed = embeds[0]
    title = str(getattr(embed, "title", "") or "").strip().casefold()
    if "orientation session" not in title:
        return None

    description = str(getattr(embed, "description", "") or "")
    limitMatch = _attendeeLimitPattern.search(description)
    maxAttendeeLimit = _positiveInt(limitMatch.group(1)) if limitMatch else 0

    hostId = _firstMentionId(_embedFieldValue(embed, "Host"))
    if hostId <= 0:
        hostId = _interactionUserId(interaction)

    attendeeGrades: list[tuple[int, str]] = []
    for field in list(getattr(embed, "fields", []) or []):
        fieldName = str(getattr(field, "name", "") or "").strip().casefold()
        if not fieldName.startswith("attendees"):
            continue
        for line in str(getattr(field, "value", "") or "").splitlines():
            userId = _firstMentionId(line)
            if userId > 0:
                attendeeGrades.append((userId, _gradeFromAttendeeLine(line)))

    footer = getattr(embed, "footer", None)
    footerText = str(getattr(footer, "text", "") or "")
    status = "FULL" if len(attendeeGrades) >= max(1, maxAttendeeLimit) else "OPEN"
    if "status:" in footerText.lower():
        status = footerText.split(":", 1)[-1].strip().upper() or status

    guild = getattr(interaction, "guild", None)
    channel = getattr(interaction, "channel", None) or getattr(message, "channel", None)
    guildId = _positiveInt(getattr(guild, "id", 0) or getattr(interaction, "guild_id", 0))
    channelId = _positiveInt(getattr(channel, "id", 0))
    messageId = _positiveInt(getattr(message, "id", 0))
    if guildId <= 0 or channelId <= 0 or messageId <= 0:
        return None

    return {
        "sessionId": int(sessionId),
        "guildId": guildId,
        "channelId": channelId,
        "messageId": messageId,
        "sessionType": "orientation",
        "hostId": hostId,
        "maxAttendeeLimit": max(maxAttendeeLimit, len(attendeeGrades), 1),
        "status": status,
        "attendeeGrades": attendeeGrades,
    }


async def _recoverMissingOrientationSessionFromMessage(
    interaction: discord.Interaction,
    sessionId: int,
) -> Optional[dict]:
    if _service is None or not hasattr(_service, "recoverSessionFromMessageSnapshot"):
        return None
    snapshot = _recoverableOrientationSnapshotFromMessage(interaction, sessionId)
    if not snapshot:
        return None
    recovered = await _service.recoverSessionFromMessageSnapshot(**snapshot)
    if recovered:
        log.warning(
            "Recovered missing orientation session %s from Discord message %s with %d attendee(s).",
            sessionId,
            snapshot.get("messageId"),
            len(snapshot.get("attendeeGrades") or []),
        )
    return recovered


async def _getSessionOrRecoverFromMessage(
    interaction: discord.Interaction,
    sessionId: int,
) -> Optional[dict]:
    session = await _service.getSession(sessionId)
    if session:
        return session
    return await _recoverMissingOrientationSessionFromMessage(interaction, sessionId)


def _spreadsheetRoutingNote(result: object) -> str:
    if not _isSpreadsheetRoutingResult(result):
        return ""

    url = str(getattr(result, "url", "") or "").strip()
    skippedReason = str(getattr(result, "skipped_reason", "") or "").strip()
    if not url:
        return skippedReason

    expectedIds = {
        _positiveInt(channelId)
        for channelId in list(getattr(result, "expected_channel_ids", []) or [])
        if _positiveInt(channelId) > 0
    }
    postedIds = {
        _positiveInt(channelId)
        for channelId in list(getattr(result, "posted_channel_ids", []) or [])
        if _positiveInt(channelId) > 0
    }
    if not expectedIds:
        return "BGC spreadsheet created, but no review channels were configured for the passing attendees."
    if expectedIds - postedIds:
        return f"BGC spreadsheet created; link posted to `{len(postedIds)}/{len(expectedIds)}` review channel(s)."
    return "BGC spreadsheet link posted."


async def _routeBgcSpreadsheetSafe(bot: discord.Client, sessionId: int, guild: Optional[discord.Guild]) -> None:
    if guild is None:
        log.error("Orientation session %s finished, but BGC spreadsheet routing had no guild.", sessionId)
        return
    try:
        result = await _routeBgcSpreadsheet(bot, sessionId, guild)
        if _isSpreadsheetRoutingResult(result) and not _spreadsheetRoutingSucceeded(result):
            log.error(
                "Orientation session %s finished with incomplete BG spreadsheet routing: %s",
                sessionId,
                _spreadsheetRoutingNote(result),
            )
    except Exception:
        log.exception("Failed to route BG spreadsheet for orientation session %s.", sessionId)


class JoinPasswordModal(ui.Modal, title="Enter Password"):
    password = ui.TextInput(label="Password", style=discord.TextStyle.short, required=True)

    def __init__(self, sessionId: int):
        super().__init__()
        self.sessionId = sessionId

    async def on_submit(self, interaction: discord.Interaction):
        await _safeInteractionDefer(interaction, ephemeral=True)

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await _safeInteractionReply(
                interaction,
                content="This action can only be used inside a server channel.",
                ephemeral=True,
            )

        if not _canClockIn(interaction.user):
            return await _safeInteractionReply(
                interaction,
                content=_clockInDeniedMessage(),
                ephemeral=True,
            )

        clockInResult = await _service.attemptClockIn(self.sessionId, interaction.user.id, str(self.password.value))
        resultStatus = str(clockInResult.get("status") or "").upper()
        if resultStatus == "SESSION_NOT_FOUND":
            return await _safeInteractionReply(
                interaction,
                content="This orientation session could not be found.",
                ephemeral=True,
            )
        if resultStatus == "SESSION_CLOSED":
            sessionStatus = str(clockInResult.get("sessionStatus") or "").upper()
            message = "This orientation is not currently open for clock-ins."
            if sessionStatus == "FULL":
                message = "This orientation has reached its attendee limit, try your luck next time!"
            return await _safeInteractionReply(
                interaction,
                content=message,
                ephemeral=True,
            )
        if resultStatus == "FULL":
            return await _safeInteractionReply(
                interaction,
                content="This orientation has reached its attendee limit, try your luck next time!",
                ephemeral=True,
            )
        if resultStatus == "ALREADY_JOINED":
            return await _safeInteractionReply(
                interaction,
                content="You are already clocked in to this orientation.",
                ephemeral=True,
            )
        if resultStatus == "BAD_PASSWORD":
            return await _safeInteractionReply(
                interaction,
                content="The password you entered is incorrect. Please try again.",
                ephemeral=True,
            )
        if resultStatus != "ADDED":
            return await _safeInteractionReply(
                interaction,
                content="This orientation could not process your clock-in right now. Please try again.",
                ephemeral=True,
            )

        await _safeInteractionReply(
            interaction,
            content="You have clocked in to this orientation.",
            ephemeral=True,
        )

        try:
            await _requestSessionMessageUpdate(
                interaction.client,
                self.sessionId,
                delaySec=0 if bool(clockInResult.get("reachedLimit")) else None,
            )
        except Exception:
            log.exception("Failed to refresh session message after attendee clock-in (session=%s).", self.sessionId)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error(
            "Orientation join modal failed for session %s.",
            self.sessionId,
            exc_info=(type(error), error, error.__traceback__),
        )
        await _replyAfterControlError(
            interaction,
            "This orientation could not process your clock-in right now. Please try again.",
        )


class SessionView(ui.View):
    def __init__(self, sessionId: int):
        super().__init__(timeout=None)
        self.sessionId = sessionId

        self.deleteBtn.custom_id = f"session:delete:{sessionId}"
        self.gradeBtn.custom_id = f"session:grade:{sessionId}"
        self.finishBtn.custom_id = f"session:finish:{sessionId}"
        self.joinBtn.custom_id = f"session:join:{sessionId}"

    async def disableIfLocked(self):
        session = await _service.getSession(self.sessionId)
        if not session:
            return
        if session["status"] in ("CANCELED", "FINISHED", "FINISHING"):
            for child in self.children:
                child.disabled = True
        if session["status"] in {"GRADING", "FULL", "FINISHING"}:
            self.joinBtn.disabled = True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: ui.Item[Any],
    ) -> None:
        customId = str(getattr(item, "custom_id", "") or "")
        log.error(
            "Orientation session control failed (session=%s custom_id=%s).",
            self.sessionId,
            customId,
            exc_info=(type(error), error, error.__traceback__),
        )
        await _replyAfterControlError(interaction)

    @ui.button(label="Delete", style=discord.ButtonStyle.danger, row=0)
    async def deleteBtn(self, interaction: discord.Interaction, button: ui.Button):
        await _safeInteractionDefer(interaction, ephemeral=True)
        sessionId = _parseSessionId(button.custom_id)
        session = await _getSessionOrRecoverFromMessage(interaction, sessionId)
        if not session:
            return await _safeInteractionReply(
                interaction,
                "This orientation session could not be found.",
                ephemeral=True,
            )
        if not _canManageSessionControls(interaction, session):
            return await _safeInteractionReply(
                interaction,
                "Only the session host or a server manager may Delete the current session.",
                ephemeral=True,
            )

        await _service.cancelSession(sessionId)
        await _updateSessionMessage(interaction.client, sessionId)
        await _safeInteractionReply(interaction, "Session canceled.", ephemeral=True)

    @ui.button(label="Change Grade", style=discord.ButtonStyle.primary, row=0)
    async def gradeBtn(self, interaction: discord.Interaction, button: ui.Button):
        await _safeInteractionDefer(interaction, ephemeral=True)
        sessionId = _parseSessionId(button.custom_id)
        session = await _getSessionOrRecoverFromMessage(interaction, sessionId)
        if not session:
            return await _safeInteractionReply(
                interaction,
                "This orientation session could not be found.",
                ephemeral=True,
            )
        if not _canManageSessionControls(interaction, session):
            return await _safeInteractionReply(
                interaction,
                "Only the session host or a server manager may open or use grading controls.",
                ephemeral=True,
            )

        attendees = await _service.getAttendees(sessionId)
        if not attendees:
            return await _safeInteractionReply(
                interaction,
                "No attendees are currently clocked in for grading.",
                ephemeral=True,
            )

        await _service.setStatus(sessionId, "GRADING")
        await _updateSessionMessage(interaction.client, sessionId)

        idx = session["gradingIndex"]
        if idx >= len(attendees):
            await _service.resetGradingIndex(sessionId)
            session = await _service.getSession(sessionId)
            idx = session["gradingIndex"]

        attendeeUserId = attendees[idx]["userId"]
        embed = _buildGradingEmbed(session, interaction.user, attendeeUserId, idx + 1, len(attendees))
        view = GradingView(sessionId, interaction.user.id)
        await _safeInteractionReply(interaction, embed=embed, view=view, ephemeral=True)

    @ui.button(label="Finish", style=discord.ButtonStyle.success, row=0)
    async def finishBtn(self, interaction: discord.Interaction, button: ui.Button):
        await _safeInteractionDefer(interaction, ephemeral=True)
        sessionId = _parseSessionId(button.custom_id)
        session = await _getSessionOrRecoverFromMessage(interaction, sessionId)
        if not session:
            return await _safeInteractionReply(
                interaction,
                "This orientation session could not be found.",
                ephemeral=True,
            )
        if not _canManageSessionControls(interaction, session):
            return await _safeInteractionReply(
                interaction,
                "Only the session host or a server manager may Finish the orientation.",
                ephemeral=True,
            )
        status = str(session.get("status") or "").upper()
        if status == "FINISHING":
            return await _safeInteractionReply(
                interaction,
                "This orientation is already being finished.",
                ephemeral=True,
            )
        if status == "FINISHED":
            return await _safeInteractionReply(
                interaction,
                "This orientation has already been finished.",
                ephemeral=True,
            )
        if status == "CANCELED":
            return await _safeInteractionReply(
                interaction,
                "This orientation has already been canceled.",
                ephemeral=True,
            )
        if not _claimFinishingSession(sessionId):
            return await _safeInteractionReply(
                interaction,
                "This orientation is already being finished.",
                ephemeral=True,
            )

        allowed, reason = await _service.isFinishAllowed(sessionId)
        if not allowed:
            _releaseFinishingSession(sessionId)
            return await _safeInteractionReply(interaction, reason, ephemeral=True)

        statusLocked = False
        statusBeforeFinish = status
        try:
            if session.get("sessionType") == "orientation":
                await _service.setStatus(sessionId, "FINISHING")
                statusLocked = True
                await _updateSessionMessage(interaction.client, sessionId)
                await _postOrientationResults(interaction.client, sessionId)
                await _service.finishSession(sessionId)
                statusLocked = False
                await _deleteSessionMessage(interaction.client, sessionId)
                asyncio.create_task(_routeBgcSpreadsheetSafe(interaction.client, sessionId, interaction.guild))
                await _safeInteractionReply(
                    interaction,
                    (
                        "Finished. Orientation results posted.\n"
                        "BGC spreadsheet creation is running in the background and will post the link when ready."
                    ),
                    ephemeral=True,
                )
                return
            await _service.finishSession(sessionId)
            await _updateSessionMessage(interaction.client, sessionId)
            await _safeInteractionReply(
                interaction,
                "Finished. BG checks posted for moderation.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Failed to finish orientation session %s", sessionId)
            if statusLocked:
                try:
                    await _service.setStatus(sessionId, statusBeforeFinish)
                    await _updateSessionMessage(interaction.client, sessionId)
                except Exception:
                    log.exception("Failed to restore orientation session %s after finish failure.", sessionId)
            await _safeInteractionReply(
                interaction,
                "The session could not be finalized due to an internal error.",
                ephemeral=True,
            )
        finally:
            _releaseFinishingSession(sessionId)

    @ui.button(emoji="\u2705", style=discord.ButtonStyle.success, row=1)
    async def joinBtn(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await _safeInteractionReply(
                interaction,
                "This action can only be used inside a server channel.",
                ephemeral=True,
            )
        sessionId = _parseSessionId(button.custom_id)
        if not _canClockIn(interaction.user):
            return await _safeInteractionReply(interaction, _clockInDeniedMessage(), ephemeral=True)
        await _safeInteractionSendModal(interaction, JoinPasswordModal(sessionId))


class GradingView(ui.View):
    def __init__(self, sessionId: int, hostId: int):
        super().__init__(timeout=900)
        self.sessionId = sessionId
        self.hostId = hostId

        self.passBtn.custom_id = f"grading:pass:{sessionId}"
        self.failBtn.custom_id = f"grading:fail:{sessionId}"

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: ui.Item[Any],
    ) -> None:
        customId = str(getattr(item, "custom_id", "") or "")
        log.error(
            "Orientation grading control failed (session=%s custom_id=%s).",
            self.sessionId,
            customId,
            exc_info=(type(error), error, error.__traceback__),
        )
        await _replyAfterControlError(
            interaction,
            "This grading control hit an internal error. Please try again.",
        )

    async def applyGrade(self, interaction: discord.Interaction, grade: str):
        await _safeInteractionDefer(interaction, ephemeral=True)
        session = await _service.getSession(self.sessionId)
        if not session:
            for child in self.children:
                child.disabled = True
            await _safeInteractionEditMessage(self, interaction, True, content="Session not found.", view=self)
            return
        if not _canManageSessionControls(interaction, session, activeGraderId=self.hostId):
            return await _safeInteractionReply(
                interaction,
                "Only the session host or a server manager may use grading controls.",
                ephemeral=True,
            )

        attendees = await _service.getAttendees(self.sessionId)
        if not attendees:
            for child in self.children:
                child.disabled = True
            await _safeInteractionEditMessage(self, interaction, True, content="No attendees.", view=self)
            return

        idx = session["gradingIndex"]
        if idx >= len(attendees):
            for child in self.children:
                child.disabled = True
            await _updateSessionMessage(interaction.client, self.sessionId)
            await _safeInteractionEditMessage(self, interaction, True, content="Grading complete.", view=self)
            return await _safeInteractionReply(interaction, "All attendees processed.", ephemeral=True)

        userId = attendees[idx]["userId"]
        await _service.setExamGrade(self.sessionId, userId, grade)
        if session.get("sessionType") == "orientation":
            await _setPendingBgRole(interaction.guild, userId, grade == "PASS")
        await _service.incrementGradingIndex(self.sessionId)

        await _updateSessionMessage(interaction.client, self.sessionId)

        session = await _service.getSession(self.sessionId)
        attendees = await _service.getAttendees(self.sessionId)
        idx = session["gradingIndex"]

        if idx >= len(attendees):
            for child in self.children:
                child.disabled = True
            await _safeInteractionEditMessage(self, interaction, True, content="Grading complete.", view=self)
            return await _safeInteractionReply(interaction, "All attendees processed.", ephemeral=True)

        nextUserId = attendees[idx]["userId"]
        hostMember = interaction.guild.get_member(self.hostId) or interaction.user
        embed = _buildGradingEmbed(session, hostMember, nextUserId, idx + 1, len(attendees))
        await _safeInteractionEditMessage(self, interaction, False, embed=embed, view=self)
       
    @ui.button(label="Pass", style=discord.ButtonStyle.success, emoji="\u2705")
    async def passBtn(self, interaction: discord.Interaction, button: ui.Button):
        await self.applyGrade(interaction, "PASS")

    @ui.button(label="Fail", style=discord.ButtonStyle.danger, emoji="\u274C")
    async def failBtn(self, interaction: discord.Interaction, button: ui.Button):
        await self.applyGrade(interaction, "FAIL")


async def handleSessionControlFallbackInteraction(interaction: discord.Interaction) -> bool:
    customId = _sessionControlCustomId(interaction)
    if not customId.startswith("session:"):
        return False

    # If the persistent view is attached normally, its callback should acknowledge
    # immediately. Waiting briefly lets that normal path win while still rescuing
    # stale/unattached session components before Discord expires the interaction.
    await asyncio.sleep(1.25)
    if interaction.response.is_done():
        return True

    try:
        sessionId = _parseSessionId(customId)
    except Exception:
        await _replyAfterControlError(interaction, "This orientation control is invalid or expired.")
        return True

    view = SessionView(sessionId)
    item = next((child for child in view.children if getattr(child, "custom_id", None) == customId), None)
    if item is None or not hasattr(item, "callback"):
        await _replyAfterControlError(interaction, "This orientation control is invalid or expired.")
        return True

    messageId = _positiveInt(getattr(getattr(interaction, "message", None), "id", 0))
    if messageId > 0:
        try:
            interaction.client.add_view(view, message_id=messageId)
        except Exception:
            log.exception("Failed to reattach orientation session view for session %s.", sessionId)

    try:
        await item.callback(interaction)
    except Exception as exc:
        await view.on_error(interaction, exc, item)
    return True
