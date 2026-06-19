from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from features.community.identity import service as janeIdentity
from features.staff.sessions.Roblox import robloxGroups, robloxTransport, robloxUsers
from runtime import interaction as interactionRuntime
from runtime import taskBudgeter
from runtime import viewBases as runtimeViewBases
from runtime import webhooks as runtimeWebhooks

log = logging.getLogger(__name__)
_idPattern = re.compile(r"\d{4,}")


def _positiveInt(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _extractId(value: object) -> int:
    text = str(value or "").strip()
    match = _idPattern.search(text)
    if not match:
        return _positiveInt(text)
    return _positiveInt(match.group(0))


def _parseBool(value: object, default: bool = True) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return bool(default)
    return text in {"1", "true", "yes", "y", "on", "remove"}


class VerifyLinkView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=600)
        self.add_item(discord.ui.Button(label="Link Roblox Account", url=url))


class RobloxProfileView(discord.ui.View):
    def __init__(self, robloxUserId: int):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Open Roblox profile",
                url=f"https://www.roblox.com/users/{int(robloxUserId)}/profile",
            )
        )


class RoverHubModal(discord.ui.Modal):
    def __init__(self, *, hubView: "RoverHubView", title: str):
        super().__init__(title=title)
        self.hubView = hubView


class ConnectGroupModal(RoverHubModal):
    groupId = discord.ui.TextInput(label="Roblox Group ID", placeholder="36000077", required=True)
    groupName = discord.ui.TextInput(label="Label", placeholder="Optional display name", required=False, max_length=120)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        groupId = _extractId(self.groupId.value)
        if groupId <= 0:
            return await self.hubView.cog.safeEphemeral(interaction, "Enter a valid Roblox group ID.")
        await janeIdentity.connectGroup(
            int(interaction.guild_id or 0),
            groupId,
            str(self.groupName.value or "").strip(),
        )
        await self.hubView.refresh(interaction, noticeText=f"Connected Roblox group `{groupId}`.")


