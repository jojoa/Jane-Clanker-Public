from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Optional

import discord

import config
from features.staff.honorGuard import outputs as honorGuardOutputs
from features.staff.honorGuard import rendering as honorGuardRendering
from features.staff.honorGuard import service as honorGuardService
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions


log = logging.getLogger(__name__)
joinButtonEmoji = "\N{WHITE HEAVY CHECK MARK}"

def _hasRole(member: discord.Member, roleId: Optional[int]) -> bool:
    return runtimePermissions.hasAnyRole(member, [roleId])


async def _safeInteractionReply(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = True,
) -> None:
    await interactionRuntime.safeInteractionReply(
        interaction,
        content=message,
        ephemeral=ephemeral,
    )


async def _safeInteractionDefer(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
    thinking: bool = False,
) -> None:
    await interactionRuntime.safeInteractionDefer(
        interaction,
        ephemeral=ephemeral,
        thinking=thinking,
    )


def _setAllButtonsDisabled(view: discord.ui.View, disabled: bool) -> None:
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            child.disabled = disabled

async def _disableIfLocked(self):
    session = await self._clockInEngine.getSession(self.sessionId)
    if session and session.get("status") in {"FINISHED", "CANCELED"}:
        _setAllButtonsDisabled(self, True)
    if session and session.get("status") in {"GRADING", "SUBMITTING"}:
        self.joinBtn.disabled = True
    if session and session.get("status") in {"OPEN"}:
        _setAllButtonsDisabled(self, False)


class HonorGuardPointAwardReviewView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", submissionId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.submissionId = int(submissionId)
        self._lock = asyncio.Lock()

    async def _getSubmission(self) -> Optional[dict]:
        return await honorGuardService.getPointAwardSubmission(self.submissionId)

    def _canReview(self, member: discord.Member) -> bool:
        reviewerRoleId = int(getattr(config, "honorGuardReviewerRoleId", 0) or 0)
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if reviewerRoleId <= 0:
            return False
        return _hasRole(member, reviewerRoleId)

    async def _updateSubmissionStatus(
        self,
        *,
        status: str,
        reviewerId: int,
        note: Optional[str],
        threadId: Optional[int],
    ) -> None:
        await honorGuardService.updatePointAwardStatus(
            self.submissionId,
            status,
            reviewerId=reviewerId,
            note=note,
            threadId=threadId,
        )

    async def _syncApprovedSubmission(self) -> dict:
        return await honorGuardService.syncApprovedSubmissionToSheet(self.submissionId)

    async def _logHonorGuardSheetChange(
        self,
        *,
        reviewerId: int,
        change: str,
        details: str,
    ) -> None:
        await honorGuardOutputs.sendHonorGuardSheetChangeLog(
            self.cog.bot,
            reviewerId=reviewerId,
            change=change,
            details=details,
        )

    async def _buildSubmissionEmbed(self) -> Optional[discord.Embed]:
        submission = await self._getSubmission()
        if not submission:
            return None
        return honorGuardRendering.buildPointAwardEmbed(submission)

    async def _finishDecision(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        note: Optional[str],
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await _safeInteractionReply(
                interaction,
                "This action can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not self._canReview(interaction.user):
            await _safeInteractionReply(
                interaction,
                "You are not authorized to review this point award.",
                ephemeral=True,
            )
            return

        async with self._lock:
            submission = await self._getSubmission()
            if not submission:
                await _safeInteractionReply(interaction, "Point award not found.", ephemeral=True)
                return

            if submission.get("status") in {"APPROVED", "REJECTED", "CANCELED"}:
                await _safeInteractionReply(
                    interaction,
                    "This submission has already been finalized.",
                    ephemeral=True,
                )
                return

            await _safeInteractionDefer(interaction, ephemeral=True, thinking=True)

            previousState = [child.disabled for child in self.children]
            _setAllButtonsDisabled(self, True)
            if isinstance(interaction.message, discord.Message):
                try:
                    await interaction.message.edit(view=self)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            try:
                await self._updateSubmissionStatus(
                    status=status,
                    reviewerId=interaction.user.id,
                    note=note,
                    threadId=None,
                )

                if status == "APPROVED":
                    syncResult = await self._syncApprovedSubmission()
                    submission = await self._getSubmission()
                    if submission:
                        awardedPoints = submission.get("promotionAwardedPoints") or 0
                        syncStatusText = "already synced" if syncResult.get("alreadySynced") else "synced now"
                        await self._logHonorGuardSheetChange(
                            reviewerId=interaction.user.id,
                            change="Edited Honor Guard points for an approved point award.",
                            details=(
                                f"User: <@{int(submission.get('targetUserId') or 0)}> | "
                                f"Points +{awardedPoints} AP | "
                                f"Reason: {submission.get('reason') or 'N/A'} | "
                                f"Sheet: {syncStatusText}"
                            ),
                        )

                _setAllButtonsDisabled(self, True)

                if isinstance(interaction.message, discord.Message):
                    embed = await self._buildSubmissionEmbed()
                    try:
                        if embed:
                            await interaction.message.edit(embed=embed, view=self)
                        else:
                            await interaction.message.edit(view=self)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                if status == "APPROVED":
                    await _safeInteractionReply(interaction, "Point award approved.", ephemeral=True)
                elif status == "REJECTED":
                    await _safeInteractionReply(interaction, "Point award rejected.", ephemeral=True)
                else:
                    await _safeInteractionReply(
                        interaction,
                        "Point award status updated.",
                        ephemeral=True,
                    )
            except Exception as exc:
                for idx, child in enumerate(self.children):
                    child.disabled = previousState[idx] if idx < len(previousState) else False
                if isinstance(interaction.message, discord.Message):
                    try:
                        await interaction.message.edit(view=self)
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                log.exception("Failed to process point award review decision.")
                await _safeInteractionReply(
                    interaction,
                    f"Could not process this action: {exc}",
                    ephemeral=True,
                )

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="honorGuard_award_review:approve",
    )
    async def approveBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finishDecision(interaction, status="APPROVED", note=None)

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="honorGuard_award_review:reject",
    )
    async def rejectBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finishDecision(interaction, status="REJECTED", note=None)


