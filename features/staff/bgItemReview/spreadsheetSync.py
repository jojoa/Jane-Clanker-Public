from __future__ import annotations

from typing import Any

import config
from runtime import orgProfiles


def _positiveInt(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _configValue(name: str, *, guildId: int = 0, default: object = None) -> object:
    return orgProfiles.getOrganizationValue(
        config,
        name,
        guildId=int(guildId or 0),
        default=default,
    )


def _startupLookbackDays(guildId: int = 0) -> int:
    return max(
        1,
        min(
            _positiveInt(
                _configValue(
                    "bgItemReviewSpreadsheetStartupLookbackDays",
                    guildId=guildId,
                    default=getattr(config, "bgItemReviewSpreadsheetStartupLookbackDays", 5),
                )
            )
            or 5,
            30,
        ),
    )


def _recurringLookbackDays(guildId: int = 0) -> int:
    return max(
        1,
        min(
            _positiveInt(
                _configValue(
                    "bgItemReviewSpreadsheetRecurringLookbackDays",
                    guildId=guildId,
                    default=getattr(config, "bgItemReviewSpreadsheetRecurringLookbackDays", 1),
                )
            )
            or 1,
            30,
        ),
    )


async def syncDeniedSpreadsheetRows(
    botClient: Any,
    *,
    guildId: int = 0,
    lookbackDays: int | None = None,
) -> dict[str, int | str | bool]:
    return {
        "enabled": False,
        "files": 0,
        "rows": 0,
        "denied": 0,
        "created": 0,
        "existing": 0,
        "known": 0,
        "errors": 0,
        "lookbackDays": int(lookbackDays or _recurringLookbackDays(guildId)),
        "reason": "Denied-row inventory item review sync was removed.",
    }
