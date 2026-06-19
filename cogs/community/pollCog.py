from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.community.polls import (
    PollView,
    buildPollEmbed,
    closePoll,
    createPoll,
    getPoll,
    listOpenPolls,
    listPollVotes,
    normalizePollOptions,
    parseRoleGateIds,
    parsePollOptions,
    setPollMessageId,
    setPollVotes,
)
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions

log = logging.getLogger(__name__)


def _parseRoleIdsText(raw: str) -> list[int]:
    roleIds: list[int] = []
    for token in str(raw or "").replace("\n", " ").replace(",", " ").split():
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            continue
        parsed = int(digits)
        if parsed > 0 and parsed not in roleIds:
            roleIds.append(parsed)
    return roleIds


class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._closeTask: asyncio.Task | None = None

    async def cog_load(self) -> None:
        await self._restorePollViews()
        if self._closeTask is None or self._closeTask.done():
            self._closeTask = asyncio.create_task(self._runPollCloseLoop())

    def cog_unload(self) -> None:
        if self._closeTask is not None and not self._closeTask.done():
            self._closeTask.cancel()
        self._closeTask = None

    async def _safeEphemeral(self, interaction: discord.Interaction, content: str) -> None:
        await interactionRuntime.safeInteractionReply(interaction, content=content, ephemeral=True)

    def _allowedCreatorRoleIds(self) -> set[int]:
        roleIds: set[int] = set()
        for raw in (
            getattr(config, "middleRankRoleId", None),
            getattr(config, "highRankRoleId", None),
        ):
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                roleIds.add(parsed)
        return roleIds

    def _canCreatePoll(self, member: discord.Member) -> bool:
        if runtimePermissions.hasAdminOrManageGuild(member):
            return True
        allowedRoleIds = self._allowedCreatorRoleIds()
        if not allowedRoleIds:
            return False
        return any(int(role.id) in allowedRoleIds for role in member.roles)

    async def _getMessageChannel(self, channelId: int) -> discord.TextChannel | discord.Thread | None:
        if int(channelId) <= 0:
            return None
        channel = self.bot.get_channel(int(channelId))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channelId))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def _canManagePoll(self, member: discord.Member, pollRow: dict) -> bool:
        if runtimePermissions.hasAdminOrManageGuild(member):
            return True
        return int(member.id) == int(pollRow.get("creatorId") or 0)

    def _parsePollCloseAt(self, pollRow: dict) -> datetime | None:
        raw = str(pollRow.get("closesAt") or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _collectPollOptions(self, *values: str | None) -> list[str]:
        return normalizePollOptions([str(value or "").strip() for value in values if str(value or "").strip()])

    def _buildPollView(self, pollRow: dict) -> PollView:
        status = str(pollRow.get("status") or "OPEN").strip().upper()
        return PollView(cog=self, pollRow=pollRow, closed=status != "OPEN")

    def _normalizeGuildRoleIds(self, guild: discord.Guild, roleIds: list[int]) -> list[int]:
        out: list[int] = []
        for roleId in roleIds:
            if guild.get_role(int(roleId)) is None:
                continue
            if int(roleId) not in out:
                out.append(int(roleId))
        return out

    async def _restorePollViews(self) -> int:
        restored = 0
        for pollRow in await listOpenPolls():
            messageId = int(pollRow.get("messageId") or 0)
            if messageId <= 0:
                continue
            self.bot.add_view(self._buildPollView(pollRow), message_id=messageId)
            restored += 1
        return restored

    async def _messageResults(self, pollRow: dict) -> None:
        pollId = int(pollRow.get("pollId") or 0)
        voteRows = await listPollVotes(pollId)
        creatorId = int(pollRow.get("creatorId") or 0)
        try:
            user = await self.bot.fetch_user(creatorId)
            if user:
                await user.send(
                    embed=buildPollEmbed(pollRow, voteRows, resultsForCreator=True),
                    view=self._buildPollView(pollRow)
                )
            else:
                log.warning(f"Creator of poll {pollId} not found")
        except discord.Forbidden:
            log.warning("Cannot message poll results: user has blocked the bot or has DMs disabled.")
        except Exception as e:
            log.warning(f"Unable to message poll results: {e}")
            
    async def _refreshPollMessage(
        self,
        pollRow: dict,
        *,
        message: discord.Message | None = None,
    ) -> None:
        pollId = int(pollRow.get("pollId") or 0)
        voteRows = await listPollVotes(pollId)
        targetMessage = message
        if targetMessage is None:
            channel = await self._getMessageChannel(int(pollRow.get("channelId") or 0))
            if channel is None:
                return
            try:
                targetMessage = await channel.fetch_message(int(pollRow.get("messageId") or 0))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        await interactionRuntime.safeMessageEdit(
            targetMessage,
            embed=buildPollEmbed(pollRow, voteRows),
            view=self._buildPollView(pollRow),
        )

    async def _runPollCloseLoop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        while True:
            try:
                await self._closeDuePollsTick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Poll close loop failed.")
            await asyncio.sleep(60)

    async def _closeDuePollsTick(self) -> None:
        now = datetime.now(timezone.utc)
        for pollRow in await listOpenPolls():
            closesAt = self._parsePollCloseAt(pollRow)
            if closesAt is None or closesAt > now:
                continue
            await closePoll(int(pollRow.get("pollId") or 0))
            refreshed = await getPoll(int(pollRow.get("pollId") or 0))
            if refreshed is not None:
                await self._refreshPollMessage(refreshed)

    @app_commands.command(name="poll", description="Create a new poll.")
    @app_commands.rename(duration_minutes="duration-minutes")
    @app_commands.rename(option_1="option-1")
    @app_commands.rename(option_2="option-2")
    @app_commands.rename(option_3="option-3")
    @app_commands.rename(option_4="option-4")
    @app_commands.rename(option_5="option-5")
    @app_commands.rename(option_6="option-6")
    @app_commands.rename(option_7="option-7")
    @app_commands.rename(option_8="option-8")
    @app_commands.rename(multi_select="multi-select")
    @app_commands.rename(role_gate_ids="role-gate-ids")
    @app_commands.rename(hide_results_until_closed="hide-results-until-closed")
    @app_commands.rename(message_results_to_creator="message-results-to-creator")
    @app_commands.describe(
        question="The poll question people will vote on.",
        option_1="First answer choice.",
        option_2="Second answer choice.",
        option_3="Optional third answer choice.",
        option_4="Optional fourth answer choice.",
        option_5="Optional fifth answer choice.",
        option_6="Optional sixth answer choice.",
        option_7="Optional seventh answer choice.",
        option_8="Optional eighth answer choice.",
        duration_minutes="How long the poll should stay open. Leave at 0 for no auto-close.",
        anonymous="Hide voter names from the poll results.",
        multi_select="Allow people to choose more than one option.",
        role_gate_ids="Optional role mentions or role IDs required to vote.",
        hide_results_until_closed="Hide vote counts until the poll is closed.",
        message_results_to_creator="Message The results to the creator of the poll instead of releasing them."
    )
    async def pollCommand(
        self,
        interaction: discord.Interaction,
        question: str,
        option_1: str,
        option_2: str,
        option_3: str | None = None,
        option_4: str | None = None,
        option_5: str | None = None,
        option_6: str | None = None,
        option_7: str | None = None,
        option_8: str | None = None,
        duration_minutes: app_commands.Range[int, 0, 10080] = 0,
        anonymous: bool = False,
        multi_select: bool = False,
        role_gate_ids: str | None = None,
        hide_results_until_closed: bool = False,
        message_results_to_creator: bool = False,
    ) -> None:
        if not interaction.guild or not interaction.channel or not isinstance(interaction.user, discord.Member):
            await self._safeEphemeral(interaction, "This command can only be used in a server channel.")
            return
        if not self._canCreatePoll(interaction.user):
            await self._safeEphemeral(interaction, "MR/HR roles or administrator/manage-server required.")
            return

        pollOptions = self._collectPollOptions(
            option_1,
            option_2,
            option_3,
            option_4,
            option_5,
            option_6,
            option_7,
            option_8,
        )
        if len(pollOptions) < 2:
            await self._safeEphemeral(interaction, "Please provide at least 2 poll options.")
            return
        if len(pollOptions) > 8:
            await self._safeEphemeral(interaction, "Please keep polls to 8 options or fewer.")
            return

        closesAtIso = None
        if int(duration_minutes or 0) > 0:
            closesAtIso = (datetime.now(timezone.utc) + timedelta(minutes=int(duration_minutes))).isoformat()

        roleGateIds = self._normalizeGuildRoleIds(interaction.guild, _parseRoleIdsText(str(role_gate_ids or "")))

        pollId = await createPoll(
            guildId=int(interaction.guild.id),
            channelId=int(interaction.channel.id),
            creatorId=int(interaction.user.id),
            question=str(question or "").strip(),
            options=pollOptions,
            anonymous=bool(anonymous),
            multiSelect=bool(multi_select),
            roleGateIds=roleGateIds,
            hideResultsUntilClosed=bool(hide_results_until_closed),
            messageResultsToCreator=bool(message_results_to_creator),
            closesAtIso=closesAtIso,
        )
        pollRow = await getPoll(pollId)
        if pollRow is None:
            await self._safeEphemeral(interaction, "Poll creation failed.")
            return

        sentMessage = await interaction.channel.send(
            embed=buildPollEmbed(pollRow, []),
            view=self._buildPollView(pollRow),
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
        await setPollMessageId(pollId, int(sentMessage.id))
        self.bot.add_view(self._buildPollView({**pollRow, "messageId": int(sentMessage.id)}), message_id=int(sentMessage.id))
        await self._safeEphemeral(interaction, f"Poll #{pollId} created.")

    async def handlePollVoteSelection(
        self,
        interaction: discord.Interaction,
        *,
        pollId: int,
        optionIndexes: list[int],
    ) -> None:
        pollRow = await getPoll(pollId)
        if pollRow is None:
            await self._safeEphemeral(interaction, "Poll not found.")
            return
        if str(pollRow.get("status") or "OPEN").strip().upper() != "OPEN":
            await self._safeEphemeral(interaction, "This poll is closed.")
            return
        options = parsePollOptions(pollRow)
        normalizedIndexes = sorted({int(value) for value in optionIndexes if int(value) >= 0})
        if not normalizedIndexes:
            await self._safeEphemeral(interaction, "That poll option is no longer valid.")
            return
        multiSelect = bool(int(pollRow.get("multiSelect") or 0))
        if not multiSelect and len(normalizedIndexes) > 1:
            await self._safeEphemeral(interaction, "This poll only allows a single choice.")
            return
        if any(value >= len(options) for value in normalizedIndexes):
            await self._safeEphemeral(interaction, "That poll option is no longer valid.")
            return
        requiredRoleIds = parseRoleGateIds(pollRow)
        if requiredRoleIds and isinstance(interaction.user, discord.Member):
            hasRequiredRole = any(int(role.id) in requiredRoleIds for role in interaction.user.roles)
            if not hasRequiredRole:
                await self._safeEphemeral(interaction, "You do not have one of the roles required to vote in this poll.")
                return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=False)
        await setPollVotes(int(pollRow.get("pollId") or 0), int(interaction.user.id), normalizedIndexes)
        refreshed = await getPoll(pollId)
        if refreshed is None:
            return
        await self._refreshPollMessage(refreshed, message=interaction.message)
        selectedLabels = ", ".join(options[index] for index in normalizedIndexes)
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=f"Vote saved: {selectedLabels}",
            ephemeral=True,
        )

    async def refreshPollFromInteraction(self, interaction: discord.Interaction, *, pollId: int) -> None:
        pollRow = await getPoll(pollId)
        if pollRow is None:
            await self._safeEphemeral(interaction, "Poll not found.")
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=False)
        await self._refreshPollMessage(pollRow, message=interaction.message)

    async def closePollFromInteraction(self, interaction: discord.Interaction, *, pollId: int) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await self._safeEphemeral(interaction, "This action can only be used in a server.")
            return
        pollRow = await getPoll(pollId)
        if pollRow is None:
            await self._safeEphemeral(interaction, "Poll not found.")
            return
        if not self._canManagePoll(interaction.user, pollRow):
            await self._safeEphemeral(interaction, "Only the poll creator or server administrators can close this poll.")
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=False)
        await closePoll(pollId)
        refreshed = await getPoll(pollId)
        if refreshed is not None:
            await self._refreshPollMessage(refreshed, message=interaction.message)
            await self._messageResults(refreshed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PollCog(bot))