class HonorGuardSoloSentryReviewView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", submissionId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.submissionId = int(submissionId)
        self._lock = asyncio.Lock()

    async def _getSubmission(self) -> Optional[dict]:
        return await honorGuardService.getSoloSentrySubmission(self.submissionId)

    def _canReview(self, member: discord.Member) -> bool:
        reviewerRoleId = int(getattr(config, "honorGuardReviewerRoleId", 0) or 0)
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if reviewerRoleId <= 0:
            return False
        return _hasRole(member, reviewerRoleId)

    async def _updateSubmissionStatus(
        self,
        *,
        status: str,
        reviewerId: int,
        note: Optional[str],
        threadId: Optional[int],
    ) -> None:
        await honorGuardService.updateSoloSentrySubmissionStatus(
            self.submissionId,
            status,
            reviewerId=reviewerId,
            note=note,
            threadId=threadId,
        )

    async def _syncApprovedSubmission(self) -> dict:
        return await honorGuardService.syncApprovedSubmissionToSheet(self.submissionId)

    async def _logHonorGuardSheetChange(
        self,
        *,
        reviewerId: int,
        change: str,
        details: str,
    ) -> None:
        await honorGuardOutputs.sendHonorGuardSheetChangeLog(
            self.cog.bot,
            reviewerId=reviewerId,
            change=change,
            details=details,
        )

    async def _buildSubmissionEmbed(self) -> Optional[discord.Embed]:
        submission = await self._getSubmission()
        if not submission:
            return None
        return honorGuardRendering.buildSoloSentrySubmissionEmbed(submission)

    async def _finishDecision(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        note: Optional[str],
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await _safeInteractionReply(
                interaction,
                "This action can only be used inside a server.",
                ephemeral=True,
            )
            return
        if not self._canReview(interaction.user):
            await _safeInteractionReply(
                interaction,
                "You are not authorized to review this sentry log.",
                ephemeral=True,
            )
            return

        async with self._lock:
            submission = await self._getSubmission()
            if not submission:
                await _safeInteractionReply(interaction, "Solo sentry submission not found.", ephemeral=True)
                return

            if submission.get("status") in {"APPROVED", "REJECTED", "CANCELED"}:
                await _safeInteractionReply(
                    interaction,
                    "This submission has already been finalized.",
                    ephemeral=True,
                )
                return

            await _safeInteractionDefer(interaction, ephemeral=True, thinking=True)

            previousState = [child.disabled for child in self.children]
            _setAllButtonsDisabled(self, True)
            if isinstance(interaction.message, discord.Message):
                try:
                    await interaction.message.edit(view=self)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            try:
                await self._updateSubmissionStatus(
                    status=status,
                    reviewerId=interaction.user.id,
                    note=note,
                    threadId=None,
                )

                if status == "APPROVED":
                    syncResult = await self._syncApprovedSubmission()
                    submission = await self._getSubmission()
                    if submission:
                        syncStatusText = "already synced" if syncResult.get("alreadySynced") else "synced now"
                        await self._logHonorGuardSheetChange(
                            reviewerId=interaction.user.id,
                            change="Edited Honor Guard points for an approved solo sentry log.",
                            details=(
                                f"User: <@{int(submission.get('targetUserId') or 0)}> | "
                                f"Date: {submission.get('eventDate') or 'N/A'} | "
                                f"Points +{submission.get('promotionEventPoints') or 0} EP, "
                                f"+{submission.get('quotaPoints') or 0} QP | "
                                f"Sheet: {syncStatusText}"
                            ),
                        )

                _setAllButtonsDisabled(self, True)

                if isinstance(interaction.message, discord.Message):
                    embed = await self._buildSubmissionEmbed()
                    try:
                        if embed:
                            await interaction.message.edit(embed=embed, view=self)
                        else:
                            await interaction.message.edit(view=self)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                if status == "APPROVED":
                    await _safeInteractionReply(interaction, "Solo sentry log approved.", ephemeral=True)
                elif status == "REJECTED":
                    await _safeInteractionReply(interaction, "Solo sentry log rejected.", ephemeral=True)
                else:
                    await _safeInteractionReply(
                        interaction,
                        "Solo sentry log status updated.",
                        ephemeral=True,
                    )
            except Exception as exc:
                for idx, child in enumerate(self.children):
                    child.disabled = previousState[idx] if idx < len(previousState) else False
                if isinstance(interaction.message, discord.Message):
                    try:
                        await interaction.message.edit(view=self)
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                log.exception("Failed to process solo sentry review decision.")
                await _safeInteractionReply(
                    interaction,
                    f"Could not process this action: {exc}",
                    ephemeral=True,
                )

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="honorGuard_sentry_review:approve",
    )
    async def approveBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finishDecision(interaction, status="APPROVED", note=None)

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="honorGuard_sentry_review:reject",
    )
    async def rejectBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finishDecision(interaction, status="REJECTED", note=None)


