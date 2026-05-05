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
from runtime import viewBases as runtimeViewBases


log = logging.getLogger(__name__)
joinButtonEmoji = "\N{WHITE HEAVY CHECK MARK}"

def _memberDisplayName(member: discord.Member) -> str:
    return str(
        getattr(member, "display_name", None)
        or getattr(member, "global_name", None)
        or getattr(member, "name", None)
        or member.id
    ).strip()

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
        requestedBy: str,
        requestMessageUrl: str,
        change: str,
        details: str,
    ) -> None:
        await honorGuardOutputs.sendHonorGuardSheetChangeLog(
            self.cog.bot,
            reviewerId=reviewerId,
            requestedBy=requestedBy,
            requestMessageUrl=requestMessageUrl,
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
                        requestedBy = f"<@{int(submission.get('submitterId') or 0)}>"
                        requestMessageUrl = str(getattr(interaction.message, "jump_url", "") or "")
                        await self._logHonorGuardSheetChange(
                            reviewerId=interaction.user.id,
                            requestedBy=requestedBy,
                            requestMessageUrl=requestMessageUrl,
                            change="Edited Honor-Guard points for an approved point award.",
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
        requestedBy: str,
        requestMessageUrl: str,
        change: str,
        details: str,
    ) -> None:
        await honorGuardOutputs.sendHonorGuardSheetChangeLog(
            self.cog.bot,
            reviewerId=reviewerId,
            requestedBy=requestedBy,
            requestMessageUrl=requestMessageUrl,
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
                        requestedBy = f"<@{int(submission.get('submitterId') or 0)}>"
                        requestMessageUrl = str(getattr(interaction.message, "jump_url", "") or "")
                        await self._logHonorGuardSheetChange(
                            reviewerId=interaction.user.id,
                            requestedBy=requestedBy,
                            requestMessageUrl=requestMessageUrl,
                            change="Edited Honor-Guard points for an approved solo sentry log.",
                            details=(
                                f"Target: <@{int(submission.get('targetUserId') or 0)}> | Date: {submission.get('eventDate') or 'N/A'} | EP: +{submission.get('promotionEventPoints') or 0} | QP: +{submission.get('quotaPoints') or 0} | Sheet: {syncStatusText}"
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

class HonorGuardEventReviewView(discord.ui.View):
    def __init__(self, cog: "HonorGuardCog", submissionId: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.submissionId = int(submissionId)
        self._lock = asyncio.Lock()

    async def _getSubmission(self) -> Optional[dict]:
        return await honorGuardService.getEventSubmission(self.submissionId)

    async def _getEvent(self, eventId: int) -> Optional[dict]:
        return await honorGuardService.getEventRecord(eventId)

    async def _getAllAttendees(self, eventId: int) -> list[dict]:
        return await honorGuardService.listHonorGuardAttendees(int(eventId))

    async def _canReview(self, member: discord.Member) -> bool:
        submission = await self._getSubmission()
        reviewerRoleId = int(getattr(config, "honorGuardReviewerRoleId", 0) or 0)
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if reviewerRoleId <= 0:
            return False
        if member.id == submission.get("submitterId"):
            return False
        if member.id == submission.get("targetUserId"):
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
        submission = await self._getSubmission()
        await honorGuardService.updateEventSubmissionStatus(
            self.submissionId,
            submission.get("eventId") or 0,
            status,
            reviewerId=reviewerId,
            note=note,
            threadId=threadId,
        )

    async def _syncApprovedSubmission(self, eventId: int) -> dict:
        result = await honorGuardService.syncApprovedSubmissionToSheet(self.submissionId)
        hostUpdate = await honorGuardService.syncEventRecordToSheets(eventId)
        return {**result, "eventHostUpdate": hostUpdate.get("eventHostUpdate") != None, "archiveSynced": hostUpdate.get("archiveSynced")}

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
        event = await self._getEvent(int(submission.get("eventId") or 0))
        allAttendees = await self._getAllAttendees(int(submission.get("eventId") or 0))
        if not submission or not event or not allAttendees:
            return None
        return honorGuardRendering.buildEventReviewEmbed(submission, event, allAttendees)

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
        if not await self._canReview(interaction.user):
            await _safeInteractionReply(
                interaction,
                "You are not authorized to review this event submission.",
                ephemeral=True,
            )
            return

        async with self._lock:
            submission = await self._getSubmission()
            if not submission:
                await _safeInteractionReply(interaction, "Event submission not found.", ephemeral=True)
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
                    syncResult = await self._syncApprovedSubmission(submission.get("eventId") or 0)
                    submission = await self._getSubmission()
                    allAttendees = await self._getAllAttendees(int(submission.get("eventId") or 0))
                    if submission:
                        syncStatusText = "already synced" if syncResult.get("alreadySynced") else "synced now"
                        await self._logHonorGuardSheetChange(
                            reviewerId=interaction.user.id,
                            change="Edited Honor Guard points for an approved event submission.",
                            details=(
                                f"Host: <@{int(submission.get('targetUserId') or 0)}> | "
                                f"Participants: {len(allAttendees)-1} | "
                                f"Date: {submission.get('eventDate') or 'N/A'} | "
                                f"Archived: {syncResult.get('archiveSynced') or False} | "
                                f"Host Update: {syncResult.get('eventHostUpdate') or False} | "
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
                    await _safeInteractionReply(interaction, "Event submission approved.", ephemeral=True)
                elif status == "REJECTED":
                    await _safeInteractionReply(interaction, "Event submission rejected.", ephemeral=True)
                else:
                    await _safeInteractionReply(
                        interaction,
                        "Event submission status updated.",
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
                log.exception("Failed to process event submission review decision.")
                await _safeInteractionReply(
                    interaction,
                    f"Could not process this action: {exc}",
                    ephemeral=True,
                )

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="honorGuard_event_review:approve",
    )
    async def approveBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finishDecision(interaction, status="APPROVED", note=None)

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="honorGuard_event_review:reject",
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
        # TODO Implement Time Selection
        event = await self.cog._clockInEngine.getSession(self.eventId)
        await interaction.response.send_modal(HonorGuardEventFinishModal(self.cog, event))
        #await self.cog.openSubmitEvent(interaction, self.eventId, 60)

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        emoji=joinButtonEmoji,
        row=1,
        custom_id="honorguard_event:join",
    )
    async def joinBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventJoin(interaction, self.eventId)

class HonorGuardEventSubmitView(runtimeViewBases.OwnerLockedView):
    def __init__(self, cog: "HonorGuardCog", userId: int, eventId: int, guildId: int, attendees: list[dict]):
        super().__init__(openerId=userId, timeout=90)
        self.cog = cog
        self.eventId = int(eventId)
        self.guild = self.cog.bot.get_guild(int(guildId)) 
        self.attendees = attendees
        self.selectedUserId = 0
        self.userSelect = discord.ui.Select(
            placeholder="Select User",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.userSelect.callback = self.onUserSelected
        self.add_item(self.userSelect)

        self._refreshSectionOptions()
        self.quotaPointsBtn.disabled = True
        self.eventPointsBtn.disabled = True

    async def on_timeout(self) -> None:
        await self.cog.closeEventSubmit(self.eventId)

    def _refreshSectionOptions(self) -> None:
        options: list[discord.SelectOption] = []
        for attendee in self.attendees:
            label = _memberDisplayName(self.guild.get_member(attendee.get("userId")))
            default = str(attendee.get("userId")) == str(self.selectedUserId)
            options.append(
                discord.SelectOption(
                    label=label,
                    value=attendee.get("userId"),
                    description=attendee.get("participantRole") or "Attendee",
                    default=default,
                )
            )
        self.userSelect.options = options
        self.userSelect.placeholder = "Select attendees to edit"
        self.userSelect.disabled = False

    def _selectedValueFromInteraction(
        self,
        interaction: discord.Interaction,
        *,
        fallback: str = "",
    ) -> str:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        values = data.get("values")
        if isinstance(values, list) and values:
            first = str(values[0]).strip()
            if first:
                return first
        return str(fallback or "").strip()

    async def onUserSelected(self, interaction: discord.Interaction) -> None:
        self.selectedUserId = self._selectedValueFromInteraction(
            interaction,
        )
        await self._refreshMessage(interaction)

    async def _refreshMessage(self, interaction: discord.Interaction) -> None:
        self.selectedUser = next((a for a in self.attendees if str(a.get("userId")) == str(self.selectedUserId) and a.get("participantRole") != "HOST"), None)
        if not self.selectedUser:
            return
        self.quotaPointsBtn.disabled = False
        self.eventPointsBtn.disabled = False

        self._refreshSectionOptions()
        await self.updateMessage(interaction)

    async def updateMessage(self, interaction: discord.Interaction) -> None:
        self.attendees = await self.cog._clockInEngine.listAttendees(int(self.eventId))
        event = await self.cog._clockInEngine.getSession(self.eventId)
        embed = self.cog._clockInAdapter.buildSubmitEmbed(event, self.attendees)
        await runtimeViewBases.safeRefreshInteractionMessage(interaction, embed=embed, view=self)

    @discord.ui.button(
        label="Edit Quota Points",
        style=discord.ButtonStyle.primary,
        row=1,
        custom_id="honorguard_event_submit:quota_points",
    )
    async def quotaPointsBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            HonorGuardEventPointsModal(
                cog=self.cog,
                eventId=self.eventId,
                user=self.selectedUser,
                type="QUOTA",
                original_interaction=interaction,
                view=self,
            )
        )
    
    @discord.ui.button(
        label="Edit Event Points",
        style=discord.ButtonStyle.primary,
        row=1,
        custom_id="honorguard_event_submit:event_points",
    )
    async def eventPointsBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(
            interaction,
            HonorGuardEventPointsModal(
                cog=self.cog,
                eventId=self.eventId,
                user=self.selectedUser,
                type="EVENT",
                original_interaction=interaction,
                view=self,
            )
        )
    
    @discord.ui.button(
        label="Submit",
        style=discord.ButtonStyle.success,
        row=2,
        custom_id="honorguard_event_submit:submit",
    )
    async def submitBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handleEventSubmit(interaction, self.eventId)

class HonorGuardEventPointsModal(discord.ui.Modal, title="Edit Points"):
    pointsInput = discord.ui.TextInput(
        label="New Points",
        style=discord.TextStyle.short,
        required=True,
        max_length=5,
    )

    def __init__(self, cog: "HonorGuardCog", eventId: int, user: dict, type: str, original_interaction: discord.Interaction, view: discord.ui.View):
        super().__init__()
        self.cog = cog
        self.eventId = int(eventId)
        self.user = user
        self.original_interaction = original_interaction
        self.view = view
        if(type == "QUOTA"):
            self.pointsInput.default = str(user.get("quotaPoints", 0))
        else:
            self.pointsInput.default = str(user.get("promotionEventPoints", 0))
        self.type = type

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.pointsInput.value or "").strip()
        try:
            points = float(raw)
            if self.type == "QUOTA":
                if points % 0.5 != 0:
                    await interactionRuntime.safeInteractionReply(interaction, "Quota points must be in increments of 0.5.")
                    return
            else:
                if points % 1 != 0:
                    await interactionRuntime.safeInteractionReply(interaction, "Event points must be whole numbers.")
                    return
        except ValueError:
            await interactionRuntime.safeInteractionReply(interaction, "Points must be a number.")
            return
        await self.cog.handleEditPoints(interaction, self.eventId, self.user, points, self.type)
        await self.view.updateMessage(self.original_interaction)

class HonorGuardEventFinishModal(discord.ui.Modal, title="Finish Event"):
    # NOT AT USE ANYMORE
    durationMinutesInput = discord.ui.TextInput(
        label="Event duration (minutes)",
        style=discord.TextStyle.short,
        required=True,
        max_length=5,
        placeholder="Example: 45",
    )

    def __init__(self, cog: "HonorGuardCog", event: dict):
        super().__init__()
        self.cog = cog
        self.eventId = int(event.get("eventId"))
        startedAt: datetime = event.get("startedAt")
        finishedAt = datetime.now()
        duration = finishedAt - startedAt
        minutes = int(duration.total_seconds() // 60)
        self.durationMinutesInput.default = str(minutes)
        self.durationMinutesInput.placeholder = f"Default: {minutes}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.durationMinutesInput.value or "").strip()
        try:
            durationMinutes = int(raw)
        except ValueError:
            await interactionRuntime.safeInteractionReply(interaction, "Duration must be a whole number of minutes.", ephemeral=True)
            return
        if durationMinutes <= 0:
            await interactionRuntime.safeInteractionReply(interaction, "Duration must be greater than 0 minutes.", ephemeral=True)
            return
        await self.cog.openSubmitEvent(interaction, self.eventId, durationMinutes)

class HonorGuardEventManageView(runtimeViewBases.OwnerLockedView):
    def __init__(self, cog: "HonorGuardCog", eventId: int, guildId: int, userId: int, attendees: list[dict]):
        super().__init__(openerId=userId, timeout=60)
        self.cog = cog
        self.eventId = int(eventId)
        self.guild = self.cog.bot.get_guild(int(guildId))
        self.attendees = attendees
        self.selectedUserId = 0
        self.userSelect = discord.ui.Select(
            placeholder="Select User",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.userSelect.callback = self.onUserSelected
        self.add_item(self.userSelect)

        self._refreshSectionOptions()
        self.assignAttendeeBtn.disabled = True
        self.assignCohostsBtn.disabled = True
        self.assignSupervisorsBtn.disabled = True
        self.removeAttendeesBtn.disabled = True

    async def on_timeout(self) -> None:
        await self.cog.closeEventManage(self.eventId)

    def _refreshSectionOptions(self) -> None:
        options: list[discord.SelectOption] = []
        for attendee in self.attendees:
            if attendee.get("participantRole", "").upper() == "HOST":
                continue
            label = _memberDisplayName(self.guild.get_member(attendee.get("userId")))
            default = str(attendee.get("userId")) == str(self.selectedUserId)
            options.append(
                discord.SelectOption(
                    label=label, 
                    value=str(attendee.get("userId")),
                    description=attendee.get("participantRole") or "Attendee",
                    default=default,
                )
            )
        self.userSelect.options = options
        self.userSelect.placeholder = "Select attendee to manage"
        self.userSelect.disabled = False

    def _selectedValueFromInteraction(
        self,
        interaction: discord.Interaction,
        *,
        fallback: str = "",
    ) -> str:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        values = data.get("values")
        if isinstance(values, list) and values:
            first = str(values[0]).strip()
            if first:
                return first
        return str(fallback or "").strip()

    async def onUserSelected(self, interaction: discord.Interaction) -> None:
        self.selectedUserId = int(self._selectedValueFromInteraction(
            interaction,
        ))
        await self._refreshMessage(interaction)

    async def _refreshMessage(self, interaction: discord.Interaction) -> None:
        self.selectedUser = next((a for a in self.attendees if str(a.get("userId")) == str(self.selectedUserId) and a.get("participantRole") != "HOST"), None)
        if(not self.selectedUser):
            self.assignAttendeeBtn.disabled = True
            self.assignCohostsBtn.disabled = True
            self.assignSupervisorsBtn.disabled = True
            self.removeAttendeesBtn.disabled = True
        else:
            participantRole = self.selectedUser.get("participantRole") or "ATTENDEE"
            singleAttendee = len(self.attendees) == 1
            if participantRole.upper() == "ATTENDEE":
                self.assignAttendeeBtn.disabled = True
                self.assignCohostsBtn.disabled = False
                self.assignSupervisorsBtn.disabled = False
                self.removeAttendeesBtn.disabled = singleAttendee
            elif participantRole.upper() == "COHOST":
                self.assignAttendeeBtn.disabled = False
                self.assignCohostsBtn.disabled = True
                self.assignSupervisorsBtn.disabled = False
                self.removeAttendeesBtn.disabled = True
            elif participantRole.upper() == "SUPERVISOR":
                self.assignAttendeeBtn.disabled = False
                self.assignCohostsBtn.disabled = False
                self.assignSupervisorsBtn.disabled = True
                self.removeAttendeesBtn.disabled = True
            else:
                raise Exception(f"Unknown participant role: {participantRole}")

        self._refreshSectionOptions()
        await self.updateMessage(interaction)

    async def updateMessage(self, interaction: discord.Interaction) -> None:
        event = await self.cog._clockInEngine.getSession(self.eventId)
        embed = self.cog._clockInAdapter.buildEventManageEmbed(event, self.attendees)
        await runtimeViewBases.safeRefreshInteractionMessage(interaction, embed=embed, view=self)

    @discord.ui.button(
        label="Assign Attendee",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="honorguard_event_manage:assign_attendee",
    )
    async def assignAttendeeBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handleAssignAttendee(interaction, self.eventId, self.selectedUser)
        self.attendees = await self.cog._clockInEngine.listAttendees(int(self.eventId))
        await self._refreshMessage(interaction)

    @discord.ui.button(
        label="Assign Cohost",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="honorguard_event_manage:assign_cohosts",
    )
    async def assignCohostsBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handleAssignCohost(interaction, self.eventId, self.selectedUser)
        self.attendees = await self.cog._clockInEngine.listAttendees(int(self.eventId))
        await self._refreshMessage(interaction)

    @discord.ui.button(
        label="Assign Supervisor",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="honorguard_event_manage:assign_supervisors",
    )
    async def assignSupervisorsBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handleAssignSupervisor(interaction, self.eventId, self.selectedUser)
        self.attendees = await self.cog._clockInEngine.listAttendees(int(self.eventId))
        await self._refreshMessage(interaction)

    @discord.ui.button(
        label="Remove Attendee",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="honorguard_event_manage:remove_attendees",
    )
    async def removeAttendeesBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handleRemoveAttendee(interaction, self.eventId, self.selectedUser)
        self.attendees = await self.cog._clockInEngine.listAttendees(int(self.eventId))
        self.selectedUserId = 0
        await self._refreshMessage(interaction)

    @discord.ui.button(
        label="Done",
        style=discord.ButtonStyle.primary,
        row=2,
        custom_id="honorguard_event_manage:done",
    )
    async def doneBtn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.closeEventManage(self.eventId)