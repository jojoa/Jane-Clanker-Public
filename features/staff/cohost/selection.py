from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


EVENT_LABELS = {
    "solo": "Solo",
    "turbine": "Turbine",
    "emergency": "Emergency",
    "grid": "Grid",
    "shift": "Shift",
}

RANK_PRIORITY = {
    "SRO": 0,
    "STA": 1,
}


@dataclass(frozen=True)
class VolunteerCandidate:
    userId: int
    rank: str = ""
    joinedAt: Optional[datetime] = None


@dataclass(frozen=True)
class CohostHistoryEntry:
    userId: int
    eventType: str
    selectedAt: Optional[datetime] = None


@dataclass(frozen=True)
class SelectionResult:
    userId: str
    rank: str
    totalCohosts: int
    eventCount: int
    eventTypesCompleted: int
    lastEventDate: Optional[datetime]
    lastAnyDate: Optional[datetime]


@dataclass
class _HistoryStats:
    totalCohosts: int = 0
    eventCount: int = 0
    eventTypesCompleted: int = 0
    lastEventDate: Optional[datetime] = None
    lastAnyDate: Optional[datetime] = None


def normalizeEvent(event: str) -> str:
    key = str(event or "").strip().lower()
    if key not in EVENT_LABELS:
        raise ValueError(f"Unknown event '{event}'. Use one of: {', '.join(EVENT_LABELS)}")
    return key


def eventLabel(event: str) -> str:
    return EVENT_LABELS[normalizeEvent(event)]


def normalizeRank(rank: object) -> str:
    text = str(rank or "").strip().upper()
    return text if text in RANK_PRIORITY else ""


def parseDbTime(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        datetime.fromisoformat,
        lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return parser(text)
        except ValueError:
            continue
    return None


def _historyStats(
    history: Iterable[CohostHistoryEntry],
    eventType: str,
) -> dict[int, _HistoryStats]:
    byUser: dict[int, list[CohostHistoryEntry]] = {}
    for entry in history:
        try:
            userId = int(entry.userId)
            normalizeEvent(entry.eventType)
        except (TypeError, ValueError):
            continue
        if userId <= 0:
            continue
        byUser.setdefault(userId, []).append(entry)

    out: dict[int, _HistoryStats] = {}
    for userId, entries in byUser.items():
        eventTypes = {
            normalizeEvent(entry.eventType)
            for entry in entries
            if str(entry.eventType or "").strip()
        }
        selectedDates = [entry.selectedAt for entry in entries if entry.selectedAt is not None]
        eventDates = [
            entry.selectedAt
            for entry in entries
            if normalizeEvent(entry.eventType) == eventType and entry.selectedAt is not None
        ]
        out[userId] = _HistoryStats(
            totalCohosts=len(entries),
            eventCount=sum(1 for entry in entries if normalizeEvent(entry.eventType) == eventType),
            eventTypesCompleted=len(eventTypes),
            lastEventDate=max(eventDates) if eventDates else None,
            lastAnyDate=max(selectedDates) if selectedDates else None,
        )
    return out


def _uniqueVolunteers(volunteers: Iterable[VolunteerCandidate]) -> list[VolunteerCandidate]:
    out: list[VolunteerCandidate] = []
    seen: set[int] = set()
    for volunteer in volunteers:
        try:
            userId = int(volunteer.userId)
        except (TypeError, ValueError):
            continue
        if userId <= 0 or userId in seen:
            continue
        seen.add(userId)
        out.append(
            VolunteerCandidate(
                userId=userId,
                rank=normalizeRank(volunteer.rank),
                joinedAt=volunteer.joinedAt,
            )
        )
    return out


def selectCohosts(
    event: str,
    volunteers: Iterable[VolunteerCandidate],
    history: Iterable[CohostHistoryEntry],
    *,
    slots: int = 2,
) -> list[SelectionResult]:
    eventType = normalizeEvent(event)
    volunteerList = _uniqueVolunteers(volunteers)
    if not volunteerList or slots <= 0:
        return []

    statsByUser = _historyStats(history, eventType)
    oldest = datetime.min
    newest = datetime.max

    def _sortKey(volunteer: VolunteerCandidate) -> tuple:
        stats = statsByUser.get(volunteer.userId, _HistoryStats())
        return (
            RANK_PRIORITY.get(volunteer.rank, len(RANK_PRIORITY)),
            stats.totalCohosts,
            stats.eventCount,
            stats.eventTypesCompleted,
            stats.lastEventDate or oldest,
            stats.lastAnyDate or oldest,
            volunteer.joinedAt or newest,
            volunteer.userId,
        )

    selected = sorted(volunteerList, key=_sortKey)[: max(0, int(slots))]
    results: list[SelectionResult] = []
    for volunteer in selected:
        stats = statsByUser.get(volunteer.userId, _HistoryStats())
        results.append(
            SelectionResult(
                userId=str(volunteer.userId),
                rank=volunteer.rank,
                totalCohosts=stats.totalCohosts,
                eventCount=stats.eventCount,
                eventTypesCompleted=stats.eventTypesCompleted,
                lastEventDate=stats.lastEventDate,
                lastAnyDate=stats.lastAnyDate,
            )
        )
    return results
