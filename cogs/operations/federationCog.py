from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from features.operations.federation import service as federationService
from runtime import cogGuards as runtimeCogGuards
from runtime import commandScopes as runtimeCommandScopes


class FederationCog(runtimeCogGuards.InteractionGuardMixin, commands.Cog):

    # Legacy slash handler kept for future reuse.
    async def federationLink(
        self,
        interaction: discord.Interaction,
        linked_guild_id: str,
        link_type: app_commands.Choice[str],
        linked_guild_id_2: str = "",
        note: str = "",
    ) -> None:
        member = await self._requireAdminOrManageGuild(interaction)
        if member is None:
            return
        if not await self._requireTestGuild(interaction):
            return

        rawValues = [str(linked_guild_id or "").strip(), str(linked_guild_id_2 or "").strip()]
        linkedGuildIds: list[int] = []
        for rawValue in rawValues:
            if not rawValue:
                continue
            try:
                parsedValue = int(rawValue)
            except (TypeError, ValueError):
                return await self._safeReply(interaction, f"Invalid linked guild ID: `{rawValue}`.")
            if parsedValue <= 0:
                return await self._safeReply(interaction, f"Invalid linked guild ID: `{rawValue}`.")
            if parsedValue == int(member.guild.id):
                return await self._safeReply(interaction, "You can't federate a guild to itself.")
            if parsedValue not in linkedGuildIds:
                linkedGuildIds.append(parsedValue)

        if not linkedGuildIds:
            return await self._safeReply(interaction, "At least one linked guild ID is required.")

        for linkedGuildId in linkedGuildIds:
            await federationService.addFederationLink(
                guildId=int(member.guild.id),
                linkedGuildId=linkedGuildId,
                linkType=link_type.value,
                note=note,
                createdBy=int(member.id),
            )

        linkedSummary = ", ".join(f"`{linkedGuildId}`" for linkedGuildId in linkedGuildIds)
        await self._safeReply(
            interaction,
            f"Linked this guild to {linkedSummary} as `{link_type.value}`.",
        )

    # Legacy slash handler kept for future reuse.
    async def federationUnlink(self, interaction: discord.Interaction, linked_guild_id: str) -> None:
        member = await self._requireAdminOrManageGuild(interaction)
        if member is None:
            return
        if not await self._requireTestGuild(interaction):
            return
        try:
            linkedGuildId = int(str(linked_guild_id or "").strip())
        except (TypeError, ValueError):
            return await self._safeReply(interaction, "Invalid linked guild ID.")
        await federationService.removeFederationLink(
            guildId=int(member.guild.id),
            linkedGuildId=linkedGuildId,
        )
        await self._safeReply(interaction, f"Removed federation link to `{linkedGuildId}` if it existed.")

    # Legacy slash handler kept for future reuse.
    async def federationList(self, interaction: discord.Interaction) -> None:
        member = await self._requireAdminOrManageGuild(interaction)
        if member is None:
            return
        if not await self._requireTestGuild(interaction):
            return
        rows = await federationService.listFederationLinks(guildId=int(member.guild.id))
        if not rows:
            return await self._safeReply(interaction, "No federation links configured for this guild.")
        lines = [
            f"`{row.get('linkedGuildId')}` - `{row.get('linkType')}`"
            + (f" - {str(row.get('note') or '').strip()}" if str(row.get("note") or "").strip() else "")
            for row in rows[:20]
        ]
        await self._safeReply(interaction, "Federation links:\n" + "\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FederationCog(), guilds=runtimeCommandScopes.getTestGuildObjects())
