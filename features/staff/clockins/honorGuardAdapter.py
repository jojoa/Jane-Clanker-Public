from typing import Sequence

import discord

from features.staff.honorGuard import service as honorGuardService


class HonorGuardAdapter:
    async def createSession(
        self,
        guildId: int,
        channelId: int,
        hostId: int,
        maxAttendeeLimit: int = 30,
        **kwargs,
    ) -> int:
        return await honorGuardService.createEventRecord(
            guildId=guildId,
            eventType=kwargs.get("eventType", "drill"),
            eventTitle=kwargs.get("eventTitle", "Honor Guard Event"),
            eventDate=kwargs.get("eventDate", ""),
            hostId=hostId,
            channelId=channelId,
            createdById=kwargs.get("createdBy", 0),
        )

    async def setSessionMessageId(self, sessionId: int, messageId: int) -> None:
        await honorGuardService.setEventRecordMessageId(int(sessionId), int(messageId))

    async def getSession(self, sessionId: int) -> dict | None:
        return await honorGuardService.getEventRecord(int(sessionId))

    async def listOpenSessions(self) -> list[dict]:
        return await honorGuardService.listOpenEventSessions()

    async def listAttendees(self, sessionId: int) -> list[dict]:
        return await honorGuardService.listHonorGuardAttendees(int(sessionId))

    async def addAttendee(self, sessionId: int, userId: int, **kwargs) -> None:
        await honorGuardService.createAttendanceRecord(eventId = int(sessionId), guildId = int(kwargs.get("guildId")), userId = int(userId), memberGroup = kwargs.get("memberGroup", "ENLISTED"), participantRole = kwargs.get("participantRole", "ATTENDEE"), createdBy = kwargs.get("createdBy", userId))

    async def removeAttendee(self, sessionId: int, userId: int) -> None:
        await honorGuardService.removeAttendanceRecord(eventId=int(sessionId), userId=int(userId))

    async def updateSessionStatus(self, sessionId: int, status: str) -> None:
        await honorGuardService.updateEventRecordStatus(int(sessionId), str(status))

    def normalizeSession(self, session: dict) -> dict:
        return {
            "sessionId": int(session.get("eventId") or 0),
            "guildId": int(session.get("guildId") or 0),
            "channelId": int(session.get("channelId") or 0),
            "messageId": int(session.get("messageId") or 0),
            "hostId": int(session.get("hostId") or 0),
            "status": str(session.get("status") or "OPEN").upper(),
        }

    def buildEmbed(self, session: dict, allAttendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)

        attendees = filter(lambda x: str(x.get("participantRole")).upper() == "ATTENDEE", allAttendees)
        supervisors = filter(lambda x: str(x.get("participantRole")).upper() == "SUPERVISOR", allAttendees)
        cohosts = filter(lambda x: str(x.get("participantRole")).upper() == "COHOST", allAttendees)

        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0
        ]
        supervisorMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(supervisors)
            if int(row.get("userId") or 0) > 0
        ]
        cohostMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(cohosts)
            if int(row.get("userId") or 0) > 0
        ]

        embed = discord.Embed(
            title="Honor Guard Clock-in",
            description=session.get("eventTitle") or "Honor Guard Event",
        )
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorMentions)})",
            value="\n".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostMentions)})",
            value="\n".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)})",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        embed.add_field(name="Status", value=normalized["status"], inline=False)
        return embed

    def buildEventManageEmbed(self, session: dict, allAttendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)

        attendees = filter(lambda x: str(x.get("participantRole")).upper() == "ATTENDEE", allAttendees)
        supervisors = filter(lambda x: str(x.get("participantRole")).upper() == "SUPERVISOR", allAttendees)
        cohosts = filter(lambda x: str(x.get("participantRole")).upper() == "COHOST", allAttendees)

        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0
        ]
        supervisorMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(supervisors)
            if int(row.get("userId") or 0) > 0
        ]
        cohostMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(cohosts)
            if int(row.get("userId") or 0) > 0
        ]

        embed = discord.Embed(
            title="Honor Guard Clock-in Management",
            description=session.get("eventTitle") or "Honor Guard Event",
        )
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorMentions)})",
            value="\n".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostMentions)})",
            value="\n".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)})",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        embed.add_field(name="Status", value=normalized["status"], inline=False)
        return embed
    
    def buildSubmitEmbed(self, session: dict, allAttendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)
        embed = discord.Embed(
            title="Submit Honor Guard Event",
            description=session.get("eventTitle") or "Honor Guard Event",
        )

        attendees = filter(lambda x: str(x.get("participantRole")).upper() == "ATTENDEE", allAttendees)
        supervisors = filter(lambda x: str(x.get("participantRole")).upper() == "SUPERVISOR", allAttendees)
        cohosts = filter(lambda x: str(x.get("participantRole")).upper() == "COHOST", allAttendees)

        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0
        ]
        supervisorMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
            for index, row in enumerate(supervisors)
            if int(row.get("userId") or 0) > 0
        ]
        cohostMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.get("quotaPoints")}E{row.get("promotionEventPoints")} points"
            for index, row in enumerate(cohosts)
            if int(row.get("userId") or 0) > 0
        ]

        host = next((row for row in allAttendees if int(row.get("userId") or 0) == normalized["hostId"]), None)
        hostMention = f"<@{int(host.get('userId') or 0)}> - {host.get("quotaPoints")}E{host.get("promotionEventPoints")} points"

        embed.add_field(name="Host", value=hostMention, inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorMentions)}):\n",
            value="\n".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostMentions)}):\n",
            value="\n".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)}):\n",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        return embed