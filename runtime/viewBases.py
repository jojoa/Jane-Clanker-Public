from __future__ import annotations

from typing import Any

import discord

from runtime import interaction as interactionRuntime


async def safeRefreshInteractionMessage(
    interaction: discord.Interaction,
    **kwargs: Any,
) -> None:
    if interaction.response.is_done():
        try:
            await interaction.edit_original_response(**kwargs)
            return
        except (discord.NotFound, discord.HTTPException, AttributeError, TypeError):
            pass
        except Exception as exc:
            if not interactionRuntime._isSafeTransportFailure(exc):
                raise
        if interaction.message is not None:
            await interactionRuntime.safeMessageEdit(interaction.message, **kwargs)
        return

    try:
        await interaction.response.edit_message(**kwargs)
    except (discord.NotFound, discord.HTTPException):
        if interaction.message is not None:
            await interactionRuntime.safeMessageEdit(interaction.message, **kwargs)
    except Exception as exc:
        if not interactionRuntime._isSafeTransportFailure(exc):
            raise
        if interaction.message is not None:
            await interactionRuntime.safeMessageEdit(interaction.message, **kwargs)


class OwnerLockedView(discord.ui.View):
    def __init__(
        self,
        *,
        openerId: int,
        timeout: float | None = 900,
        ownerMessage: str = "This panel belongs to someone else.",
    ) -> None:
        super().__init__(timeout=timeout)
        self.openerId = int(openerId)
        self.ownerMessage = str(ownerMessage or "This panel belongs to someone else.")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.openerId:
            return True
        await interactionRuntime.safeInteractionReply(
            interaction,
            content=self.ownerMessage,
            ephemeral=True,
        )
        return False
