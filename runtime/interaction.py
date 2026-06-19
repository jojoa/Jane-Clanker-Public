from __future__ import annotations

import logging
import socket
import ssl
from typing import Any, Optional

import aiohttp
import discord

from . import taskBudgeter

log = logging.getLogger(__name__)

_MISSING = object()
_retrySafeLayerInstalled = False
_SAFE_DISCORD_EXCEPTIONS = (discord.NotFound, discord.Forbidden, discord.HTTPException)
_SAFE_CALLER_EXCEPTIONS = (AttributeError, TypeError)
_SAFE_MESSAGE_DELETE_EXCEPTIONS = _SAFE_DISCORD_EXCEPTIONS + (AttributeError,)
_SAFE_CHANNEL_SEND_EXCEPTIONS = _SAFE_DISCORD_EXCEPTIONS + _SAFE_CALLER_EXCEPTIONS


class InteractionDeferFailed(RuntimeError):
    pass


def _logSafeFailure(action: str, exc: Exception) -> None:
    log.debug("Discord %s failed safely: %s", action, exc)


def _logSafeTransportFailure(action: str, exc: Exception) -> None:
    log.warning(
        "Discord %s hit transient transport failure: %s: %s",
        action,
        exc.__class__.__name__,
        exc,
    )


def _shouldRetryReplyWithoutView(exc: TypeError) -> bool:
    return "view" in str(exc).lower()


