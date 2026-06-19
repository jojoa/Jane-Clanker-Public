import asyncio
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands, Interaction, TextChannel, Message, channel
from discord import Embed
import mcrcon
from runtime import permissions as runtimePermissions
import runtime.interaction as interactionRuntime
import logging

from db.sqlite import fetchOne, execute
from settings.community import minecraftRCONAddress, minecraftAuthenticationToken, minecraftRCONPort, \
    minecraftAllowedRoleIds, minecraftCheckCooldownSeconds

log = logging.getLogger(__name__)

async def buildEmbed(TPS, playerCount, Status) -> Embed | None:
    color = None
    if Status == "Online":
        color = discord.Color.green()

    if Status == "Offline":
        color = discord.Color.red()

    if Status == "Maintenance":
        color = discord.Color.blue()

    embed = Embed(title="Minecraft Server Status",
                          colour=color)

    embed.add_field(name="Current TPS:",
                    value=f"{TPS}",
                    inline=True)
    embed.add_field(name="Current player count:",
                    value=f"{playerCount}/60",
                    inline=True)
    embed.add_field(name="Online Status:",
                    value=f"{Status}",
                    inline=False)

    return embed

def canRunCommand(member: discord.Member) -> bool:
    hasrole = runtimePermissions.hasAnyRole(member, minecraftAllowedRoleIds)
    return hasrole

class MinecraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status = "Offline"
        self.statusMessageID = 0
        self.statusChannelID = 0
        self.playerCount = 0
        self.TPS = 0
        self.connection = None
        self.loopTask = None

    async def _safeEphemeral(self, interaction: Interaction, message: str) -> None:
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=message,
            ephemeral=True,
        )

    async def _messageAlreadyMade(self, interaction: Interaction) -> bool:
        if self.statusMessageID != 0:
            await self._safeEphemeral(interaction, "Sorry, a status message has already been created.")
            return True
        return False


    async def _getStatusMessageAndChanneldb(self):
        return await fetchOne("""
        SELECT statusMessageId, statusChannelId
        FROM mcstatus
        """)

    async def _updateDatabase(self):
        await execute("""
        UPDATE mcstatus
        SET statusMessageId = ?, statusChannelId = ?, lastPlayerCount = ?, lastStatus = ?
        """,(self.statusMessageID, self.statusChannelID, self.playerCount, self.status))

    async def _createDatabase(self):
        await execute("""
        INSERT INTO mcstatus
        (statusMessageId, statusChannelId, lastPlayerCount, lastStatus, lastMaintenanceDate)
        VALUES (?, ?, ?, ?, datetime('now'))
        """,(self.statusMessageID, self.statusChannelID, self.playerCount, self.status))

    async def _startMaintenanceDatabaseUpdate(self):
        await execute("""
        UPDATE mcstatus
        SET lastMaintenanceDate = datetime('now'), lastStatus = 'Maintenance'
        WHERE statusMessageId = ?
        """, (self.statusMessageID,))

    async def _setOnlineModeDatabaseUpdate(self):
        await execute("""
        UPDATE mcstatus
        SET lastStatus = ?
        WHERE statusMessageId = ?
        """, (self.status,self.statusMessageID))

    async def _deleteMessageEntryDatabase(self):
        await execute("""
        DELETE FROM mcstatus;
        """)

    async def _getStatusMessageAndUpdateSelfVariables(self) -> None | discord.Message:
        row = await self._getStatusMessageAndChanneldb()
        log.info(row)
        if row is None:
            log.warning("Unable to fetch status message.")
            return None
        messageID = row["statusMessageId"]
        channelID = row["statusChannelId"]
        if messageID is None:
            log.warning("No messageID found in database.")
            return None

        if channelID is None:
            log.warning("No channelID found in database.")
            return None

        self.statusMessageID = messageID
        self.statusChannelID = channelID
        channelInner = self.bot.get_channel(self.statusChannelID)
        if channelInner is None:
            log.warning("Sorry, I was unable to find your channel.")
            return None

        message = await channelInner.fetch_message(messageID)
        if message is None:
            log.warning("Sorry, I was unable to find your message.")
            return None
        return message

    def _getPlayerCount(self):
        responsePlayerCount:str = self.connection.command("/list")
        return responsePlayerCount[10:12].removesuffix(" ")

    def _getTPS(self):
        responseTPS = self.connection.command("/forge tps")
        return responseTPS[-7:]

    def _setConnection(self):
        self.connection = mcrcon.MCRcon(host=minecraftRCONAddress,password=minecraftAuthenticationToken, port=minecraftRCONPort)

    def _startConnection(self):
        self.connection.connect()

    def _stopConnection(self):
        self.connection.disconnect()

    def _buildEmbed(self):
        return buildEmbed(self.TPS,self.playerCount,self.status)

    async def _loopFunction(self):
        activeMessage = await self._getStatusMessageAndUpdateSelfVariables()
        if activeMessage is None:
            log.warning("No active message found in database.")
            return

        self.statusMessageID = activeMessage.id
        self.statusChannelID = activeMessage.channel.id

        self._setConnection()
        if self.connection is None:
            log.warning("There was an error connecting to the Minecraft server.")
            self.status = "Offline"
            return
        self.status = "Online"
        self._startConnection()
        self.playerCount = self._getPlayerCount()
        self.TPS = self._getTPS()
        self._stopConnection()
        await activeMessage.edit(embed=await self._buildEmbed())

    async def _loop(self):
        try:
            while True:
                await self._loopFunction()
                await asyncio.sleep(minecraftCheckCooldownSeconds)
        except Exception as e:
            log.exception("An exception occurred while trying to connect to the Minecraft server.")

    async def run(self, channel: discord.TextChannel) -> None:
        #Checks if we aready have a message to edit or not.
        activeMessage = await self._getStatusMessageAndUpdateSelfVariables()
        if activeMessage is None:
            log.warning("No active message found in database.")
            await self._createDatabase()
            await self._sendStartingMessageAndUpdateDatabase(channel)

        await asyncio.sleep(minecraftCheckCooldownSeconds)
        self.loopTask = asyncio.create_task(self._loop(), name="minecraft-server-check")

    #yes, I copied this :P
    async def stop(self):
        task = self.loopTask
        if task is None:
            return
        self.loopTask = None
        if task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    #ultility :P
    async def _currentMessageActive(self) -> bool:
        if self.statusMessageID != 0:
            log.warning("There has already been a MC status message set.")
            return True

        if self.statusChannelID != 0:
            log.warning("There has already been a MC status channel set.")
            return True
        return False

    #When this is run it assumes the message has already been registered in teh database!
    async def _sendStartingMessageAndUpdateDatabase(self, channel : discord.TextChannel) -> None:
        embed = await buildEmbed(0,0,"Offline")
        if embed is None:
            log.warning("There was an error sending the starting message.")
            return
        message = await channel.send(embed=embed)
        log.info("Minecraft status message sent.")
        self.statusMessageID = message.id
        self.statusChannelID = message.channel.id
        await self._updateDatabase()


    @app_commands.command(name="register-status-channel", description="Registers the given channel as a minecraft status channel.")
    async def register_status_channel(self, interaction: Interaction, channel: discord.TextChannel):
        allowed = canRunCommand(member=interaction.user)
        if not allowed:
            await self._safeEphemeral(interaction, "You can't unregister this channel.")
            return
        await self.run(channel)

    @app_commands.command(name="unregister-status-channel", description="Unregisters the given channel as a minecraft status channel.")
    async def unregister_status_channel(self, interaction: Interaction, channel: discord.TextChannel):
        allowed = canRunCommand(member=interaction.user)
        if not allowed:
            await self._safeEphemeral(interaction, "You can't unregister this channel.")
            return
        try:
            await self.stop()
        except Exception:
            log.warning("There was an error unregistering the status channel.")

        message = await self._getStatusMessageAndUpdateSelfVariables()
        if message is None:
            log.warning("There was an error deleting the status message.")
            await self._safeEphemeral(interaction, "There was an error deleting the status message.")
            return
        await self._deleteMessageEntryDatabase()
        await message.delete()
        self.status = "Offline"
        self.statusMessageID = 0
        self.statusChannelID = 0
        self.playerCount = 0
        self.TPS = 0
        self.connection = None
        self.loopTask = None

    @app_commands.command(name="restart-minecraft-status",description="Restarts the Minecraft server status.")
    async def restart_minecraft_status(self, interaction: Interaction):
        allowed = canRunCommand(member=interaction.user)
        if not allowed:
            await self._safeEphemeral(interaction, "You can't unregister this channel.")
            return
        message = await self._getStatusMessageAndUpdateSelfVariables()
        if message is None:
            log.warning("There was an error restarting the status message.")
            await self._safeEphemeral(interaction, "There was an error restarting the status message.")
            return
        await self.run(message.channel)

    @app_commands.command(name="delete-minecraft-database-status", description="You should not be here.")
    async def delete_minecraft_database_status(self, interaction: Interaction):
        allowed = canRunCommand(member=interaction.user)
        if not allowed:
            await self._safeEphemeral(interaction, "You can't unregister this channel.")
            return
        await self._deleteMessageEntryDatabase()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftCog(bot))