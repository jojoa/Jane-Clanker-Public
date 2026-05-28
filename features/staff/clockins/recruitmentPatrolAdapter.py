from typing import Sequence

import discord

from features.staff.recruitment import service as recruitmentService


class RecruitmentPatrolAdapter:
    async def createSession(
        self,
        guildId: int,
        channelId: int,
        hostId: int,
        maxAttendeeLimit: int = 30,
        **kwargs,
    ) -> int:
        return await recruitmentService.createRecruitmentPatrolSession(
            guildId=guildId,
            channelId=channelId,
            hostId=hostId,
        )

    async def setSessionMessageId(self, sessionId: int, messageId: int) -> None:
        await recruitmentService.setRecruitmentPatrolMessageId(int(sessionId), int(messageId))

    async def getSession(self, sessionId: int) -> dict | None:
        return await recruitmentService.getRecruitmentPatrolSession(int(sessionId))

    async def listOpenSessions(self) -> list[dict]:
        return await recruitmentService.listOpenRecruitmentPatrolSessions()

    async def listAttendees(self, sessionId: int) -> list[dict]:
        return await recruitmentService.listRecruitmentPatrolAttendees(int(sessionId))

    async def addAttendee(self, sessionId: int, userId: int, **kwargs) -> None:
        await recruitmentService.addRecruitmentPatrolAttendee(int(sessionId), int(userId))

    async def removeAttendee(self, sessionId: int, userId: int) -> None:
        await recruitmentService.removeRecruitmentPatrolAttendee(int(sessionId), int(userId))

    async def updateSessionStatus(self, sessionId: int, status: str) -> None:
        await recruitmentService.updateRecruitmentPatrolStatus(int(sessionId), str(status))

    def normalizeSession(self, session: dict) -> dict:
        return {
            "sessionId": int(session.get("patrolId") or 0),
            "guildId": int(session.get("guildId") or 0),
            "channelId": int(session.get("channelId") or 0),
            "messageId": int(session.get("messageId") or 0),
            "hostId": int(session.get("hostId") or 0),
            "status": str(session.get("status") or "OPEN").upper(),
        }

    def buildEmbed(self, session: dict, attendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)
        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0
        ]

        embed = discord.Embed(
            title="Recruitment Patrol Clock-in",
            description="Group patrol attendance list",
        )
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)})",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        embed.add_field(name="Status", value=normalized["status"], inline=False)
        return embed