class HonorGuardEventView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", eventId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.eventId = int(eventId)

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.danger,
        row=0,
        custom_id="honorguard_event:delete",
    )
    async def deleteBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventDelete(interaction, self.eventId)

    @discord.ui.button(
        label="Manage",
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id="honorguard_event:manage",
    )
    async def manageBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.openEventManage(interaction, self.eventId)

    @discord.ui.button(
        label="Finish",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="honorguard_event:finish",
    )
    async def finishBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.openEventFinish(interaction, self.eventId)

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        emoji=joinButtonEmoji,
        row=1,
        custom_id="honorguard_event:join",
    )
    async def joinBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventJoin(interaction, self.eventId)

class HonorGuardEventSubmitView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", eventId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.eventId = int(eventId)

    @discord.ui.button(
        label="Points",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="honorguard_event_submit:points",
    )
    async def pointsBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.openEditPoints(interaction, self.eventId)

    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="honorguard_event_submit:submit",
    )
    async def submitBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventFinish(interaction, self.eventId)
    
    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="honorguard_event_submit:submit",
    )
    async def submitBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventSubmit(interaction, self.eventId)

class HonorGuardEventFinishModal(discord.ui.Modal, title="Finish Event"):
    durationMinutesInput = discord.ui.TextInput(
        label="Event duration (minutes)",
        style=discord.TextStyle.short,
        required=True,
        max_length=5,
        placeholder="Example: 45",
    )

    def __init__(self, cog: "HonorGuardCog", eventId: int):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        event = honorGuardService.getEventRecord(self.eventId)
        startedAt = event.get("startedAt") if event else datetime.now(datetime.timezone.utc)
        finishedAt = event.get("finishedAt") if event else datetime.now(datetime.timezone.utc)
        duration = finishedAt - startedAt
        minutes = int(duration.total_seconds() // 60)
        self.durationMinutesInput.default = str(minutes)
        self.durationMinutesInput.placeholder = f"Default: {minutes}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.durationMinutesInput.value or "").strip()
        try:
            durationMinutes = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Duration must be a whole number of minutes.",
                ephemeral=True,
            )
            return
        if durationMinutes <= 0:
            await interaction.response.send_message(
                "Duration must be greater than 0 minutes.",
                ephemeral=True,
            )
            return
        await self.cog.openSubmitEvent(interaction, self.eventId, durationMinutes)