def _isTransientTransportError(exc: Exception) -> bool:
    transientMarkers = (
        "wrong version number",
        "cipher operation failed",
        "bad record mac",
        "unexpected eof",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "session is closed",
        "tls",
        "ssl",
    )

    current: Exception | None = exc
    while current is not None:
        if isinstance(
            current,
            (
                aiohttp.ClientError,
                ssl.SSLError,
                socket.timeout,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return True
        if isinstance(current, OSError):
            lowerMessage = str(current).lower()
            if any(marker in lowerMessage for marker in transientMarkers):
                return True
        lowerMessage = str(current).lower()
        if any(marker in lowerMessage for marker in transientMarkers):
            return True
        current = current.__cause__ if isinstance(current.__cause__, Exception) else None
    return False


def _isSafeTransportFailure(exc: Exception) -> bool:
    return _isTransientTransportError(exc)


def isUnknownInteractionError(exc: Exception) -> bool:
    if isinstance(exc, discord.NotFound):
        return True
    if isinstance(exc, discord.HTTPException):
        if getattr(exc, "code", None) == 10062:
            return True
        text = str(exc).lower()
        return "unknown interaction" in text
    return False


async def safeInteractionDefer(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
    thinking: bool = False,
    raiseOnFailure: bool = True,
) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except (discord.NotFound, discord.HTTPException) as exc:
        _logSafeFailure("interaction defer", exc)
        if raiseOnFailure:
            raise InteractionDeferFailed("Interaction could not be acknowledged before it expired.") from exc
        return False
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("interaction defer", exc)
            if raiseOnFailure:
                raise InteractionDeferFailed("Interaction could not be acknowledged due to a Discord transport error.") from exc
            return False
        raise


async def safeInteractionReply(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    embeds: Optional[list[discord.Embed]] = None,
    file: Optional[discord.File] = None,
    view: Any = _MISSING,
    ephemeral: bool = True,
    allowedMentions: Any = _MISSING,
) -> bool:
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if embeds is not None:
        kwargs["embeds"] = embeds
    if file is not None:
        kwargs["file"] = file
    if view is not _MISSING and isinstance(view, discord.ui.View):
        kwargs["view"] = view
    if allowedMentions is not _MISSING:
        kwargs["allowed_mentions"] = allowedMentions

    async def _dispatch() -> None:
        if interaction.response.is_done():
            await taskBudgeter.runInteractiveDiscord(lambda: interaction.followup.send(**kwargs))
        else:
            await interaction.response.send_message(**kwargs)

    try:
        await _dispatch()
        return True
    except TypeError as exc:
        # Common case: unsupported kwargs for a specific response path.
        if "view" in kwargs and _shouldRetryReplyWithoutView(exc):
            kwargs.pop("view", None)
            try:
                await _dispatch()
                return True
            except TypeError as retryExc:
                log.warning("Interaction reply type error after dropping view: %s", retryExc)
                return False
            except (discord.NotFound, discord.HTTPException) as retryExc:
                _logSafeFailure("interaction reply retry", retryExc)
                return False
        log.warning("Interaction reply type error: %s", exc)
        return False
    except (discord.NotFound, discord.HTTPException) as exc:
        _logSafeFailure("interaction reply", exc)
        return False
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("interaction reply", exc)
            return False
        raise


async def safeInteractionSendModal(
    interaction: discord.Interaction,
    modal: discord.ui.Modal,
    *,
    expiredMessage: str = "This interaction expired. Please run the command again.",
) -> bool:
    if interaction.response.is_done():
        await safeInteractionReply(
            interaction,
            content=expiredMessage,
            ephemeral=True,
        )
        return False
    try:
        await interaction.response.send_modal(modal)
        return True
    except (discord.NotFound, discord.HTTPException) as exc:
        _logSafeFailure("interaction modal", exc)
        return False
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("interaction modal", exc)
            return False
        raise


async def safeMessageEdit(message: discord.Message, **kwargs: Any) -> bool:
    try:
        await taskBudgeter.runInteractiveDiscord(lambda: message.edit(**kwargs))
        return True
    except _SAFE_DISCORD_EXCEPTIONS as exc:
        _logSafeFailure("message edit", exc)
        return False
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("message edit", exc)
            return False
        raise


async def safeMessageDelete(message: discord.Message) -> bool:
    try:
        await taskBudgeter.runInteractiveDiscord(lambda: message.delete())
        return True
    except _SAFE_MESSAGE_DELETE_EXCEPTIONS as exc:
        _logSafeFailure("message delete", exc)
        return False
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("message delete", exc)
            return False
        raise


async def safeChannelSend(channel: Any, **kwargs: Any) -> discord.Message | None:
    try:
        return await taskBudgeter.runInteractiveDiscord(lambda: channel.send(**kwargs))
    except _SAFE_CHANNEL_SEND_EXCEPTIONS as exc:
        _logSafeFailure("channel send", exc)
        return None
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("channel send", exc)
            return None
        raise


async def safeFetchChannel(botOrGuild: Any, channelId: int) -> Any | None:
    try:
        normalizedChannelId = int(channelId or 0)
    except (TypeError, ValueError):
        return None
    if normalizedChannelId <= 0:
        return None
    getChannel = getattr(botOrGuild, "get_channel", None)
    if callable(getChannel):
        try:
            cached = getChannel(normalizedChannelId)
        except Exception:
            cached = None
        if cached is not None:
            return cached
    fetchChannel = getattr(botOrGuild, "fetch_channel", None)
    if not callable(fetchChannel):
        return None
    try:
        return await taskBudgeter.runInteractiveDiscord(lambda: fetchChannel(normalizedChannelId))
    except _SAFE_DISCORD_EXCEPTIONS + (discord.InvalidData, TypeError, ValueError) as exc:
        _logSafeFailure("fetch channel", exc)
        return None
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("fetch channel", exc)
            return None
        raise


async def safeFetchMessage(channel: Any, messageId: int) -> discord.Message | None:
    try:
        normalizedMessageId = int(messageId or 0)
    except (TypeError, ValueError):
        return None
    if normalizedMessageId <= 0 or not hasattr(channel, "fetch_message"):
        return None
    try:
        return await taskBudgeter.runInteractiveDiscord(lambda: channel.fetch_message(normalizedMessageId))
    except _SAFE_DISCORD_EXCEPTIONS + _SAFE_CALLER_EXCEPTIONS as exc:
        _logSafeFailure("fetch message", exc)
        return None
    except Exception as exc:
        if _isSafeTransportFailure(exc):
            _logSafeTransportFailure("fetch message", exc)
            return None
        raise


def _makeRetrySafeResponseMethod(originalMethod: Any, action: str) -> Any:
    async def _patched(self: discord.InteractionResponse, *args: Any, **kwargs: Any) -> Any:
        try:
            return await taskBudgeter.runInteractionAck(lambda: originalMethod(self, *args, **kwargs))
        except (discord.NotFound, discord.HTTPException) as exc:
            if isUnknownInteractionError(exc):
                _logSafeFailure(action, exc)
                return None
            raise

    return _patched


def installRetrySafeInteractionLayer() -> None:
    global _retrySafeLayerInstalled
    if _retrySafeLayerInstalled:
        return

    originalSendMessage = discord.InteractionResponse.send_message
    originalDefer = discord.InteractionResponse.defer
    originalSendModal = discord.InteractionResponse.send_modal
    originalEditMessage = discord.InteractionResponse.edit_message

    discord.InteractionResponse.send_message = _makeRetrySafeResponseMethod(  # type: ignore[assignment]
        originalSendMessage,
        "interaction response send_message",
    )
    discord.InteractionResponse.defer = _makeRetrySafeResponseMethod(  # type: ignore[assignment]
        originalDefer,
        "interaction response defer",
    )
    discord.InteractionResponse.send_modal = _makeRetrySafeResponseMethod(  # type: ignore[assignment]
        originalSendModal,
        "interaction response send_modal",
    )
    discord.InteractionResponse.edit_message = _makeRetrySafeResponseMethod(  # type: ignore[assignment]
        originalEditMessage,
        "interaction response edit_message",
    )

    _retrySafeLayerInstalled = True
