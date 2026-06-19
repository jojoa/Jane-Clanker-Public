from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from features.staff.sessions import views


class SessionViewRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_persistent_views_reattaches_full_orientation_sessions(self) -> None:
        bot = SimpleNamespace(add_view=Mock())

        fullSession = {
            "sessionId": 123,
            "guildId": 1,
            "channelId": 2,
            "messageId": 456,
            "sessionType": "orientation",
            "status": "FULL",
            "createdAt": "2099-01-01 00:00:00",
            "bgQueueMessageId": None,
            "bgQueueMinorMessageId": None,
        }

        with (
            patch.object(views.service, "getSessionsByStatus", AsyncMock(return_value=[fullSession])) as getSessions,
            patch.object(views.service, "getAttendees", AsyncMock(return_value=[])),
            patch.object(views.orientationRoverWarmup, "scheduleOrientationRoverWarmup", Mock(return_value=True)),
        ):
            result = await views.restorePersistentViews(bot)

        getSessions.assert_awaited_once_with(["OPEN", "FULL", "GRADING", "FINISHED"])
        bot.add_view.assert_called_once()
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["bgQueues"], 0)
        self.assertEqual(result["bgChecks"], 0)

    async def test_restore_persistent_views_skips_stale_active_orientation_sessions(self) -> None:
        bot = SimpleNamespace(add_view=Mock())

        oldOrientation = {
            "sessionId": 124,
            "guildId": 1,
            "channelId": 2,
            "messageId": 457,
            "sessionType": "orientation",
            "status": "OPEN",
            "createdAt": "2000-01-01 00:00:00",
            "bgQueueMessageId": None,
            "bgQueueMinorMessageId": None,
        }

        with (
            patch.object(views.service, "getSessionsByStatus", AsyncMock(return_value=[oldOrientation])),
            patch.object(views.service, "getAttendees", AsyncMock(return_value=[])),
            patch.object(views.orientationRoverWarmup, "scheduleOrientationRoverWarmup", Mock(return_value=True)) as warmup,
        ):
            result = await views.restorePersistentViews(bot)

        bot.add_view.assert_not_called()
        warmup.assert_not_called()
        self.assertEqual(result["sessions"], 0)

    async def test_restore_persistent_views_handles_timezone_aware_created_at(self) -> None:
        bot = SimpleNamespace(add_view=Mock())

        timezoneAwareOrientation = {
            "sessionId": 125,
            "guildId": 1,
            "channelId": 2,
            "messageId": 458,
            "sessionType": "orientation",
            "status": "OPEN",
            "createdAt": "2099-01-01T00:00:00+00:00",
            "bgQueueMessageId": None,
            "bgQueueMinorMessageId": None,
        }

        with (
            patch.object(views.service, "getSessionsByStatus", AsyncMock(return_value=[timezoneAwareOrientation])),
            patch.object(views.service, "getAttendees", AsyncMock(return_value=[])),
            patch.object(views.orientationRoverWarmup, "scheduleOrientationRoverWarmup", Mock(return_value=True)),
        ):
            result = await views.restorePersistentViews(bot)

        bot.add_view.assert_called_once()
        self.assertEqual(result["sessions"], 1)


if __name__ == "__main__":
    unittest.main()