class HonorGuardEventManageView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", eventId: int, managementId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.eventId = int(eventId)
        self.managementId = int(managementId)

        @discord.ui.button(
            label="Edit Cohosts",
            style=discord.ButtonStyle.secondary,
            row=0,
            custom_id="honorguard_event_manage:edit_cohosts",
        )
        async def editCohostsBtn(interaction: discord.Interaction, _: discord.ui.Button) -> None:
            await self.cog.openEditCohosts(interaction, self.eventId, self.managementId)

        @discord.ui.button(
            label="Edit Supervisors",
            style=discord.ButtonStyle.secondary,
            row=0,
            custom_id="honorguard_event_manage:edit_supervisors",
        )
        async def editSupervisorsBtn(interaction: discord.Interaction, _: discord.ui.Button) -> None:
            await self.cog.openEditSupervisors(interaction, self.eventId, self.managementId)

        @discord.ui.button(
            label="Remove Attendees",
            style=discord.ButtonStyle.secondary,
            row=0,
            custom_id="honorguard_event_manage:remove_attendees",
        )
        async def removeAttendeesBtn(interaction: discord.Interaction, _: discord.ui.Button) -> None:
            await self.cog.openRemoveAttendees(interaction, self.eventId, self.managementId)

        @discord.ui.button(
            label="Done",
            style=discord.ButtonStyle.primary,
            row=1,
            custom_id="honorguard_event_manage:done",
        )
        async def doneBtn(interaction: discord.Interaction, _: discord.ui.Button) -> None:
            await self.cog.closeEventManage(interaction, self.eventId, self.managementId)
        
        @discord.ui.button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1,
            custom_id="honorguard_event_manage:cancel_changes",
        )
        async def cancelChangesBtn(interaction: discord.Interaction, _: discord.ui.Button) -> None:
            await self.cog.cancelEventManage(interaction, self.eventId, self.managementId)

class HonorGuardEditCohostsModal(discord.ui.Modal, title="Edit Cohosts"):
    def __init__(self, cog: "HonorGuardCog", eventId: int, cohostText: str, managementId: int):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        self.cohostText = cohostText
        self.managementId = int(managementId)

        self.cohostsInput = discord.ui.TextInput(
            label="Cohost user IDs (comma-separated)",
            style=discord.TextStyle.short,
            required=False,
            default=self.cohostText if cohostText else None,
            placeholder="Example: '123456789012345678, 234567890123456789'" if not cohostText else None,
        )
        
        self.add_item(self.cohostsInput)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.cohostsInput.value or "").strip()
        userIdStrs = [part.strip() for part in raw.split(",") if part.strip()]
        userIds = []
        for userIdStr in userIdStrs:
            try:
                userId = int(userIdStr)
                if userId >= 0:
                    userIds.append(userId)
            except ValueError:
                await interaction.response.send_message(
                    f"Invalid user ID: {userIdStr}",
                    ephemeral=True,
                )
                return
        await self.cog.handleEditCohosts(interaction, self.eventId, userIds, self.managementId)

class HonorGuardEditSupervisorsModal(discord.ui.Modal, title="Edit Supervisors"):
    def __init__(self, cog: "HonorGuardCog", eventId: int, supervisorText: str, managementId: int):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        self.supervisorText = supervisorText
        self.managementId = int(managementId)

        self.supervisorsInput = discord.ui.TextInput(
            label="Supervisor user IDs (comma-separated)",
            style=discord.TextStyle.short,
            required=False,
            default=self.supervisorText if supervisorText else None,
            placeholder="Example: '123456789012345678, 234567890123456789'" if not supervisorText else None,
        )
        
        self.add_item(self.supervisorsInput)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.supervisorsInput.value or "").strip()
        userIdStrs = [part.strip() for part in raw.split(",") if part.strip()]
        userIds = []
        for userIdStr in userIdStrs:
            try:
                userId = int(userIdStr)
                if userId >= 0:
                    userIds.append(userId)
            except ValueError:
                await interaction.response.send_message(
                    f"Invalid user ID: {userIdStr}",
                    ephemeral=True,
                )
                return
        await self.cog.handleEditSupervisors(interaction, self.eventId, userIds, self.managementId)

class HonorGuardRemoveAttendeesModal(discord.ui.Modal, title="Remove Attendees"):
    def __init__(self, cog: "HonorGuardCog", eventId: int, managementId: int):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        self.managementId = int(managementId)

        self.attendeesInput = discord.ui.TextInput(
            label="Attendee user IDs (comma-separated)",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="Example: 123456789012345678, 234567890123456789" ,
        )
        
        self.add_item(self.attendeesInput)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.attendeesInput.value or "").strip()
        userIdStrs = [part.strip() for part in raw.split(",") if part.strip()]
        userIds = []
        for userIdStr in userIdStrs:
            try:
                userId = int(userIdStr)
                if userId >= 0:
                    userIds.append(userId)
            except ValueError:
                await interaction.response.send_message(
                    f"Invalid user ID: {userIdStr}",
                    ephemeral=True,
                )
                return
        await self.cog.handleRemoveAttendees(interaction, self.eventId, userIds, self.managementId)