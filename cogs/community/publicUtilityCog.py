from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from features.community.publicUtility import (
    ButtonRoleView,
    RoleMenuView,
    addBlockedSelfRole,
    createButtonRoleEntry,
    deleteButtonRoleEntry,
    groupButtonRoleEntriesByMessage,
    configuredRoleMenus,
    isBlockedSelfRole,
    listAllButtonRoleEntries,
    listBlockedSelfRoles,
    listButtonRoleEntries,
    menuConfig,
    removeBlockedSelfRole,
    resolveAssignableRole,
)
from runtime import cogGuards as runtimeCogGuards
from runtime import interaction as interactionRuntime
from runtime import permissions as runtimePermissions
from runtime import viewBases as runtimeViewBases


def _parsePositiveId(raw: object) -> int:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return 0
    try:
        parsed = int(digits)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


class ReactionRoleMenuModal(discord.ui.Modal, title="Post Role Menu"):
    menuKey = discord.ui.TextInput(
        label="Menu key",
        required=True,
        max_length=64,
        placeholder="Example: colors",
    )

    def __init__(self, cog: "PublicUtilityCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.postRoleMenu(
            interaction,
            menu_key=str(self.menuKey.value or "").strip(),
        )


class ReactionRoleButtonsModal(discord.ui.Modal, title="Create Reaction-Role Message"):
    titleInput = discord.ui.TextInput(
        label="Embed title",
        required=True,
        max_length=120,
        placeholder="Choose Your Roles",
    )
    descriptionInput = discord.ui.TextInput(
        label="Embed description",
        required=False,
        max_length=1500,
        style=discord.TextStyle.paragraph,
        placeholder="Click a button below to add or remove roles.",
    )
    rowsInput = discord.ui.TextInput(
        label="Buttons",
        required=True,
        max_length=1500,
        style=discord.TextStyle.paragraph,
        placeholder="@Role | Label | 😀\n@Role2 | Label 2\n@Role3",
    )

    def __init__(self, cog: "PublicUtilityCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.runCreateReactionRoleButtons(
            interaction,
            title=str(self.titleInput.value or "").strip(),
            description=str(self.descriptionInput.value or "").strip(),
            rowsText=str(self.rowsInput.value or "").strip(),
        )


class ReactionRoleRemoveModal(discord.ui.Modal, title="Remove Reaction-Role Button"):
    messageIdInput = discord.ui.TextInput(
        label="Message ID",
        required=True,
        max_length=32,
        placeholder="123456789012345678",
    )
    roleInput = discord.ui.TextInput(
        label="Role mention or ID",
        required=True,
        max_length=64,
        placeholder="@Members or 123456789012345678",
    )

    def __init__(self, cog: "PublicUtilityCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.runRemoveReactionRoleButton(
            interaction,
            messageIdText=str(self.messageIdInput.value or "").strip(),
            roleText=str(self.roleInput.value or "").strip(),
        )


