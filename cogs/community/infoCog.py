from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from features.community.info import (
    buildServerInfoEmbed,
    buildServerStatsEmbed,
    buildUserInfoEmbed,
    captureGuildSnapshot,
    getLatestSnapshot,
    getLatestSnapshotBefore,
    incrementChannelMessageCount,
    incrementJoinCount,
    incrementLeaveCount,
    listChannelActivitySince,
    listMemberActivitySince,
    pruneOldActivity,
    pruneOldSnapshots,
)
from runtime import interaction as interactionRuntime
from runtime import webhooks as webhookRuntime

log = logging.getLogger(__name__)


class InfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._snapshotTask: asyncio.Task | None = None

    async def cog_load(self) -> None:
        if self._snapshotTask is None or self._snapshotTask.done():
            self._snapshotTask = asyncio.create_task(self._runSnapshotLoop())

    def cog_unload(self) -> None:
        if self._snapshotTask is not None and not self._snapshotTask.done():
            self._snapshotTask.cancel()
        self._snapshotTask = None

    async def _safeEphemeral(self, interaction: discord.Interaction, content: str) -> None:
        await interactionRuntime.safeInteractionReply(interaction, content=content, ephemeral=True)

    async def _captureAllGuildStats(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await captureGuildSnapshot(guild)
            except Exception:
                log.exception("Failed capturing stats snapshot for guild %s.", guild.id)
        await pruneOldSnapshots()
        await pruneOldActivity()

    async def _runSnapshotLoop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._captureAllGuildStats()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Guild stats snapshot loop failed.")
            await asyncio.sleep(21600)

    # Legacy slash handler kept for future reuse.
    async def userInfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await self._safeEphemeral(interaction, "This command can only be used in a server.")
            return
        target = member or interaction.user
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=buildUserInfoEmbed(target),
            ephemeral=False,
        )

    # Legacy slash handler kept for future reuse.
    async def serverInfo(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.channel:
            await self._safeEphemeral(interaction, "This command can only be used in a server.")
            return
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=False)
        embed = buildServerInfoEmbed(interaction.guild)
        avatarUrl = interaction.guild.icon.url if interaction.guild.icon else None
        sentViaWebhook = await webhookRuntime.sendOwnedWebhookMessage(
            botClient=self.bot,
            channel=interaction.channel,
            webhookName="Jane Anonymous Server Info",
            embed=embed,
            username="Server Info",
            avatarUrl=avatarUrl,
            reason="Anonymous server info lookup",
        )
        if sentViaWebhook:
            await self._safeEphemeral(interaction, "Server info posted.")
            return
        await interactionRuntime.safeInteractionReply(interaction, embed=embed, ephemeral=False)

    # Legacy slash handler kept for future reuse.
    async def serverStats(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await self._safeEphemeral(interaction, "This command can only be used in a server.")
            return
        now = datetime.now(timezone.utc)
        latestSnapshot = await getLatestSnapshot(int(interaction.guild.id))
        if latestSnapshot is None:
            await captureGuildSnapshot(interaction.guild)
            latestSnapshot = await getLatestSnapshot(int(interaction.guild.id))
        daySnapshot = await getLatestSnapshotBefore(int(interaction.guild.id), (now - timedelta(days=1)).isoformat())
        weekSnapshot = await getLatestSnapshotBefore(int(interaction.guild.id), (now - timedelta(days=7)).isoformat())
        memberActivityRows = await listMemberActivitySince(int(interaction.guild.id), now - timedelta(days=7))
        channelActivityRows = await listChannelActivitySince(int(interaction.guild.id), now - timedelta(days=7), limit=5)
        await interactionRuntime.safeInteractionReply(
            interaction,
            embed=buildServerStatsEmbed(
                interaction.guild,
                latestSnapshot=latestSnapshot,
                daySnapshot=daySnapshot,
                weekSnapshot=weekSnapshot,
                memberActivityRows=memberActivityRows,
                channelActivityRows=channelActivityRows,
            ),
            ephemeral=False,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            await incrementChannelMessageCount(int(message.guild.id), int(message.channel.id))
        except Exception:
            log.exception("Failed tracking channel message stats for guild %s.", message.guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await incrementJoinCount(int(member.guild.id))
        except Exception:
            log.exception("Failed tracking member join for guild %s.", member.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await incrementLeaveCount(int(member.guild.id))
        except Exception:
            log.exception("Failed tracking member leave for guild %s.", member.guild.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InfoCog(bot))
