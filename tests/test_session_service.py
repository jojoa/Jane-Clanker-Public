from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from db import sqlite as sqliteDb
from features.staff.sessions import service


class SessionClockInTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tempDir = tempfile.TemporaryDirectory()
        self._originalDbPath = sqliteDb.dbPath
        await sqliteDb.closeDb()
        sqliteDb.dbPath = str(Path(self._tempDir.name) / "test.db")
        await sqliteDb.initDb()

    async def asyncTearDown(self) -> None:
        await sqliteDb.closeDb()
        sqliteDb.dbPath = self._originalDbPath
        self._tempDir.cleanup()

    async def test_attempt_clock_in_respects_limit_under_concurrency(self) -> None:
        sessionId = await service.createSession(
            guildId=1,
            channelId=2,
            messageId=3,
            sessionType="orientation",
            hostId=4,
            password="secret",
            maxAttendeeLimit=2,
        )

        results = await asyncio.gather(
            service.attemptClockIn(sessionId, 101, "secret"),
            service.attemptClockIn(sessionId, 102, "secret"),
            service.attemptClockIn(sessionId, 103, "secret"),
        )

        statuses = [str(result.get("status") or "") for result in results]
        self.assertEqual(statuses.count("ADDED"), 2)
        self.assertEqual(
            sum(
                1
                for result in results
                if str(result.get("status") or "") == "FULL"
                or (
                    str(result.get("status") or "") == "SESSION_CLOSED"
                    and str(result.get("sessionStatus") or "") == "FULL"
                )
            ),
            1,
        )

        attendees = await service.getAttendees(sessionId)
        self.assertEqual(len(attendees), 2)
        self.assertTrue({int(row["userId"]) for row in attendees}.issubset({101, 102, 103}))

        session = await service.getSession(sessionId)
        self.assertIsNotNone(session)
        self.assertEqual(session["status"], "FULL")

    async def test_attempt_clock_in_rejects_bad_password_without_using_slot(self) -> None:
        sessionId = await service.createSession(
            guildId=1,
            channelId=2,
            messageId=3,
            sessionType="orientation",
            hostId=4,
            password="secret",
            maxAttendeeLimit=1,
        )

        badResult = await service.attemptClockIn(sessionId, 101, "wrong")
        goodResult = await service.attemptClockIn(sessionId, 101, "secret")

        self.assertEqual(badResult["status"], "BAD_PASSWORD")
        self.assertEqual(goodResult["status"], "ADDED")
        self.assertEqual(await service.getAttendeeCount(sessionId), 1)

    async def test_expire_stale_sessions_cancels_full_sessions(self) -> None:
        staleFullSessionId = await service.createSession(
            guildId=1,
            channelId=2,
            messageId=3,
            sessionType="orientation",
            hostId=4,
            password="secret",
            maxAttendeeLimit=1,
        )
        await service.attemptClockIn(staleFullSessionId, 101, "secret")
        await sqliteDb.execute(
            "UPDATE sessions SET createdAt = datetime('now', '-72 hours') WHERE sessionId = ?",
            (staleFullSessionId,),
        )

        expired = await service.expireStaleSessions(maxAgeHours=48)

        self.assertEqual(expired, [staleFullSessionId])
        session = await service.getSession(staleFullSessionId)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["status"], "CANCELED")
        self.assertIsNotNone(session["finishedAt"])


class SqliteRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_database_operation_retries_then_succeeds(self) -> None:
        attempts = 0

        async def _operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with (
            patch.object(sqliteDb.asyncio, "sleep", AsyncMock()) as sleepMock,
            patch.object(sqliteDb.log, "warning"),
        ):
            result = await sqliteDb._runWithLockedDatabaseRetries("test", "SELECT 1", _operation)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        sleepMock.assert_awaited_once()

    async def test_non_lock_operational_error_is_not_retried(self) -> None:
        attempts = 0

        async def _operation() -> str:
            nonlocal attempts
            attempts += 1
            raise sqlite3.OperationalError("no such table: missing")

        with patch.object(sqliteDb.asyncio, "sleep", AsyncMock()) as sleepMock:
            with self.assertRaises(sqlite3.OperationalError):
                await sqliteDb._runWithLockedDatabaseRetries("test", "SELECT * FROM missing", _operation)

        self.assertEqual(attempts, 1)
        sleepMock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
