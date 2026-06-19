from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

import discord
from discord import app_commands
from discord.ext import commands

from . import interaction as interactionRuntime
from . import transientNetwork

log = logging.getLogger(__name__)
_ERROR_MIRROR_HANDLER_NAME = "jane-error-mirror-dm"


class ErrorCoordinator:
    def __init__(
        self,
        *,
        botClient: Any,
        configModule: Any,
        taskBudgeter: Any,
        retryQueue: Any | None = None,
    ) -> None:
        self.botClient = botClient
        self.config = configModule
        self.taskBudgeter = taskBudgeter
        self.retryQueue = retryQueue

    def _errorMirrorUserId(self) -> int | None:
        configured = getattr(self.config, "errorMirrorUserId", 0)
        try:
            parsed = int(configured)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed

        fallbackIds = getattr(self.config, "temporaryCommandAllowedUserIds", []) or []
        for raw in fallbackIds:
            try:
                candidate = int(raw)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return candidate
        return None

    @staticmethod
    def _truncateForDiscord(value: str, maxLen: int) -> str:
        if len(value) <= maxLen:
            return value
        if maxLen <= 3:
            return value[:maxLen]
        return value[: maxLen - 3] + "..."

    async def _sendMirrorEmbed(
        self,
        *,
        content: str,
        embed: discord.Embed,
        retrySource: str,
    ) -> None:
        targetUserId = self._errorMirrorUserId()
        if not targetUserId:
            return

        targetUser = self.botClient.get_user(targetUserId)
        if targetUser is None:
            try:
                targetUser = await self.taskBudgeter.runDiscord(lambda: self.botClient.fetch_user(targetUserId))
            except Exception:
                await self._enqueueMirrorRetry(
                    targetUserId=targetUserId,
                    content=content,
                    embed=embed,
                    retrySource=retrySource,
                )
                return
        if targetUser is None:
            return

        try:
            await self.taskBudgeter.runDiscord(
                lambda: targetUser.send(
                    content=content or None,
                    embed=embed,
                )
            )
        except Exception:
            await self._enqueueMirrorRetry(
                targetUserId=targetUserId,
                content=content,
                embed=embed,
                retrySource=retrySource,
            )
            return

    async def _enqueueMirrorRetry(
        self,
        *,
        targetUserId: int,
        content: str,
        embed: discord.Embed,
        retrySource: str,
    ) -> None:
        if self.retryQueue is None:
            return
        try:
            await self.retryQueue.enqueue(
                jobType="error-mirror-dm",
                payload={
                    "targetUserId": int(targetUserId),
                    "content": str(content or ""),
                    "title": str(embed.title or ""),
                    "description": str(embed.description or ""),
                },
                maxAttempts=6,
                initialDelaySec=10,
                source=retrySource,
            )
        except Exception:
            pass

    async def sendErrorMirrorDm(
        self,
        *,
        source: str,
        commandName: str,
        userId: object,
        guildId: object,
        error: Exception,
    ) -> None:
        tracebackText = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        tracebackText = self._truncateForDiscord(tracebackText.strip() or repr(error), 3400)

        embed = discord.Embed(
            title="Jane Error Mirror",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
            description=f"```py\n{tracebackText}\n```",
        )
        embed.add_field(name="Source", value=source or "unknown", inline=True)
        embed.add_field(name="Command", value=(commandName or "unknown"), inline=True)
        embed.add_field(name="User ID", value=str(userId), inline=True)
        embed.add_field(name="Guild ID", value=str(guildId), inline=True)
        embed.set_footer(text="Mirrored from terminal exception log")

        await self._sendMirrorEmbed(
            content="================ Jane Error Log ================",
            embed=embed,
            retrySource="error-coordinator",
        )

    async def sendLoggedErrorMirrorDm(
        self,
        *,
        loggerName: str,
        levelName: str,
        message: str,
        renderedError: str,
    ) -> None:
        renderedText = self._truncateForDiscord(str(renderedError or "").strip() or "(no traceback)", 3200)
        description = (
            f"**Logger:** `{self._truncateForDiscord(str(loggerName or 'unknown'), 120)}`\n"
            f"**Level:** `{self._truncateForDiscord(str(levelName or 'ERROR'), 40)}`\n"
            f"**Message:** {self._truncateForDiscord(str(message or 'unknown'), 500)}\n"
            f"```py\n{renderedText}\n```"
        )
        embed = discord.Embed(
            title="Jane Logged Error",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
            description=self._truncateForDiscord(description, 3900),
        )
        embed.set_footer(text="Mirrored from Python logging")
        await self._sendMirrorEmbed(
            content="================ Jane Error Log ================",
            embed=embed,
            retrySource="logged-error-mirror",
        )

    async def handleAppCommandError(
        self,
        *,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        safeInteractionSend: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        underlying = error.original if isinstance(error, app_commands.CommandInvokeError) else error

        if isinstance(underlying, interactionRuntime.InteractionDeferFailed):
            log.info(
                "App command interaction expired before work could start (userId=%s guildId=%s).",
                getattr(interaction.user, "id", "unknown"),
                getattr(interaction.guild, "id", "dm"),
                extra={"skipErrorMirrorDm": True},
            )
            return
        if isinstance(underlying, app_commands.CommandOnCooldown):
            return await safeInteractionSend(
                interaction,
                f"That command is on cooldown. Try again in {underlying.retry_after:.1f}s.",
            )
        if isinstance(underlying, app_commands.MissingPermissions):
            return await safeInteractionSend(
                interaction,
                "You do not have permission to use that command.",
            )
        if isinstance(underlying, app_commands.CheckFailure):
            return await safeInteractionSend(
                interaction,
                "You are not allowed to use that command in this context.",
            )
        if isinstance(underlying, app_commands.TransformerError):
            return await safeInteractionSend(
                interaction,
                "One or more inputs are invalid. Please review your command fields and try again.",
            )

        commandName = ""
        if isinstance(interaction.data, dict):
            commandName = str(interaction.data.get("name") or "")
        log.exception(
            "Unhandled app command error (command=%s, userId=%s, guildId=%s).",
            commandName or "unknown",
            getattr(interaction.user, "id", "unknown"),
            getattr(interaction.guild, "id", "dm"),
            extra={"skipErrorMirrorDm": True},
        )
        await self.sendErrorMirrorDm(
            source="app-command",
            commandName=commandName or "unknown",
            userId=getattr(interaction.user, "id", "unknown"),
            guildId=getattr(interaction.guild, "id", "dm"),
            error=underlying if isinstance(underlying, Exception) else Exception(str(underlying)),
        )
        await safeInteractionSend(
            interaction,
            "That command failed due to an internal error. Please try again in a moment.",
        )

    async def handlePrefixCommandError(
        self,
        *,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            return
        log.exception(
            "Unhandled prefix command error (command=%s, userId=%s, guildId=%s).",
            getattr(getattr(ctx, "command", None), "qualified_name", "unknown"),
            getattr(getattr(ctx, "author", None), "id", "unknown"),
            getattr(getattr(ctx, "guild", None), "id", "dm"),
            extra={"skipErrorMirrorDm": True},
        )
        await self.sendErrorMirrorDm(
            source="prefix-command",
            commandName=str(getattr(getattr(ctx, "command", None), "qualified_name", "unknown")),
            userId=getattr(getattr(ctx, "author", None), "id", "unknown"),
            guildId=getattr(getattr(ctx, "guild", None), "id", "dm"),
            error=error if isinstance(error, Exception) else Exception(str(error)),
        )


class ErrorMirrorLogHandler(logging.Handler):
    def __init__(self, *, coordinator: ErrorCoordinator, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(level=logging.ERROR)
        self.name = _ERROR_MIRROR_HANDLER_NAME
        self.coordinator = coordinator
        self.loop = loop
        self._formatter = logging.Formatter()

    def _renderRecord(self, record: logging.LogRecord) -> str:
        if record.exc_info:
            return self._formatter.formatException(record.exc_info)
        if record.stack_info:
            return str(record.stack_info)
        return record.getMessage()

    def _isTransientNetworkRecord(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, BaseException) and transientNetwork.isLikelyTransientNetworkError(exc):
                return True
        return transientNetwork.textLooksLikeTransientNetworkError(record.getMessage())

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "skipErrorMirrorDm", False):
            return
        if self._isTransientNetworkRecord(record):
            return
        if self.loop.is_closed():
            return
        try:
            loggerName = str(record.name or "unknown")
            levelName = str(record.levelname or "ERROR")
            message = record.getMessage()
            renderedError = self._renderRecord(record)

            def _schedule() -> None:
                try:
                    task = self.loop.create_task(
                        self.coordinator.sendLoggedErrorMirrorDm(
                            loggerName=loggerName,
                            levelName=levelName,
                            message=message,
                            renderedError=renderedError,
                        )
                    )
                    task.add_done_callback(self._consumeTaskException)
                except Exception:
                    pass

            self.loop.call_soon_threadsafe(_schedule)
        except Exception:
            pass

    @staticmethod
    def _consumeTaskException(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass


def installErrorMirrorLogging(
    *,
    coordinator: ErrorCoordinator,
    loop: asyncio.AbstractEventLoop,
) -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "name", "") == _ERROR_MIRROR_HANDLER_NAME:
            if isinstance(handler, ErrorMirrorLogHandler):
                handler.coordinator = coordinator
                handler.loop = loop
            return
    root.addHandler(ErrorMirrorLogHandler(coordinator=coordinator, loop=loop))