class ReactionRolePolicyModal(discord.ui.Modal):
    roleInput = discord.ui.TextInput(
        label="Role mention or ID",
        required=True,
        max_length=64,
        placeholder="@Members or 123456789012345678",
    )

    def __init__(self, cog: "PublicUtilityCog", *, action: str) -> None:
        self.cog = cog
        self.action = str(action or "").strip().lower()
        title = "Block Reaction-Role" if self.action == "block" else "Unblock Reaction-Role"
        super().__init__(title=title, timeout=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.action == "block":
            await self.cog.runBlockReactionRole(
                interaction,
                roleText=str(self.roleInput.value or "").strip(),
            )
            return
        await self.cog.runUnblockReactionRole(
            interaction,
            roleText=str(self.roleInput.value or "").strip(),
        )


class ReactionRolePanelView(runtimeViewBases.OwnerLockedView):
    def __init__(
        self,
        cog: "PublicUtilityCog",
        *,
        openerId: int,
        canManageButtons: bool,
        canManagePolicy: bool,
        canPostMenus: bool,
    ) -> None:
        super().__init__(openerId=openerId, timeout=600, ownerMessage="This reaction-role panel belongs to someone else.")
        self.cog = cog
        if not canPostMenus:
            self.postMenuBtn.disabled = True
        if not canManageButtons:
            self.createButtonsBtn.disabled = True
            self.removeButtonBtn.disabled = True
        if not canManagePolicy:
            self.blockRoleBtn.disabled = True
            self.unblockRoleBtn.disabled = True
            self.blockedListBtn.disabled = True

    @discord.ui.button(label="Post Menu", style=discord.ButtonStyle.secondary, row=0)
    async def postMenuBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(interaction, ReactionRoleMenuModal(self.cog))

    @discord.ui.button(label="Create Buttons", style=discord.ButtonStyle.primary, row=0)
    async def createButtonsBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(interaction, ReactionRoleButtonsModal(self.cog))

    @discord.ui.button(label="Remove Button", style=discord.ButtonStyle.secondary, row=0)
    async def removeButtonBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(interaction, ReactionRoleRemoveModal(self.cog))

    @discord.ui.button(label="Block Role", style=discord.ButtonStyle.danger, row=1)
    async def blockRoleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(interaction, ReactionRolePolicyModal(self.cog, action="block"))

    @discord.ui.button(label="Unblock Role", style=discord.ButtonStyle.success, row=1)
    async def unblockRoleBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interactionRuntime.safeInteractionSendModal(interaction, ReactionRolePolicyModal(self.cog, action="unblock"))

    @discord.ui.button(label="Blocked List", style=discord.ButtonStyle.secondary, row=1)
    async def blockedListBtn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.listBlockedReactionRoles(interaction)


class PublicUtilityCog(runtimeCogGuards.InteractionGuardMixin, commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _configuredRoleIdSet(attrName: str) -> set[int]:
        raw = getattr(config, attrName, [])
        if not isinstance(raw, (list, tuple, set)):
            return set()
        out: set[int] = set()
        for value in raw:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                out.add(parsed)
        return out

    def _canManageReactionRoleButtons(self, member: discord.Member) -> bool:
        allowedRoleIds = self._configuredRoleIdSet("reactionRoleCommandRoleIds")
        if not allowedRoleIds:
            return runtimePermissions.hasAdminOrManageGuild(member)
        memberRoleIds = {int(role.id) for role in member.roles}
        return bool(memberRoleIds & allowedRoleIds)

    def _canManageReactionRolePolicy(self, member: discord.Member) -> bool:
        policyRoleIds = self._configuredRoleIdSet("reactionRolePolicyRoleIds")
        if not policyRoleIds:
            policyRoleIds = self._configuredRoleIdSet("reactionRoleCommandRoleIds")
        if not policyRoleIds:
            return runtimePermissions.hasAdminOrManageGuild(member)
        memberRoleIds = {int(role.id) for role in member.roles}
        return bool(memberRoleIds & policyRoleIds)

    async def _requireReactionRoleCommandAccess(
        self,
        interaction: discord.Interaction,
    ) -> discord.Member | None:
        member = await self._requireGuildMember(interaction)
        if member is None:
            return None
        if self._canManageReactionRoleButtons(member):
            return member
        await self._safeReply(interaction, "You do not have permission to manage reaction roles.")
        return None

    async def _requireReactionRolePolicyAccess(
        self,
        interaction: discord.Interaction,
    ) -> discord.Member | None:
        member = await self._requireGuildMember(interaction)
        if member is None:
            return None
        if self._canManageReactionRolePolicy(member):
            return member
        await self._safeReply(interaction, "You do not have permission to change reaction-role safety rules.")
        return None

    @staticmethod
    def _buildReactionRoleEmbed(*, title: str, description: str) -> discord.Embed:
        safeTitle = str(title or "REACTION ROLES").strip()[:256]
        safeDescription = str(description or "").replace("\\n", "\n").strip()[:4096]
        return discord.Embed(
            title=safeTitle or "REACTION ROLES",
            description=safeDescription or "Click a button below to add or remove roles.",
            color=discord.Color.blurple(),
        )

    @staticmethod
    def _buildReactionRolePanelEmbed() -> discord.Embed:
        return discord.Embed(
            title="Reaction-Role Manager",
            description=(
                "Use the buttons below to manage self-role menus and button-role messages.\n"
                "Create button rows with one line per role using `role | label | emoji`.\n"
                "Example: `@Members | Members | 😀`"
            ),
            color=discord.Color.blurple(),
        )

    async def cog_load(self) -> None:
        for menuKey in configuredRoleMenus(config).keys():
            self.bot.add_view(RoleMenuView(configModule=config, menuKey=menuKey))
        buttonEntries = await listAllButtonRoleEntries()
        for messageId, entries in groupButtonRoleEntriesByMessage(buttonEntries).items():
            self.bot.add_view(ButtonRoleView(entries=entries), message_id=int(messageId))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channelId = int(getattr(config, "welcomeChannelId", 0) or 0)
        if channelId <= 0:
            return
        channel = member.guild.get_channel(channelId)
        if not isinstance(channel, discord.TextChannel):
            return
        template = str(
            getattr(
                config,
                "welcomeMessageTemplate",
                "Welcome to **{guild}**, {mention}.",
            )
            or "Welcome to **{guild}**, {mention}."
        )
        try:
            await channel.send(
                template.format(
                    mention=member.mention,
                    user=member.display_name,
                    guild=member.guild.name,
                )
            )
        except (discord.Forbidden, discord.HTTPException, KeyError):
            return

    # Legacy slash handler kept for future reuse.
    async def postRoleMenu(self, interaction: discord.Interaction, menu_key: str) -> None:
        if await self._requireAdminOrManageGuild(interaction) is None:
            return
        row = menuConfig(config, menu_key)
        if row is None:
            return await self._safeReply(interaction, "That role menu key is not configured.")
        title = str(row.get("title") or "Choose Your Roles").strip()[:256]
        description = str(row.get("description") or "Select the roles you want from the menu below.").strip()
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blurple(),
        )
        await self._safeReply(
            interaction,
            embed=embed,
            view=RoleMenuView(configModule=config, menuKey=menu_key),
        )

    async def _refreshButtonRoleMessage(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        messageId: int,
    ) -> bool:
        entries = await listButtonRoleEntries(messageId=messageId)
        if not entries:
            return False
        try:
            message = await channel.fetch_message(int(messageId))
            await message.edit(view=ButtonRoleView(entries=entries))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return False
        self.bot.add_view(ButtonRoleView(entries=entries), message_id=int(messageId))
        return True

    async def _isRoleAllowedForSelfAssign(
        self,
        *,
        guild: discord.Guild,
        role: discord.Role,
        botMember: discord.Member,
    ) -> bool:
        if await isBlockedSelfRole(guildId=int(guild.id), roleId=int(role.id)):
            return False
        return resolveAssignableRole(guild=guild, roleId=int(role.id), botMember=botMember) is not None

    async def _resolveBotMember(self, guild: discord.Guild) -> discord.Member | None:
        member = guild.me or guild.get_member(int(getattr(self.bot.user, "id", 0) or 0))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(getattr(self.bot.user, "id", 0) or 0))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None

    async def _createReactionRoleMessage(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        rows: list[tuple[discord.Role, str, str]],
    ) -> None:
        if not interaction.guild:
            await self._safeReply(interaction, "Use this command in a server.")
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._safeReply(interaction, "Use this command in a text channel or thread.")
            return
        botMember = await self._resolveBotMember(interaction.guild)
        if botMember is None:
            await self._safeReply(interaction, "Jane could not resolve her member record in this server.")
            return

        validRows: list[tuple[discord.Role, str, str]] = []
        seenRoleIds: set[int] = set()
        for index, (roleValue, labelValue, emojiValue) in enumerate(rows, start=1):
            safeLabel = str(labelValue or roleValue.name).strip()[:80]
            if not safeLabel:
                await self._safeReply(interaction, f"Slot {index} is missing a label.")
                return
            if int(roleValue.id) in seenRoleIds:
                await self._safeReply(interaction, f"Do not repeat {roleValue.mention} in the same message.")
                return
            if not await self._isRoleAllowedForSelfAssign(
                guild=interaction.guild,
                role=roleValue,
                botMember=botMember,
            ):
                await self._safeReply(interaction, f"{roleValue.mention} is blocked or I cannot manage it.")
                return
            validRows.append((roleValue, safeLabel, str(emojiValue or "").strip()))
            seenRoleIds.add(int(roleValue.id))

        if not validRows:
            await self._safeReply(interaction, "Add at least one role button.")
            return

        try:
            message = await channel.send(
                embed=self._buildReactionRoleEmbed(title=title, description=description),
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._safeReply(interaction, "I could not post the reaction-role message here.")
            return

        addedLines: list[str] = []
        for orderIndex, (roleValue, safeLabel, emojiSpec) in enumerate(validRows):
            await createButtonRoleEntry(
                guildId=int(interaction.guild.id),
                channelId=int(channel.id),
                messageId=int(message.id),
                roleId=int(roleValue.id),
                buttonLabel=safeLabel,
                emojiSpec=emojiSpec,
                orderIndex=orderIndex,
            )
            addedLines.append(f"{safeLabel} -> {roleValue.mention}")

        refreshed = await self._refreshButtonRoleMessage(channel=channel, messageId=int(message.id))
        if not refreshed:
            await self._safeReply(interaction, "The message was posted, but I could not attach the buttons.")
            return
        await self._safeReply(
            interaction,
            "Created reaction-role message "
            f"`{int(message.id)}` with {len(validRows)} button(s):\n" + "\n".join(addedLines),
        )

    async def runCreateReactionRoleButtons(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        rowsText: str,
    ) -> None:
        if await self._requireReactionRoleCommandAccess(interaction) is None:
            return
        if not interaction.guild:
            return await self._safeReply(interaction, "Use this command in a server.")
        rows: list[tuple[discord.Role, str, str]] = []
        for index, rawLine in enumerate(str(rowsText or "").splitlines(), start=1):
            line = str(rawLine or "").strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) > 3:
                await self._safeReply(interaction, f"Line {index} has too many `|` separators.")
                return
            roleId = _parsePositiveId(parts[0] if parts else "")
            role = interaction.guild.get_role(roleId) if roleId > 0 else None
            if role is None:
                await self._safeReply(interaction, f"Line {index} has an invalid role.")
                return
            label = parts[1] if len(parts) >= 2 and parts[1] else role.name
            emoji = parts[2] if len(parts) >= 3 else ""
            rows.append((role, label, emoji))
        if not rows:
            await self._safeReply(interaction, "Add at least one non-empty button row.")
            return
        if len(rows) > 7:
            await self._safeReply(interaction, "Limit the button list to 7 roles per message.")
            return
        await self._createReactionRoleMessage(
            interaction,
            title=title,
            description=description,
            rows=rows,
        )

    async def runRemoveReactionRoleButton(
        self,
        interaction: discord.Interaction,
        *,
        messageIdText: str,
        roleText: str,
    ) -> None:
        if await self._requireReactionRoleCommandAccess(interaction) is None:
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await self._safeReply(interaction, "Use this command in the channel that has the target message.")
        try:
            targetMessageId = int(str(messageIdText or "").strip())
        except (TypeError, ValueError):
            return await self._safeReply(interaction, "Message ID must be a number.")
        roleId = _parsePositiveId(roleText)
        if roleId <= 0:
            return await self._safeReply(interaction, "Role mention or role ID required.")
        await deleteButtonRoleEntry(messageId=targetMessageId, roleId=roleId)
        remainingEntries = await listButtonRoleEntries(messageId=targetMessageId)
        if remainingEntries:
            await self._refreshButtonRoleMessage(channel=channel, messageId=targetMessageId)
        else:
            try:
                message = await channel.fetch_message(targetMessageId)
                await message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        role = interaction.guild.get_role(roleId) if interaction.guild is not None else None
        roleMention = role.mention if role is not None else f"<@&{roleId}>"
        await self._safeReply(
            interaction,
            f"Removed {roleMention} from button-role message `{targetMessageId}`.",
        )

    async def runBlockReactionRole(
        self,
        interaction: discord.Interaction,
        *,
        roleText: str,
    ) -> None:
        if await self._requireReactionRolePolicyAccess(interaction) is None:
            return
        if not interaction.guild:
            return await self._safeReply(interaction, "Use this command in a server.")
        roleId = _parsePositiveId(roleText)
        role = interaction.guild.get_role(roleId)
        if role is None:
            return await self._safeReply(interaction, "Role mention or role ID required.")
        await addBlockedSelfRole(guildId=int(interaction.guild.id), roleId=int(role.id))
        await self._safeReply(interaction, f"Blocked {role.mention} from future reaction-role buttons.")

    async def runUnblockReactionRole(
        self,
        interaction: discord.Interaction,
        *,
        roleText: str,
    ) -> None:
        if await self._requireReactionRolePolicyAccess(interaction) is None:
            return
        if not interaction.guild:
            return await self._safeReply(interaction, "Use this command in a server.")
        roleId = _parsePositiveId(roleText)
        role = interaction.guild.get_role(roleId)
        if role is None:
            return await self._safeReply(interaction, "Role mention or role ID required.")
        await removeBlockedSelfRole(guildId=int(interaction.guild.id), roleId=int(role.id))
        await self._safeReply(interaction, f"Unblocked {role.mention} for reaction-role buttons.")

    async def listBlockedReactionRoles(self, interaction: discord.Interaction) -> None:
        if await self._requireReactionRolePolicyAccess(interaction) is None:
            return
        if not interaction.guild:
            return await self._safeReply(interaction, "Use this command in a server.")
        rows = await listBlockedSelfRoles(guildId=int(interaction.guild.id))
        if not rows:
            return await self._safeReply(interaction, "No reaction-role blocks are configured.")
        description = "\n".join(
            interaction.guild.get_role(int(row.get("roleId") or 0)).mention
            if interaction.guild.get_role(int(row.get("roleId") or 0)) is not None
            else f"<@&{int(row.get('roleId') or 0)}>"
            for row in rows
        )[:4000]
        embed = discord.Embed(
            title="Blocked Reaction Roles",
            description=description,
            color=discord.Color.blurple(),
        )
        await self._safeReply(interaction, embed=embed)

    @app_commands.command(name="reaction-role", description="Open the reaction-role control panel.")
    async def reactionRolePanel(self, interaction: discord.Interaction) -> None:
        member = await self._requireGuildMember(interaction)
        if member is None:
            return
        canManageButtons = self._canManageReactionRoleButtons(member)
        canManagePolicy = self._canManageReactionRolePolicy(member)
        canPostMenus = runtimePermissions.hasAdminOrManageGuild(member)
        if not canManageButtons and not canManagePolicy and not canPostMenus:
            await self._safeReply(interaction, "You do not have permission to manage reaction roles.")
            return
        await self._safeReply(
            interaction,
            embed=self._buildReactionRolePanelEmbed(),
            view=ReactionRolePanelView(
                self,
                openerId=int(member.id),
                canManageButtons=canManageButtons,
                canManagePolicy=canManagePolicy,
                canPostMenus=canPostMenus,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PublicUtilityCog(bot))
