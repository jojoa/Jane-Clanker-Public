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
        await honorGuardService.createAttendanceRecord(eventId = int(sessionId), guildId = int(kwargs.get("guildId")), targetUserId = int(userId), memberGroup = kwargs.get("memberGroup", "ENLISTED"), participationRole = kwargs.get("participationRole", "ATTENDEE"), createdBy = kwargs.get("createdBy", userId))

    async def removeAttendee(self, sessionId: int, userId: int) -> None:
        await honorGuardService.removeAttendanceRecord(int(sessionId), int(userId))

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

    def buildEmbed(self, session: dict, attendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)
        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "ATTENDEE").upper() == "ATTENDEE"
        ]
        supervisorMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "SUPERVISOR").upper() == "SUPERVISOR"
        ]

        cohostMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "COHOST").upper() == "COHOST"
        ]

        embed = discord.Embed(
            title="Honor Guard Clock-in",
            description="Event attendance list",
        )
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorMentions)})",
            value=", ".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostMentions)})",
            value=", ".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)})",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        embed.add_field(name="Status", value=normalized["status"], inline=False)
        return embed

    def buildEventManageEmbed(self, session: dict, attendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)
        attendeeMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "ATTENDEE").upper() == "ATTENDEE"
        ]
        supervisorMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "SUPERVISOR").upper() == "SUPERVISOR"
        ]

        cohostMentions = [
            f"{index + 1}. <@{int(row.get('userId') or 0)}>"
            for index, row in enumerate(attendees)
            if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "COHOST").upper() == "COHOST"
        ]

        embed = discord.Embed(
            title="Honor Guard Clock-in Management",
            description="Manage event attendance",
        )
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorMentions)})",
            value=", ".join(supervisorMentions) if supervisorMentions else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostMentions)})",
            value=", ".join(cohostMentions) if cohostMentions else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeMentions)})",
            value="\n".join(attendeeMentions) if attendeeMentions else "No attendees yet.",
            inline=False,
        )
        embed.add_field(name="Status", value=normalized["status"], inline=False)
        return embed
    
    def buildSubmitEmbed(self, session: dict, attendees: Sequence[dict]) -> discord.Embed:
        normalized = self.normalizeSession(session)
        embed = discord.Embed(
            title="Submit Honor Guard Event",
            description="Review the event details and submit.",
        )
        attendeeText = "\n".join(
            [
                f"{index + 1}. <@{int(row.get('userId') or 0)}> - {row.honorGuardService.calculatePointDeltas()} points"
                for index, row in enumerate(attendees)
                if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "ATTENDEE").upper() == "ATTENDEE"
            ]
        ) or "No attendees yet."
        supervisorText = "\n".join(
            [
                f"{index + 1}. <@{int(row.get('userId') or 0)}>"
                for index, row in enumerate(attendees)
                if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "SUPERVISOR").upper() == "SUPERVISOR"
            ]
        ) or "No supervisors assigned."
        cohostText = "\n".join(
            [
                f"{index + 1}. <@{int(row.get('userId') or 0)}>"
                for index, row in enumerate(attendees)
                if int(row.get("userId") or 0) > 0 and str(row.get("participantRole") or "COHOST").upper() == "COHOST"
            ]
        ) or "No cohosts assigned."
        embed.add_field(name="Host", value=f"<@{normalized['hostId']}>", inline=False)
        embed.add_field(
            name=f"Supervisors ({len(supervisorText)}):\n",
            value=", ".join(supervisorText) if supervisorText else "No supervisors assigned.",
            inline=False)
        embed.add_field(
            name=f"Cohosts ({len(cohostText)}):\n",
            value=", ".join(cohostText) if cohostText else "No cohosts assigned.",
            inline=False)
        embed.add_field(
            name=f"Attendees ({len(attendeeText)}):\n",
            value="\n".join(attendeeText) if attendeeText else "No attendees yet.",
            inline=False,
        )
        return embed