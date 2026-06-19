from __future__ import annotations

from typing import Any

import discord

_deps: dict[str, Any] = {}


def configure(**deps: Any) -> None:
    _deps.update(deps)


def _dep(name: str) -> Any:
    value = _deps.get(name)
    if value is None:
        raise RuntimeError(f"bgOutfitsFlow dependency not configured: {name}")
    return value


async def sendBgOutfitsForTarget(
    interaction: discord.Interaction,
    sessionId: int,
    targetUserId: int,
) -> None:
    await _dep("safeInteractionDefer")(interaction, ephemeral=True)
    attendees = _dep("bgCandidates")(await _dep("service").getAttendees(sessionId))
    attendee = next((row for row in attendees if row["userId"] == targetUserId), None)
    if attendee is None:
        await _dep("safeInteractionReply")(
            interaction,
            content="This attendee is no longer present in the background-check queue.",
            ephemeral=True,
        )
        return

    identity = await _dep("resolveRobloxIdentity")(attendee)
    await _dep("scanRobloxOutfitsForAttendee")(
        sessionId,
        attendee,
        identity,
        forceRescan=True,
    )
    attendee = await _dep("service").getAttendee(sessionId, targetUserId) or attendee

    status = attendee.get("robloxOutfitScanStatus")
    error = attendee.get("robloxOutfitScanError")
    if status == "NO_ROVER":
        await _dep("safeInteractionReply")(
            interaction,
            content="No linked Roblox account found for this attendee. Ask them to run `/verify`.",
            ephemeral=True,
        )
        return
    if status == "ERROR":
        note = f"Outfit scan failed: {error}" if error else "Outfit scan failed."
        await _dep("safeInteractionReply")(interaction, content=note, ephemeral=True)
        return

    outfits = _dep("loadJsonList")(attendee.get("robloxOutfitsJson"))
    if not outfits:
        await _dep("safeInteractionReply")(
            interaction,
            content="No outfits found or this user's outfits are private.",
            ephemeral=True,
        )
        return

    outfitIds = [o.get("id") for o in outfits if isinstance(o, dict) and o.get("id") is not None]
    outfitIds = [oid for oid in outfitIds if isinstance(oid, int)]
    thumbResult = await _dep("robloxModule").fetchRobloxOutfitThumbnails(outfitIds, size="150x150")
    thumbs = {t.get("id"): t.get("imageUrl") for t in thumbResult.thumbnails if isinstance(t, dict)}

    pageSize = 10
    view = _dep("outfitPageViewClass")(
        sessionId,
        targetUserId,
        outfits,
        thumbs,
        interaction.user.id,
        safeReply=_dep("safeInteractionReply"),
        buildOutfitPageEmbeds=_dep("buildOutfitPageEmbeds"),
        pageSize=pageSize,
        robloxUsername=attendee.get("robloxUsername"),
    )
    embeds = _dep("buildOutfitPageEmbeds")(
        targetUserId,
        attendee.get("robloxUsername"),
        outfits,
        thumbs,
        0,
        pageSize,
    )
    await _dep("safeInteractionReply")(interaction, embeds=embeds, view=view, ephemeral=True)