class RemoveGroupModal(RoverHubModal):
    groupId = discord.ui.TextInput(label="Roblox Group ID", placeholder="36000077", required=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        groupId = _extractId(self.groupId.value)
        if groupId <= 0:
            return await self.hubView.cog.safeEphemeral(interaction, "Enter a valid Roblox group ID.")
        await janeIdentity.removeGroup(int(interaction.guild_id or 0), groupId)
        await self.hubView.refresh(interaction, noticeText=f"Removed Roblox group `{groupId}` and its rules.")


class RemoveRuleModal(RoverHubModal):
    def __init__(self, *, hubView: "RoverHubView", ruleKind: str):
        super().__init__(hubView=hubView, title=f"Remove {ruleKind.title()} Rule")
        self.ruleKind = ruleKind
        self.ruleId = discord.ui.TextInput(label="Rule ID", placeholder="Shown in the hub", required=True)
        self.add_item(self.ruleId)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ruleId = _extractId(self.ruleId.value)
        if ruleId <= 0:
            return await self.hubView.cog.safeEphemeral(interaction, "Enter a valid rule ID.")
        if self.ruleKind == "role":
            await janeIdentity.removeRoleRule(int(interaction.guild_id or 0), ruleId)
        else:
            await janeIdentity.removeNameRule(int(interaction.guild_id or 0), ruleId)
        await self.hubView.refresh(interaction, noticeText=f"Removed {self.ruleKind} rule `#{ruleId}`.")


class AddRoleRuleByNumberModal(RoverHubModal):
    rankNumber = discord.ui.TextInput(label="Rank Number", placeholder="Number shown in the role rules panel", required=True)
    roleId = discord.ui.TextInput(label="Discord Role", placeholder="@Role or role ID", required=True)
    removeWhenUnmatched = discord.ui.TextInput(label="Remove When No Longer Matches", placeholder="yes", required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rankNumber = _positiveInt(self.rankNumber.value)
        roleId = _extractId(self.roleId.value)
        selection = self.hubView.choiceForNumber(rankNumber)
        if selection is None:
            return await self.hubView.cog.safeEphemeral(interaction, "That rank number is not in this panel.")
        role = interaction.guild.get_role(roleId) if interaction.guild is not None else None
        if role is None:
            return await self.hubView.cog.safeEphemeral(interaction, "That Discord role was not found in this server.")
        rank = _positiveInt(selection.get("rank"))
        groupId = _positiveInt(selection.get("groupId"))
        ruleId = await janeIdentity.addRoleRule(
            guildId=int(interaction.guild_id or 0),
            groupId=groupId,
            minRank=rank,
            maxRank=rank,
            roleId=roleId,
            removeWhenUnmatched=_parseBool(self.removeWhenUnmatched.value, True),
        )
        rankName = str(selection.get("rankName") or f"Rank {rank}").strip()
        await self.hubView.refresh(
            interaction,
            noticeText=f"Added role rule `#{ruleId}`: `{rankName}` -> {role.mention}.",
        )


class SetUnverifiedRoleModal(RoverHubModal):
    roleId = discord.ui.TextInput(label="Unverified Discord Role", placeholder="@Unverified or role ID", required=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        roleId = _extractId(self.roleId.value)
        if roleId <= 0:
            return await self.hubView.cog.safeEphemeral(interaction, "Enter a valid Discord role.")
        role = interaction.guild.get_role(roleId) if interaction.guild is not None else None
        if role is None:
            return await self.hubView.cog.safeEphemeral(interaction, "That role was not found in this server.")
        await janeIdentity.setUnverifiedRole(int(interaction.guild_id or 0), roleId)
        await self.hubView.refresh(interaction, noticeText=f"Set unverified role to {role.mention}.")


class AddNameRuleByNumberModal(RoverHubModal):
    rankNumber = discord.ui.TextInput(label="Rank Number", placeholder="Number shown in the name rules panel", required=True)
    prefix = discord.ui.TextInput(label="Nickname Prefix", placeholder="[HR]", required=False, max_length=24)
    suffix = discord.ui.TextInput(label="Nickname Suffix", placeholder="[ANRO]", required=False, max_length=24)
    priority = discord.ui.TextInput(label="Priority", placeholder="0", required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rankNumber = _positiveInt(self.rankNumber.value)
        selection = self.hubView.choiceForNumber(rankNumber)
        if selection is None:
            return await self.hubView.cog.safeEphemeral(interaction, "That rank number is not in this panel.")
        rank = _positiveInt(selection.get("rank"))
        groupId = _positiveInt(selection.get("groupId"))
        ruleId = await janeIdentity.addNameRule(
            guildId=int(interaction.guild_id or 0),
            groupId=groupId,
            minRank=rank,
            maxRank=rank,
            prefix=str(self.prefix.value or "").strip(),
            suffix=str(self.suffix.value or "").strip(),
            priority=_positiveInt(self.priority.value, 0),
        )
        rankName = str(selection.get("rankName") or f"Rank {rank}").strip()
        await self.hubView.refresh(
            interaction,
            noticeText=f"Added nickname rule `#{ruleId}` for `{rankName}`.",
        )


class GroupRankSelect(discord.ui.Select):
    def __init__(
        self,
        panel: "GroupRankPanelView",
        groups: list[dict[str, Any]],
        selectedGroupId: int,
        label: str,
        noticeText: str,
    ):
        self.panel = panel
        self.noticeText = str(noticeText or "")
        options: list[discord.SelectOption] = []
        validGroups = [group for group in groups if _positiveInt(group.get("groupId")) > 0]
        for group in validGroups[:25]:
            groupId = _positiveInt(group.get("groupId"))
            groupLabel = str(group.get("groupName") or f"Group {groupId}").strip()[:100]
            options.append(
                discord.SelectOption(
                    label=groupLabel,
                    value=str(groupId),
                    description=f"Roblox group {groupId}"[:100],
                    default=groupId == selectedGroupId,
                )
            )
        super().__init__(
            placeholder=str(label or "Select Roblox group")[:150],
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.panel.cog.ensureAdmin(interaction):
            return
        selectedGroupId = _positiveInt(self.values[0] if self.values else 0)
        await self.panel.refresh(
            interaction,
            noticeText=self.noticeText,
            selectedGroupId=selectedGroupId,
        )


class GroupRankPanelView(runtimeViewBases.OwnerLockedView):
    def __init__(
        self,
        *,
        cog: "IdentityCog",
        openerId: int,
        groups: list[dict[str, Any]],
        selectedGroupId: int,
        rankChoices: list[dict[str, Any]],
        noticeText: str = "",
        ownerMessage: str,
        selectPlaceholder: str,
        selectNoticeText: str,
    ):
        super().__init__(
            openerId=openerId,
            timeout=900,
            ownerMessage=ownerMessage,
        )
        self.cog = cog
        self.groups = list(groups or [])
        self.selectedGroupId = _positiveInt(selectedGroupId)
        self.rankChoices = list(rankChoices or [])
        self.noticeText = str(noticeText or "")
        self.selectPlaceholder = str(selectPlaceholder or "Select Roblox group")
        self.selectNoticeText = str(selectNoticeText or "")
        if any(_positiveInt(group.get("groupId")) > 0 for group in self.groups):
            self.add_item(
                GroupRankSelect(
                    self,
                    self.groups,
                    self.selectedGroupId,
                    label=self.selectPlaceholder,
                    noticeText=self.selectNoticeText,
                )
            )

    def choiceForNumber(self, number: int) -> dict[str, Any] | None:
        safeNumber = _positiveInt(number)
        for choice in self.rankChoices:
            if _positiveInt(choice.get("number")) == safeNumber:
                return choice
        return None

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        noticeText: str | None = None,
        selectedGroupId: int | None = None,
    ) -> None:
        if noticeText is not None:
            self.noticeText = str(noticeText or "")
        if selectedGroupId is not None:
            self.selectedGroupId = _positiveInt(selectedGroupId)
        await self.refreshPanelMessage(
            interaction=interaction,
            openerId=self.openerId,
            selectedGroupId=self.selectedGroupId,
            noticeText=self.noticeText,
        )

    async def refreshPanelMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        selectedGroupId: int,
        noticeText: str,
    ) -> None:
        raise NotImplementedError


class RoleRulesPanelView(GroupRankPanelView):
    def __init__(
        self,
        *,
        cog: "IdentityCog",
        openerId: int,
        groups: list[dict[str, Any]],
        selectedGroupId: int,
        rankChoices: list[dict[str, Any]],
        noticeText: str = "",
    ):
        super().__init__(
            cog=cog,
            openerId=openerId,
            groups=groups,
            selectedGroupId=selectedGroupId,
            rankChoices=rankChoices,
            noticeText=noticeText,
            ownerMessage="This role-rules panel belongs to someone else.",
            selectPlaceholder="Select Roblox group for role rules",
            selectNoticeText="Role rules group changed.",
        )

    async def refreshPanelMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        selectedGroupId: int,
        noticeText: str,
    ) -> None:
        await self.cog.refreshRoleRulesPanelMessage(
            interaction=interaction,
            openerId=openerId,
            selectedGroupId=selectedGroupId,
            noticeText=noticeText,
        )

    @discord.ui.button(label="Add Role Rule", style=discord.ButtonStyle.primary, row=1)
    async def addRoleRuleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(AddRoleRuleByNumberModal(hubView=self, title="Add Role Rule"))

    @discord.ui.button(label="Remove Role Rule", style=discord.ButtonStyle.danger, row=1)
    async def removeRoleRuleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(RemoveRuleModal(hubView=self, ruleKind="role"))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refreshBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await self.refresh(interaction, noticeText="Role rules refreshed.")


class NameRulesPanelView(GroupRankPanelView):
    def __init__(
        self,
        *,
        cog: "IdentityCog",
        openerId: int,
        groups: list[dict[str, Any]],
        selectedGroupId: int,
        rankChoices: list[dict[str, Any]],
        noticeText: str = "",
    ):
        super().__init__(
            cog=cog,
            openerId=openerId,
            groups=groups,
            selectedGroupId=selectedGroupId,
            rankChoices=rankChoices,
            noticeText=noticeText,
            ownerMessage="This name-rules panel belongs to someone else.",
            selectPlaceholder="Select Roblox group for name rules",
            selectNoticeText="Name rules group changed.",
        )

    async def refreshPanelMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        selectedGroupId: int,
        noticeText: str,
    ) -> None:
        await self.cog.refreshNameRulesPanelMessage(
            interaction=interaction,
            openerId=openerId,
            selectedGroupId=selectedGroupId,
            noticeText=noticeText,
        )

    @discord.ui.button(label="Add Name Rule", style=discord.ButtonStyle.primary, row=1)
    async def addNameRuleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(AddNameRuleByNumberModal(hubView=self, title="Add Name Rule"))

    @discord.ui.button(label="Remove Name Rule", style=discord.ButtonStyle.danger, row=1)
    async def removeNameRuleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(RemoveRuleModal(hubView=self, ruleKind="name"))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refreshBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await self.refresh(interaction, noticeText="Name rules refreshed.")


class RoverHubView(runtimeViewBases.OwnerLockedView):
    def __init__(self, *, cog: "IdentityCog", openerId: int, noticeText: str = ""):
        super().__init__(
            openerId=openerId,
            timeout=900,
            ownerMessage="This Rover hub belongs to someone else.",
        )
        self.cog = cog
        self.noticeText = str(noticeText or "")

    async def refresh(self, interaction: discord.Interaction, *, noticeText: str | None = None) -> None:
        if noticeText is not None:
            self.noticeText = str(noticeText or "")
        await self.cog.refreshRoverHubMessage(
            interaction=interaction,
            openerId=self.openerId,
            noticeText=self.noticeText,
        )

    @discord.ui.button(label="Connect Group", style=discord.ButtonStyle.primary, row=0)
    async def connectGroupBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(ConnectGroupModal(hubView=self, title="Connect Roblox Group"))

    @discord.ui.button(label="Remove Group", style=discord.ButtonStyle.danger, row=0)
    async def removeGroupBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(RemoveGroupModal(hubView=self, title="Remove Roblox Group"))

    @discord.ui.button(label="Role Rules", style=discord.ButtonStyle.primary, row=1)
    async def roleRulesBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await self.cog.openRoleRulesPanel(interaction)

    @discord.ui.button(label="Name Rules", style=discord.ButtonStyle.primary, row=2)
    async def nameRulesBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await self.cog.openNameRulesPanel(interaction)

    @discord.ui.button(label="Set Unverified Role", style=discord.ButtonStyle.primary, row=3)
    async def setUnverifiedRoleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await interaction.response.send_modal(SetUnverifiedRoleModal(hubView=self, title="Set Unverified Role"))

    @discord.ui.button(label="Clear Unverified Role", style=discord.ButtonStyle.secondary, row=3)
    async def clearUnverifiedRoleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await janeIdentity.clearUnverifiedRole(int(interaction.guild_id or 0))
        await self.refresh(interaction, noticeText="Cleared the unverified role.")

    @discord.ui.button(label="Bulk Update", style=discord.ButtonStyle.primary, row=4)
    async def bulkUpdateBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.startBulkUpdateFromInteraction(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=4)
    async def refreshBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.cog.ensureAdmin(interaction):
            return
        await self.refresh(interaction, noticeText="Rover hub refreshed.")


class IdentityCog(commands.Cog):
    whoisGroup = app_commands.Group(
        name="whois",
        description="Look up Jane Identity Roblox verification info.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._bulkUpdateGuilds: set[int] = set()
        self._backgroundTasks: set[asyncio.Task[Any]] = set()
        self._scheduledRefreshTask: asyncio.Task[Any] | None = None
        self._relayPollTask: asyncio.Task[Any] | None = None
        self._relayLastProblem = ""

    async def cog_load(self) -> None:
        if self._scheduledRefreshTask is None or self._scheduledRefreshTask.done():
            self._scheduledRefreshTask = asyncio.create_task(
                self._scheduledRefreshLoop(),
                name="jane-identity-scheduled-refresh",
            )
        if self._relayPollTask is None or self._relayPollTask.done():
            self._relayPollTask = asyncio.create_task(
                self._relayPollLoop(),
                name="jane-identity-relay-poll",
            )

    def cog_unload(self) -> None:
        if self._scheduledRefreshTask is not None and not self._scheduledRefreshTask.done():
            self._scheduledRefreshTask.cancel()
        self._scheduledRefreshTask = None
        if self._relayPollTask is not None and not self._relayPollTask.done():
            self._relayPollTask.cancel()
        self._relayPollTask = None
        for task in list(self._backgroundTasks):
            if not task.done():
                task.cancel()
        self._backgroundTasks.clear()

    async def safeEphemeral(self, interaction: discord.Interaction, message: str) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=message,
            ephemeral=True,
        )

    async def _safeReply(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        view: discord.ui.View | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=content,
            embed=embed,
            view=view,
            ephemeral=True,
            allowedMentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )

    @staticmethod
    def _shortText(value: object, limit: int = 1000) -> str:
        text = " ".join(str(value or "").strip().split())
        if not text:
            return "Jane could not finish this verification attempt."
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)].rstrip()}..."

    async def _resolveDiscordUser(self, discordUserId: int) -> discord.User | None:
        safeDiscordUserId = _positiveInt(discordUserId)
        if safeDiscordUserId <= 0:
            return None
        user = self.bot.get_user(safeDiscordUserId)
        if user is not None:
            return user
        try:
            return await taskBudgeter.runDiscord(lambda: self.bot.fetch_user(safeDiscordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.info("Could not resolve Discord user %s for Jane Identity relay DM.", safeDiscordUserId)
            return None

    async def _sendVerificationFailureDm(
        self,
        result: janeIdentity.IdentityLinkResult,
        *,
        cancelled: bool = False,
    ) -> None:
        user = await self._resolveDiscordUser(result.discord_user_id)
        if user is None:
            return

        title = "Jane Identity Verification Cancelled" if cancelled else "Jane Identity Verification Failed"
        description = (
            "Roblox authorization was cancelled before Jane could link your account."
            if cancelled
            else "Jane could not finish linking your Roblox account."
        )
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.orange() if cancelled else discord.Color.red(),
        )
        embed.add_field(name="What happened", value=self._shortText(result.error), inline=False)
        guild = self.bot.get_guild(_positiveInt(result.guild_id))
        if guild is not None:
            embed.add_field(name="Server", value=str(guild.name), inline=False)
        embed.add_field(
            name="Next step",
            value="Run `/verify` again in Discord when you are ready to try again.",
            inline=False,
        )
        embed.set_footer(text="Jane Identity")

        try:
            await taskBudgeter.runDiscord(
                lambda: user.send(
                    content="Jane could not finish your Roblox verification.",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM Jane Identity relay failure to Discord user %s.", result.discord_user_id)

    async def _sendVerificationSuccessDm(
        self,
        result: janeIdentity.IdentityLinkResult,
        changes: dict[str, Any],
    ) -> None:
        user = await self._resolveDiscordUser(result.discord_user_id)
        if user is None:
            return

        guild = self.bot.get_guild(_positiveInt(result.guild_id))
        summary = janeIdentity.formatApplySummary(changes, guild=guild)
        hasIssues = bool(changes.get("permissionIssues"))
        embed = discord.Embed(
            title="Jane Identity Verification Complete",
            description=f"Linked Roblox account: `{result.roblox_username}`",
            color=discord.Color.orange() if hasIssues else discord.Color.green(),
        )
        if guild is not None:
            embed.add_field(name="Server", value=str(guild.name), inline=False)
        embed.add_field(name="Update summary", value=self._shortText(summary), inline=False)
        embed.set_footer(text="Jane Identity")

        content = (
            "Jane linked your Roblox account, but some Discord updates need staff attention."
            if hasIssues
            else "Jane linked your Roblox account."
        )
        try:
            await taskBudgeter.runDiscord(
                lambda: user.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM Jane Identity relay success to Discord user %s.", result.discord_user_id)

    @staticmethod
    def _relayEnabled() -> bool:
        return bool(getattr(config, "janeIdentityRelayEnabled", False))

    @staticmethod
    def _relayPollIntervalSec() -> float:
        try:
            value = float(getattr(config, "janeIdentityRelayPollIntervalSec", 5) or 5)
        except (TypeError, ValueError):
            value = 5.0
        return max(2.0, min(value, 300.0))

    @staticmethod
    def _relayBatchSize() -> int:
        try:
            value = int(getattr(config, "janeIdentityRelayBatchSize", 10) or 10)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(value, 25))

    @staticmethod
    def _relayEndpoint(path: str) -> str:
        baseUrl = janeIdentity.relayBaseUrl().rstrip("/")
        cleanPath = str(path or "").strip()
        if not cleanPath.startswith("/"):
            cleanPath = f"/{cleanPath}"
        return f"{baseUrl}{cleanPath}"

    def _relayProblem(self, message: str) -> None:
        clean = str(message or "").strip()
        if clean and clean != self._relayLastProblem:
            self._relayLastProblem = clean
            log.warning("Jane Identity relay polling paused: %s", clean)

    async def _fetchRelayItems(self) -> list[dict[str, Any]]:
        token = janeIdentity.relayApiToken()
        if not token:
            self._relayProblem("relay API token is missing")
            return []
        if not janeIdentity.relayBaseUrl():
            self._relayProblem("relay base URL is missing")
            return []

        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"Authorization": f"Bearer {token}"}
        url = self._relayEndpoint("/identity/api/pending.php")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params={"limit": str(self._relayBatchSize())}) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
                    raise RuntimeError(f"relay pending lookup failed ({response.status})")
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        self._relayLastProblem = ""
        return [item for item in items if isinstance(item, dict)]

    async def _ackRelayItems(self, relayIds: list[str]) -> None:
        cleanIds = [str(relayId).strip() for relayId in relayIds if str(relayId).strip()]
        if not cleanIds:
            return
        token = janeIdentity.relayApiToken()
        if not token or not janeIdentity.relayBaseUrl():
            return
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._relayEndpoint("/identity/api/ack.php"),
                headers=headers,
                json={"ids": cleanIds},
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
                    raise RuntimeError(f"relay ack failed ({response.status})")

    async def _processRelayItem(self, item: dict[str, Any]) -> bool:
        relayId = str(item.get("relayId") or "").strip()
        if not relayId:
            return False
        state = str(item.get("state") or "").strip()
        code = str(item.get("code") or "").strip()
        oauthError = str(item.get("error") or "").strip()
        errorDescription = str(item.get("errorDescription") or item.get("error_description") or oauthError).strip()

        if oauthError:
            result = await janeIdentity.failRobloxOAuthAttempt(
                state=state,
                error=errorDescription or oauthError,
            )
            await self._sendVerificationFailureDm(result, cancelled=True)
            return True

        result = await janeIdentity.completeRobloxOAuth(code=code, state=state)
        if not result.ok:
            await self._sendVerificationFailureDm(result)
            return True

        try:
            changes = await janeIdentity.applyMemberVerification(self.bot, result)
        except Exception:
            log.exception(
                "Jane Identity relay linked Roblox account but failed to apply member state for Discord user %s.",
                result.discord_user_id,
            )
            changes = {
                "updated": False,
                "permissionIssues": [
                    "Jane linked the account, but applying Discord nickname/roles failed. Staff should check logs."
                ],
            }
        await self._sendVerificationSuccessDm(result, changes)
        return True

    async def _pollRelayOnce(self) -> None:
        items = await self._fetchRelayItems()
        if not items:
            return
        processedIds: list[str] = []
        for item in items:
            relayId = str(item.get("relayId") or "").strip()
            try:
                processed = await self._processRelayItem(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Jane Identity relay item processing failed for relayId=%s.", relayId or "unknown")
                processed = False
            if processed and relayId:
                processedIds.append(relayId)
        if processedIds:
            await self._ackRelayItems(processedIds)

    async def _relayPollLoop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                if self._relayEnabled():
                    await self._pollRelayOnce()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._relayProblem(str(exc) or "relay polling failed")
                log.exception("Jane Identity relay poll failed.")
            await asyncio.sleep(self._relayPollIntervalSec())

    @staticmethod
    def _discordUserLabel(user: discord.abc.User) -> str:
        discriminator = str(getattr(user, "discriminator", "") or "").strip()
        name = str(getattr(user, "name", "") or str(user)).strip()
        if discriminator:
            return f"{name}#{discriminator}"
        return str(user)

    @staticmethod
    def _formatRobloxDate(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "Unknown"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw[:10] if raw else "Unknown"
        return f"{parsed.month}/{parsed.day}/{parsed.year}"

    async def _fetchRobloxDetails(self, robloxUserId: int) -> dict[str, Any]:
        try:
            safeUserId = int(robloxUserId)
        except (TypeError, ValueError):
            safeUserId = 0
        if safeUserId <= 0:
            return {}
        try:
            status, data = await robloxTransport.requestJson(
                "GET",
                f"https://users.roblox.com/v1/users/{safeUserId}",
                timeoutSec=10,
            )
        except Exception:
            return {}
        if status != 200 or not isinstance(data, dict):
            return {}
        return data

    async def _fetchRobloxAvatarUrl(self, robloxUserId: int) -> str:
        try:
            safeUserId = int(robloxUserId)
        except (TypeError, ValueError):
            safeUserId = 0
        if safeUserId <= 0:
            return ""
        try:
            status, data = await robloxTransport.requestJson(
                "GET",
                "https://thumbnails.roblox.com/v1/users/avatar",
                params={
                    "userIds": str(safeUserId),
                    "size": "150x150",
                    "format": "Png",
                    "isCircular": "false",
                },
                timeoutSec=10,
            )
        except Exception:
            return ""
        if status != 200 or not isinstance(data, dict):
            return ""
        rows = data.get("data")
        if not isinstance(rows, list) or not rows:
            return ""
        first = rows[0]
        if not isinstance(first, dict):
            return ""
        return str(first.get("imageUrl") or "").strip()

    async def _postWhoisWebhook(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
    ) -> bool:
        channel = interaction.channel
        if channel is None:
            return False
        return await runtimeWebhooks.sendOwnedWebhookMessage(
            botClient=self.bot,
            channel=channel,
            webhookName=self._janeWebhookName(),
            username=self._janeWebhookName(),
            avatarUrl=self._janeWebhookAvatarUrl(),
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            reason="Jane Identity whois lookup",
        )

    def _buildDiscordWhoisEmbed(
        self,
        *,
        member: discord.Member,
        robloxUserId: int,
        robloxUsername: str,
        details: dict[str, Any],
        avatarUrl: str,
    ) -> discord.Embed:
        displayName = str(details.get("displayName") or robloxUsername or "").strip() or "Unknown"
        currentUsername = str(details.get("name") or robloxUsername or "").strip() or "Unknown"
        description = str(details.get("description") or "").strip() or "(no Roblox description)"
        embed = discord.Embed(
            title=self._discordUserLabel(member),
            description=description[:4096],
            color=0x2B2D31,
        )
        embed.add_field(name="Roblox username", value=currentUsername, inline=True)
        embed.add_field(name="Roblox display name", value=displayName, inline=True)
        embed.add_field(name="Roblox user ID", value=str(int(robloxUserId)), inline=True)
        embed.add_field(name="Is banned?", value="Yes" if bool(details.get("isBanned")) else "No", inline=True)
        embed.add_field(name="Join date", value=self._formatRobloxDate(details.get("created")), inline=True)
        if avatarUrl:
            embed.set_thumbnail(url=avatarUrl)
        return embed

    async def _memberForDiscordId(self, guild: discord.Guild, discordUserId: int) -> discord.Member | None:
        member = guild.get_member(int(discordUserId))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(discordUserId))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _buildRobloxWhoisEmbed(self, member: discord.Member) -> discord.Embed:
        embed = discord.Embed(
            title=self._discordUserLabel(member),
            description=member.mention,
            color=0x2B2D31,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    def isAdmin(self, member: discord.Member) -> bool:
        return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)

    async def ensureAdmin(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await self.safeEphemeral(interaction, "This control must be used in a server.")
            return False
        if not self.isAdmin(interaction.user):
            await self.safeEphemeral(interaction, "Administrator or manage-server required.")
            return False
        return True

    async def _sendLinkPrompt(
        self,
        interaction: discord.Interaction,
        *,
        prefix: str = "",
    ) -> None:
        problem = janeIdentity.configurationProblem()
        if problem:
            return await self._safeReply(
                interaction,
                f"{prefix}Jane Identity cannot create a fresh link yet: `{problem}`",
            )
        attempt = await janeIdentity.createLinkAttempt(
            discordUserId=int(interaction.user.id),
            guildId=int(interaction.guild_id or 0),
        )
        ttlMinutes = max(1, int((attempt.expires_at - datetime.now(timezone.utc)).total_seconds() // 60))
        await self._safeReply(
            interaction,
            (
                f"{prefix}Use the button below to sign into Roblox and link your account to Jane. "
                f"This link expires in about `{ttlMinutes}` minute(s)."
            ),
            view=VerifyLinkView(attempt.authorize_url),
        )

    async def refreshStoredLink(self, interaction: discord.Interaction, lookup: Any) -> None:
        result = janeIdentity.resultFromStoredIdentity(
            discordUserId=int(interaction.user.id),
            guildId=int(interaction.guild_id or 0),
            lookup=lookup,
        )
        if not result.ok:
            return await self._sendLinkPrompt(interaction)
        changes = await janeIdentity.applyMemberVerification(self.bot, result)
        summary = janeIdentity.formatApplySummary(changes, guild=interaction.guild)
        await self._safeReply(
            interaction,
            f"Refreshed `{result.roblox_username}` for this server.\n{summary}",
        )

    @staticmethod
    def _groupLabel(row: dict[str, Any]) -> str:
        groupId = _positiveInt(row.get("groupId"))
        name = str(row.get("groupName") or "").strip()
        if name:
            return f"{name} ({groupId})"
        return f"Group {groupId}"

    @staticmethod
    def _selectedRoleRuleGroup(
        groups: list[dict[str, Any]],
        selectedGroupId: int = 0,
    ) -> dict[str, Any] | None:
        validGroups = [group for group in groups if _positiveInt(group.get("groupId")) > 0]
        if not validGroups:
            return None
        safeSelectedGroupId = _positiveInt(selectedGroupId)
        for group in validGroups:
            if _positiveInt(group.get("groupId")) == safeSelectedGroupId:
                return group
        return validGroups[0]

    async def _roleRuleRankChoicesForGroup(
        self,
        group: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        choices: list[dict[str, Any]] = []
        errors: list[str] = []
        if group is None:
            return choices, errors
        groupId = _positiveInt(group.get("groupId"))
        if groupId <= 0:
            return choices, errors
        result = await robloxGroups.fetchRobloxGroupRoles(groupId)
        if result.status != 200:
            errors.append(f"`{groupId}`: {result.error or 'Could not load rank names.'}")
            return choices, errors
        for role in result.roles:
            try:
                rank = int(role.get("rank"))
            except (TypeError, ValueError):
                rank = -1
            if rank < 0:
                continue
            choices.append(
                {
                    "number": len(choices) + 1,
                    "groupId": groupId,
                    "groupLabel": self._groupLabel(group),
                    "rank": rank,
                    "rankName": str(role.get("name") or f"Rank {rank}").strip(),
                    "memberCount": _positiveInt(role.get("memberCount")),
                }
            )
        return choices, errors

    @staticmethod
    def _choiceLine(choice: dict[str, Any]) -> str:
        memberCount = _positiveInt(choice.get("memberCount"))
        memberText = f" - {memberCount} member(s)" if memberCount > 0 else ""
        return (
            f"`#{choice.get('number')}` {choice.get('rankName')} "
            f"(rank `{choice.get('rank')}`){memberText}"
        )

    def buildRoleRulesPanelEmbed(
        self,
        guild: discord.Guild,
        configRows: dict[str, Any],
        selectedGroup: dict[str, Any] | None,
        rankChoices: list[dict[str, Any]],
        errors: list[str],
        *,
        noticeText: str = "",
    ) -> discord.Embed:
        selectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        selectedGroupLabel = self._groupLabel(selectedGroup or {}) if selectedGroupId > 0 else "(none)"
        embed = discord.Embed(
            title="Role Rules",
            description=(
                str(noticeText or "").strip()
                or "Select a Roblox group, then use the numbered ranks below when adding a Discord role rule."
            ),
            color=discord.Color.blurple(),
        )
        groups = configRows.get("groups") or []
        if len(groups) > 25:
            embed.add_field(
                name="Group Selector",
                value="Only the first 25 connected groups can appear in the dropdown.",
                inline=False,
            )
        embed.add_field(name="Selected Roblox Group", value=selectedGroupLabel, inline=False)

        allRoleRules = configRows.get("roleRules") or []
        roleRules = [
            row for row in allRoleRules
            if _positiveInt(row.get("groupId")) == selectedGroupId
        ]
        choicesByGroupRank = {
            (_positiveInt(choice.get("groupId")), _positiveInt(choice.get("rank"))): choice
            for choice in rankChoices
        }
        ruleLines: list[str] = []
        for row in roleRules[:20]:
            groupId = _positiveInt(row.get("groupId"))
            minRank = _positiveInt(row.get("minRank"))
            maxRank = _positiveInt(row.get("maxRank"))
            role = guild.get_role(_positiveInt(row.get("roleId")))
            roleText = role.mention if role is not None else f"`{row.get('roleId')}`"
            rankChoice = choicesByGroupRank.get((groupId, minRank)) if minRank == maxRank else None
            if rankChoice:
                rankText = f"`#{rankChoice.get('number')}` {rankChoice.get('rankName')}"
            else:
                rankText = f"ranks `{minRank}-{maxRank}`"
            ruleLines.append(f"`#{row.get('ruleId')}` {rankText} -> {roleText}")
        if len(roleRules) > len(ruleLines):
            ruleLines.append(f"... and {len(roleRules) - len(ruleLines)} more rule(s).")
        embed.add_field(
            name=f"Configured Role Rules For This Group ({len(roleRules)})",
            value="\n".join(ruleLines) if ruleLines else "(none)",
            inline=False,
        )

        if rankChoices:
            lines = [self._choiceLine(choice) for choice in rankChoices]
            chunks = runtimeWebhooks.buildEmbedFieldChunks(
                lines,
                emptyText="No ranks found.",
                overflowNoun="rank(s)",
                maxChunks=max(1, min(8, 24 - len(embed.fields))),
            )
            for chunkIndex, chunk in enumerate(chunks):
                if len(embed.fields) >= 25:
                    break
                name = "Ranks" if chunkIndex == 0 else "Ranks continued"
                embed.add_field(name=name, value=chunk, inline=False)

        if selectedGroup is None:
            embed.add_field(
                name="Ranks",
                value="Connect a Roblox group first.",
                inline=False,
            )
        elif not rankChoices and not errors:
            embed.add_field(
                name="Ranks",
                value="No ranks were returned for the selected group.",
                inline=False,
            )
        if errors:
            chunks = runtimeWebhooks.buildEmbedFieldChunks(
                errors,
                emptyText="None",
                overflowNoun="error(s)",
                maxChunks=1,
            )
            if len(embed.fields) < 25:
                embed.add_field(name="Load Issues", value=chunks[0], inline=False)
        embed.set_footer(text="Add uses rank numbers for the selected group. Remove uses configured rule IDs.")
        return embed

    async def openRoleRulesPanel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        groups = configRows.get("groups") or []
        selectedGroup = self._selectedRoleRuleGroup(groups)
        selectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        rankChoices, errors = await self._roleRuleRankChoicesForGroup(selectedGroup)
        embed = self.buildRoleRulesPanelEmbed(guild, configRows, selectedGroup, rankChoices, errors)
        await interaction.followup.send(
            embed=embed,
            view=RoleRulesPanelView(
                cog=self,
                openerId=int(interaction.user.id),
                groups=groups,
                selectedGroupId=selectedGroupId,
                rankChoices=rankChoices,
            ),
            ephemeral=True,
        )

    async def refreshRoleRulesPanelMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        selectedGroupId: int = 0,
        noticeText: str = "",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        groups = configRows.get("groups") or []
        selectedGroup = self._selectedRoleRuleGroup(groups, selectedGroupId)
        resolvedSelectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        rankChoices, errors = await self._roleRuleRankChoicesForGroup(selectedGroup)
        embed = self.buildRoleRulesPanelEmbed(
            guild,
            configRows,
            selectedGroup,
            rankChoices,
            errors,
            noticeText=noticeText,
        )
        view = RoleRulesPanelView(
            cog=self,
            openerId=int(openerId),
            groups=groups,
            selectedGroupId=resolvedSelectedGroupId,
            rankChoices=rankChoices,
            noticeText=noticeText,
        )
        await runtimeViewBases.safeRefreshInteractionMessage(interaction, embed=embed, view=view)

    def buildNameRulesPanelEmbed(
        self,
        configRows: dict[str, Any],
        selectedGroup: dict[str, Any] | None,
        rankChoices: list[dict[str, Any]],
        errors: list[str],
        *,
        noticeText: str = "",
    ) -> discord.Embed:
        selectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        selectedGroupLabel = self._groupLabel(selectedGroup or {}) if selectedGroupId > 0 else "(none)"
        embed = discord.Embed(
            title="Name Rules",
            description=(
                str(noticeText or "").strip()
                or "Select a Roblox group, then use the numbered ranks below when adding a nickname rule."
            ),
            color=discord.Color.blurple(),
        )
        groups = configRows.get("groups") or []
        if len(groups) > 25:
            embed.add_field(
                name="Group Selector",
                value="Only the first 25 connected groups can appear in the dropdown.",
                inline=False,
            )
        embed.add_field(name="Selected Roblox Group", value=selectedGroupLabel, inline=False)

        allNameRules = configRows.get("nameRules") or []
        nameRules = [
            row for row in allNameRules
            if _positiveInt(row.get("groupId")) == selectedGroupId
        ]
        choicesByGroupRank = {
            (_positiveInt(choice.get("groupId")), _positiveInt(choice.get("rank"))): choice
            for choice in rankChoices
        }
        ruleLines: list[str] = []
        for row in nameRules[:20]:
            groupId = _positiveInt(row.get("groupId"))
            minRank = _positiveInt(row.get("minRank"))
            maxRank = _positiveInt(row.get("maxRank"))
            rankChoice = choicesByGroupRank.get((groupId, minRank)) if minRank == maxRank else None
            if rankChoice:
                rankText = f"`#{rankChoice.get('number')}` {rankChoice.get('rankName')}"
            else:
                rankText = f"ranks `{minRank}-{maxRank}`"
            sample = " ".join(
                part for part in (
                    str(row.get("prefix") or "").strip(),
                    "robloxusername",
                    str(row.get("suffix") or "").strip(),
                )
                if part
            )
            priority = _positiveInt(row.get("priority"))
            priorityText = f" priority `{priority}`" if priority else ""
            ruleLines.append(f"`#{row.get('ruleId')}` {rankText} -> `{sample}`{priorityText}")
        if len(nameRules) > len(ruleLines):
            ruleLines.append(f"... and {len(nameRules) - len(ruleLines)} more rule(s).")
        embed.add_field(
            name=f"Configured Name Rules For This Group ({len(nameRules)})",
            value="\n".join(ruleLines) if ruleLines else "(none)",
            inline=False,
        )

        if rankChoices:
            lines = [self._choiceLine(choice) for choice in rankChoices]
            chunks = runtimeWebhooks.buildEmbedFieldChunks(
                lines,
                emptyText="No ranks found.",
                overflowNoun="rank(s)",
                maxChunks=max(1, min(8, 24 - len(embed.fields))),
            )
            for chunkIndex, chunk in enumerate(chunks):
                if len(embed.fields) >= 25:
                    break
                name = "Ranks" if chunkIndex == 0 else "Ranks continued"
                embed.add_field(name=name, value=chunk, inline=False)

        if selectedGroup is None:
            embed.add_field(
                name="Ranks",
                value="Connect a Roblox group first.",
                inline=False,
            )
        elif not rankChoices and not errors:
            embed.add_field(
                name="Ranks",
                value="No ranks were returned for the selected group.",
                inline=False,
            )
        if errors:
            chunks = runtimeWebhooks.buildEmbedFieldChunks(
                errors,
                emptyText="None",
                overflowNoun="error(s)",
                maxChunks=1,
            )
            if len(embed.fields) < 25:
                embed.add_field(name="Load Issues", value=chunks[0], inline=False)
        embed.set_footer(text="Add uses rank numbers for the selected group. Remove uses configured rule IDs.")
        return embed

    async def openNameRulesPanel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        groups = configRows.get("groups") or []
        selectedGroup = self._selectedRoleRuleGroup(groups)
        selectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        rankChoices, errors = await self._roleRuleRankChoicesForGroup(selectedGroup)
        embed = self.buildNameRulesPanelEmbed(configRows, selectedGroup, rankChoices, errors)
        await interaction.followup.send(
            embed=embed,
            view=NameRulesPanelView(
                cog=self,
                openerId=int(interaction.user.id),
                groups=groups,
                selectedGroupId=selectedGroupId,
                rankChoices=rankChoices,
            ),
            ephemeral=True,
        )

    async def refreshNameRulesPanelMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        selectedGroupId: int = 0,
        noticeText: str = "",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        groups = configRows.get("groups") or []
        selectedGroup = self._selectedRoleRuleGroup(groups, selectedGroupId)
        resolvedSelectedGroupId = _positiveInt((selectedGroup or {}).get("groupId"))
        rankChoices, errors = await self._roleRuleRankChoicesForGroup(selectedGroup)
        embed = self.buildNameRulesPanelEmbed(
            configRows,
            selectedGroup,
            rankChoices,
            errors,
            noticeText=noticeText,
        )
        view = NameRulesPanelView(
            cog=self,
            openerId=int(openerId),
            groups=groups,
            selectedGroupId=resolvedSelectedGroupId,
            rankChoices=rankChoices,
            noticeText=noticeText,
        )
        await runtimeViewBases.safeRefreshInteractionMessage(interaction, embed=embed, view=view)

    def buildRoverHubEmbed(
        self,
        guild: discord.Guild,
        configRows: dict[str, Any],
        *,
        noticeText: str = "",
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Rover Hub",
            description=(
                str(noticeText or "").strip()
                or "Manage Jane Identity groups, rank roles, and nickname formatting."
            ),
            color=discord.Color.blurple(),
        )
        settings = configRows.get("settings") or {}
        groups = configRows.get("groups") or []
        roleRules = configRows.get("roleRules") or []
        nameRules = configRows.get("nameRules") or []
        unverifiedRole = guild.get_role(_positiveInt(settings.get("unverifiedRoleId")))
        unverifiedText = unverifiedRole.mention if unverifiedRole is not None else "(none)"
        groupLines = [
            f"`{row.get('groupId')}` {str(row.get('groupName') or '').strip() or '(no label)'}"
            for row in groups[:12]
        ]
        roleLines = []
        for row in roleRules[:12]:
            role = guild.get_role(_positiveInt(row.get("roleId")))
            roleText = role.mention if role is not None else f"`{row.get('roleId')}`"
            roleLines.append(
                f"`#{row.get('ruleId')}` group `{row.get('groupId')}` ranks "
                f"`{row.get('minRank')}-{row.get('maxRank')}` -> {roleText}"
            )
        nameLines = []
        for row in nameRules[:12]:
            sample = " ".join(
                part for part in (
                    str(row.get("prefix") or "").strip(),
                    "robloxusername",
                    str(row.get("suffix") or "").strip(),
                )
                if part
            )
            nameLines.append(
                f"`#{row.get('ruleId')}` group `{row.get('groupId')}` ranks "
                f"`{row.get('minRank')}-{row.get('maxRank')}` -> `{sample}`"
            )
        embed.add_field(
            name=f"Connected Groups ({len(groups)})",
            value="\n".join(groupLines) if groupLines else "(none)",
            inline=False,
        )
        embed.add_field(
            name=f"Rank Role Rules ({len(roleRules)})",
            value="\n".join(roleLines) if roleLines else "(none)",
            inline=False,
        )
        embed.add_field(
            name=f"Nickname Rules ({len(nameRules)})",
            value="\n".join(nameLines) if nameLines else "(none)",
            inline=False,
        )
        embed.add_field(
            name="Unverified Role",
            value=unverifiedText,
            inline=False,
        )
        embed.set_footer(text="Use rule IDs shown here when removing rules.")
        return embed

    async def refreshRoverHubMessage(
        self,
        *,
        interaction: discord.Interaction,
        openerId: int,
        noticeText: str = "",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        embed = self.buildRoverHubEmbed(guild, configRows, noticeText=noticeText)
        view = RoverHubView(cog=self, openerId=int(openerId), noticeText=noticeText)
        await runtimeViewBases.safeRefreshInteractionMessage(interaction, embed=embed, view=view)

    def _trackBackgroundTask(self, coro: Any, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._backgroundTasks.add(task)

        def _done(doneTask: asyncio.Task[Any]) -> None:
            self._backgroundTasks.discard(doneTask)
            if doneTask.cancelled():
                return
            try:
                exc = doneTask.exception()
            except Exception:
                return
            if exc is not None:
                log.error(
                    "Jane Identity background task failed: %s",
                    name,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_done)

    def _janeWebhookName(self) -> str:
        botUser = getattr(self.bot, "user", None)
        for attrName in ("display_name", "global_name", "name"):
            value = str(getattr(botUser, attrName, "") or "").strip()
            if value:
                return value[:80]
        return "Jane Clanker"

    def _janeWebhookAvatarUrl(self) -> str:
        botUser = getattr(self.bot, "user", None)
        displayAvatar = getattr(botUser, "display_avatar", None)
        url = str(getattr(displayAvatar, "url", "") or "").strip()
        return url

    @staticmethod
    def _bulkUpdateMaxMembers() -> int:
        try:
            value = int(getattr(config, "janeIdentityBulkUpdateMaxMembers", 5000) or 5000)
        except (TypeError, ValueError):
            value = 5000
        return max(1, value)

    @staticmethod
    def _bulkUpdateDelaySec() -> float:
        try:
            value = float(getattr(config, "janeIdentityBulkUpdateDelaySec", 0.2) or 0.0)
        except (TypeError, ValueError):
            value = 0.2
        return max(0.0, min(value, 5.0))

    @staticmethod
    def _scheduledRefreshEnabled() -> bool:
        return bool(getattr(config, "janeIdentityScheduledRefreshEnabled", True))

    @staticmethod
    def _centralTimezone() -> timezone:
        try:
            return ZoneInfo("America/Chicago")
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=-6), name="CST")

    @classmethod
    def _centralNow(cls) -> datetime:
        return datetime.now(cls._centralTimezone())

    @staticmethod
    def _scheduledRefreshShardCount() -> int:
        try:
            value = int(getattr(config, "janeIdentityScheduledRefreshShardCount", 7) or 7)
        except (TypeError, ValueError):
            value = 7
        return max(1, min(value, 31))

    @staticmethod
    def _scheduledRefreshHourCentral() -> int:
        try:
            value = int(getattr(config, "janeIdentityScheduledRefreshHourCentral", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(value, 23))

    @staticmethod
    def _scheduledRefreshMinuteCentral() -> int:
        try:
            value = int(getattr(config, "janeIdentityScheduledRefreshMinuteCentral", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(value, 59))

    @classmethod
    def _nextScheduledRefreshAt(cls, now: datetime | None = None) -> datetime:
        current = now or cls._centralNow()
        target = current.replace(
            hour=cls._scheduledRefreshHourCentral(),
            minute=cls._scheduledRefreshMinuteCentral(),
            second=0,
            microsecond=0,
        )
        if target <= current:
            target += timedelta(days=1)
        return target

    @staticmethod
    def _scheduledRefreshMaxMembers() -> int:
        try:
            value = int(getattr(config, "janeIdentityScheduledRefreshMaxMembers", 5000) or 5000)
        except (TypeError, ValueError):
            value = 5000
        return max(1, value)

    @staticmethod
    def _scheduledRefreshDelaySec() -> float:
        try:
            value = float(getattr(config, "janeIdentityScheduledRefreshDelaySec", 0.5) or 0.0)
        except (TypeError, ValueError):
            value = 0.5
        return max(0.0, min(value, 30.0))

    @staticmethod
    def _scheduledRefreshBusyPollSec() -> float:
        try:
            value = float(getattr(config, "janeIdentityScheduledRefreshBusyPollSec", 30) or 30)
        except (TypeError, ValueError):
            value = 30.0
        return max(5.0, min(value, 300.0))

    async def _scheduledRefreshMembers(self, guild: discord.Guild) -> list[discord.Member]:
        maxMembers = self._scheduledRefreshMaxMembers()
        seen: dict[int, discord.Member] = {}
        if bool(getattr(config, "janeIdentityScheduledRefreshFetchMembers", True)):
            try:
                async for member in guild.fetch_members(limit=None):
                    seen[int(member.id)] = member
                    if len(seen) >= maxMembers:
                        break
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not fetch full member list for Jane Identity scheduled refresh in guild %s.", guild.id)
        for member in guild.members:
            if len(seen) >= maxMembers:
                break
            seen.setdefault(int(member.id), member)
        return list(seen.values())

    async def _waitForScheduledRefreshQuietBudget(self) -> None:
        if not bool(getattr(config, "janeIdentityScheduledRefreshPauseWhenBusy", True)):
            return
        watchedFeatures = {
            "robloxApi",
            "discordIo",
            "interactionAck",
            "sheetsIo",
            "sheetsInteractive",
            "sheetsBackground",
            "backgroundJobs",
        }
        while True:
            if self._bulkUpdateGuilds:
                await asyncio.sleep(self._scheduledRefreshBusyPollSec())
                continue
            try:
                snapshot = await taskBudgeter.getBudgeter().snapshot()
            except Exception:
                return
            features = snapshot.get("features") if isinstance(snapshot, dict) else {}
            busy = False
            if isinstance(features, dict):
                for name in watchedFeatures:
                    stats = features.get(name)
                    if not isinstance(stats, dict):
                        continue
                    pending = int(stats.get("pending") or 0)
                    if pending > 0:
                        busy = True
                        break
            if not busy:
                return
            await asyncio.sleep(self._scheduledRefreshBusyPollSec())

    async def _bulkUpdateMembers(self, guild: discord.Guild) -> list[discord.Member]:
        maxMembers = self._bulkUpdateMaxMembers()
        seen: dict[int, discord.Member] = {}
        if bool(getattr(config, "janeIdentityBulkUpdateFetchMembers", True)):
            try:
                async for member in guild.fetch_members(limit=None):
                    seen[int(member.id)] = member
                    if len(seen) >= maxMembers:
                        break
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not fetch full member list for Jane Identity bulk update in guild %s.", guild.id)
        for member in guild.members:
            if len(seen) >= maxMembers:
                break
            seen.setdefault(int(member.id), member)
        return list(seen.values())

    @staticmethod
    def _identityResultFromPayload(
        *,
        member: discord.Member,
        guildId: int,
        payload: dict[str, Any],
    ) -> janeIdentity.IdentityLinkResult | None:
        username = str(payload.get("robloxUsername") or payload.get("username") or "").strip()
        if not username:
            return None
        return janeIdentity.IdentityLinkResult(
            ok=True,
            discord_user_id=int(member.id),
            guild_id=int(guildId),
            roblox_user_id=_positiveInt(payload.get("robloxId")),
            roblox_username=username,
        )

    async def _postBulkUpdateSummary(
        self,
        channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
        *,
        guild: discord.Guild,
        requestedById: int,
        summary: dict[str, int],
    ) -> None:
        if channel is None:
            return
        embed = discord.Embed(
            title="Jane Identity Bulk Update Complete",
            description=(
                f"Requested by <@{requestedById}>.\n"
                "This run used stored Jane Identity links only; it did not call RoVer for unlinked members."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Members scanned", value=str(summary.get("scanned", 0)), inline=True)
        embed.add_field(name="Linked refreshed", value=str(summary.get("linked", 0)), inline=True)
        embed.add_field(name="Changed", value=str(summary.get("changed", 0)), inline=True)
        embed.add_field(name="Unverified updated", value=str(summary.get("unverifiedChanged", 0)), inline=True)
        embed.add_field(name="Bots skipped", value=str(summary.get("botsSkipped", 0)), inline=True)
        embed.add_field(name="Failed", value=str(summary.get("failed", 0)), inline=True)
        embed.set_footer(text=f"{guild.name} - Manual/throttled update")
        await runtimeWebhooks.sendOwnedWebhookMessage(
            botClient=self.bot,
            channel=channel,
            webhookName=self._janeWebhookName(),
            username=self._janeWebhookName(),
            avatarUrl=self._janeWebhookAvatarUrl(),
            embed=embed,
            reason="Jane Identity bulk update summary",
        )

    async def _runScheduledRefreshForGuild(
        self,
        *,
        guild: discord.Guild,
        identitiesByDiscordId: dict[int, dict[str, Any]],
        shardIndex: int,
        shardCount: int,
    ) -> dict[str, int]:
        summary = {
            "candidates": 0,
            "selected": 0,
            "changed": 0,
            "failed": 0,
            "botsSkipped": 0,
        }
        members = await self._scheduledRefreshMembers(guild)
        linkedMembers: list[tuple[discord.Member, dict[str, Any]]] = []
        for member in members:
            if member.bot:
                summary["botsSkipped"] += 1
                continue
            payload = identitiesByDiscordId.get(int(member.id))
            if payload is None:
                continue
            result = self._identityResultFromPayload(
                member=member,
                guildId=int(guild.id),
                payload=payload,
            )
            if result is None:
                continue
            linkedMembers.append((member, payload))

        linkedMembers.sort(key=lambda item: int(item[0].id))
        summary["candidates"] = len(linkedMembers)
        delaySec = self._scheduledRefreshDelaySec()
        for index, (member, payload) in enumerate(linkedMembers):
            if index % shardCount != shardIndex:
                continue
            summary["selected"] += 1
            try:
                await self._waitForScheduledRefreshQuietBudget()
                result = self._identityResultFromPayload(
                    member=member,
                    guildId=int(guild.id),
                    payload=payload,
                )
                if result is None:
                    continue
                changes = await taskBudgeter.runLowestPriorityRoblox(
                    lambda: janeIdentity.applyMemberVerification(self.bot, result, member=member)
                )
                if bool(changes.get("updated")):
                    summary["changed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                summary["failed"] += 1
                log.exception(
                    "Jane Identity scheduled refresh failed for member %s in guild %s.",
                    member.id,
                    guild.id,
                )
            if delaySec > 0:
                await asyncio.sleep(delaySec)
        return summary

    async def _runScheduledRefresh(self, startedAtCentral: datetime) -> None:
        shardCount = self._scheduledRefreshShardCount()
        shardIndex = int(startedAtCentral.date().toordinal() % shardCount)
        identityRows = await janeIdentity.listStoredIdentityLinks(verifiedOnly=True)
        identitiesByDiscordId = {
            _positiveInt(row.get("discordId")): row
            for row in identityRows
            if _positiveInt(row.get("discordId")) > 0
        }
        totals = {
            "guilds": 0,
            "candidates": 0,
            "selected": 0,
            "changed": 0,
            "failed": 0,
            "botsSkipped": 0,
        }
        log.info(
            "Jane Identity scheduled refresh starting: shard %s/%s, linked identities=%s.",
            shardIndex + 1,
            shardCount,
            len(identitiesByDiscordId),
        )
        for guild in list(self.bot.guilds):
            if guild is None:
                continue
            await self._waitForScheduledRefreshQuietBudget()
            guildSummary = await self._runScheduledRefreshForGuild(
                guild=guild,
                identitiesByDiscordId=identitiesByDiscordId,
                shardIndex=shardIndex,
                shardCount=shardCount,
            )
            totals["guilds"] += 1
            for key in ("candidates", "selected", "changed", "failed", "botsSkipped"):
                totals[key] += int(guildSummary.get(key, 0))
        log.info(
            "Jane Identity scheduled refresh complete: shard %s/%s, guilds=%s, candidates=%s, selected=%s, changed=%s, failed=%s, botsSkipped=%s.",
            shardIndex + 1,
            shardCount,
            totals["guilds"],
            totals["candidates"],
            totals["selected"],
            totals["changed"],
            totals["failed"],
            totals["botsSkipped"],
        )

    async def _scheduledRefreshLoop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                if not self._scheduledRefreshEnabled():
                    await asyncio.sleep(300)
                    continue
                now = self._centralNow()
                nextRun = self._nextScheduledRefreshAt(now)
                await asyncio.sleep(max(1.0, (nextRun - now).total_seconds()))
                if not self._scheduledRefreshEnabled() or self.bot.is_closed():
                    continue
                await self._runScheduledRefresh(self._centralNow())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Jane Identity scheduled refresh loop failed.")
                await asyncio.sleep(300)

    async def _runBulkUpdate(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
        requestedById: int,
    ) -> None:
        guildId = int(guild.id)
        summary = {
            "scanned": 0,
            "linked": 0,
            "changed": 0,
            "unverifiedChanged": 0,
            "botsSkipped": 0,
            "failed": 0,
        }
        try:
            identityRows = await janeIdentity.listStoredIdentityLinks(verifiedOnly=True)
            identitiesByDiscordId = {
                _positiveInt(row.get("discordId")): row
                for row in identityRows
                if _positiveInt(row.get("discordId")) > 0
            }
            members = await self._bulkUpdateMembers(guild)
            delaySec = self._bulkUpdateDelaySec()
            unverifiedRoleIds = await janeIdentity.unverifiedRoleIds(guildId)
            managedRoleIds = await janeIdentity.managedRoleIds(guildId)
            for index, member in enumerate(members):
                if member.bot:
                    summary["botsSkipped"] += 1
                    continue
                summary["scanned"] += 1
                try:
                    payload = identitiesByDiscordId.get(int(member.id))
                    result = (
                        self._identityResultFromPayload(member=member, guildId=guildId, payload=payload)
                        if payload is not None
                        else None
                    )
                    if result is not None:
                        summary["linked"] += 1
                        changes = await taskBudgeter.runLowPriorityRoblox(
                            lambda: janeIdentity.applyMemberVerification(self.bot, result, member=member)
                        )
                        if bool(changes.get("updated")):
                            summary["changed"] += 1
                    else:
                        changes = await janeIdentity.applyUnverifiedMemberState(
                            self.bot,
                            guildId,
                            int(member.id),
                            unverifiedRoleIdList=unverifiedRoleIds,
                            managedRoleIdList=managedRoleIds,
                            member=member,
                        )
                        if bool(changes.get("updated")):
                            summary["unverifiedChanged"] += 1
                except Exception:
                    summary["failed"] += 1
                    log.exception("Jane Identity bulk update failed for member %s in guild %s.", member.id, guildId)
                if delaySec > 0 and index + 1 < len(members):
                    await asyncio.sleep(delaySec)
        finally:
            self._bulkUpdateGuilds.discard(guildId)
            await self._postBulkUpdateSummary(
                channel,
                guild=guild,
                requestedById=requestedById,
                summary=summary,
            )

    async def startBulkUpdateFromInteraction(self, interaction: discord.Interaction) -> None:
        if not await self.ensureAdmin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        guildId = int(guild.id)
        if guildId in self._bulkUpdateGuilds:
            return await self.safeEphemeral(interaction, "A Jane Identity bulk update is already running here.")
        self._bulkUpdateGuilds.add(guildId)
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._trackBackgroundTask(
            self._runBulkUpdate(
                guild=guild,
                channel=interaction.channel,
                requestedById=int(interaction.user.id),
            ),
            name=f"jane-identity-bulk-{guildId}",
        )
        await interaction.followup.send(
            "Jane Identity bulk update started. It is manual, throttled, and uses stored Jane links instead of RoVer lookups.",
            ephemeral=True,
        )

    async def _handleMemberJoin(self, member: discord.Member) -> None:
        if member.bot or not bool(getattr(config, "janeIdentityAutoApplyOnJoin", True)):
            return
        lookup = await robloxUsers.getVerifiedRobloxIdentity(int(member.id))
        if lookup is not None and str(getattr(lookup, "robloxUsername", "") or "").strip():
            result = janeIdentity.resultFromStoredIdentity(
                discordUserId=int(member.id),
                guildId=int(member.guild.id),
                lookup=lookup,
            )
            await taskBudgeter.runLowPriorityRoblox(
                lambda: janeIdentity.applyMemberVerification(self.bot, result, member=member)
            )
            return
        await janeIdentity.applyUnverifiedMemberState(
            self.bot,
            int(member.guild.id),
            int(member.id),
            member=member,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        self._trackBackgroundTask(
            self._handleMemberJoin(member),
            name=f"jane-identity-member-join-{int(member.guild.id)}-{int(member.id)}",
        )

    @app_commands.command(name="verify", description="Link or refresh your Roblox account in Jane.")
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await self._safeReply(interaction, "Run this inside a server.")
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        current = await robloxUsers.getVerifiedRobloxIdentity(int(interaction.user.id))
        if current is not None and current.robloxUsername:
            return await self.refreshStoredLink(interaction, current)
        await self._sendLinkPrompt(interaction)

    @app_commands.command(name="unlink", description="Remove your Jane Roblox account link and start a fresh link.")
    @app_commands.guild_only()
    async def unlink(self, interaction: discord.Interaction):
        await interactionRuntime.safeInteractionDefer(interaction, ephemeral=True, thinking=True)
        await robloxUsers.forgetRobloxIdentity(int(interaction.user.id))
        if interaction.guild is not None and isinstance(interaction.user, discord.Member):
            self._trackBackgroundTask(
                janeIdentity.applyUnverifiedMemberState(
                    self.bot,
                    int(interaction.guild.id),
                    int(interaction.user.id),
                    member=interaction.user,
                ),
                name=f"jane-identity-unlink-state-{int(interaction.guild.id)}-{int(interaction.user.id)}",
            )
        await self._sendLinkPrompt(
            interaction,
            prefix="Your stored Roblox account link has been removed. ",
        )

    @app_commands.command(name="rover", description="Open the Jane Identity/Rover configuration hub.")
    @app_commands.default_permissions(administrator=True, manage_guild=True)
    @app_commands.guild_only()
    async def rover(self, interaction: discord.Interaction):
        if not await self.ensureAdmin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            return await self.safeEphemeral(interaction, "This command must be used in a server.")
        configRows = await janeIdentity.guildConfiguration(int(guild.id))
        embed = self.buildRoverHubEmbed(guild, configRows)
        await self._safeReply(
            interaction,
            "",
            embed=embed,
            view=RoverHubView(cog=self, openerId=int(interaction.user.id)),
        )

    @whoisGroup.command(name="discord", description="Look up a server member's linked Roblox account.")
    @app_commands.guild_only()
    async def whoisDiscord(self, interaction: discord.Interaction, user: discord.Member):
        if not interaction.guild:
            return await self._safeReply(interaction, "Run this inside a server.")
        await interaction.response.defer(ephemeral=True)

        lookup = await robloxUsers.fetchVerifiedRobloxUser(int(user.id), guildId=int(interaction.guild_id or 0))
        robloxUserId = _positiveInt(getattr(lookup, "robloxId", 0))
        robloxUsername = str(getattr(lookup, "robloxUsername", "") or "").strip()
        if robloxUserId <= 0 and robloxUsername:
            resolved = await robloxUsers.fetchRobloxUserByUsername(robloxUsername)
            robloxUserId = _positiveInt(getattr(resolved, "robloxId", 0))
            robloxUsername = str(getattr(resolved, "robloxUsername", "") or robloxUsername).strip()
        if robloxUserId <= 0 or not robloxUsername:
            await interaction.followup.send(
                f"No linked Roblox account was found for {user.mention}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
            )
            return

        details = await self._fetchRobloxDetails(robloxUserId)
        avatarUrl = await self._fetchRobloxAvatarUrl(robloxUserId)
        embed = self._buildDiscordWhoisEmbed(
            member=user,
            robloxUserId=robloxUserId,
            robloxUsername=robloxUsername,
            details=details,
            avatarUrl=avatarUrl,
        )
        sent = await self._postWhoisWebhook(
            interaction,
            embed=embed,
            view=RobloxProfileView(robloxUserId),
        )
        await interaction.followup.send(
            "Posted whois lookup." if sent else "I could not post the whois webhook. Check manage-webhooks permission.",
            ephemeral=True,
        )

    @whoisGroup.command(name="roblox", description="Find server members linked to a Roblox username.")
    @app_commands.guild_only()
    async def whoisRoblox(self, interaction: discord.Interaction, username: str):
        if not interaction.guild:
            return await self._safeReply(interaction, "Run this inside a server.")
        await interaction.response.defer(ephemeral=True)

        resolved = await robloxUsers.fetchRobloxUserByUsername(username)
        robloxUserId = _positiveInt(getattr(resolved, "robloxId", 0))
        robloxUsername = str(getattr(resolved, "robloxUsername", "") or username).strip()
        if robloxUserId <= 0:
            await interaction.followup.send(
                str(getattr(resolved, "error", "") or f"No Roblox user found for `{username}`."),
                ephemeral=True,
            )
            return

        identities = await janeIdentity.apiDiscordIdentitiesByRobloxId(
            robloxUserId,
            guildId=int(interaction.guild_id or 0),
        )
        if not identities:
            identities = await janeIdentity.apiDiscordIdentitiesByRobloxUsername(
                robloxUsername,
                guildId=int(interaction.guild_id or 0),
            )

        members: list[discord.Member] = []
        seenIds: set[int] = set()
        for identity in identities:
            discordId = _positiveInt(identity.get("discordId"))
            if discordId <= 0 or discordId in seenIds:
                continue
            member = await self._memberForDiscordId(interaction.guild, discordId)
            if member is None:
                continue
            seenIds.add(discordId)
            members.append(member)

        if not members:
            await interaction.followup.send(
                f"No members in this server are linked to `{robloxUsername}`.",
                ephemeral=True,
            )
            return

        embeds = [self._buildRobloxWhoisEmbed(member) for member in members[:10]]
        extraText = ""
        if len(members) > len(embeds):
            extraText = f"\nOnly showing the first `{len(embeds)}` matching member(s)."
        content = (
            f"The following server members are verified as `{robloxUsername}`.\n"
            "**Note:** Matching users will not be included in this list if they aren't also in this Discord server."
            f"{extraText}"
        )
        sent = await self._postWhoisWebhook(
            interaction,
            content=content,
            embeds=embeds,
        )
        await interaction.followup.send(
            "Posted whois lookup." if sent else "I could not post the whois webhook. Check manage-webhooks permission.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(IdentityCog(bot))
