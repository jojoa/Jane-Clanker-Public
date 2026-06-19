from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from types import FrameType
from typing import Any

log = logging.getLogger(__name__)


def _configFloat(configModule: Any, name: str, default: float) -> float:
    try:
        value = float(getattr(configModule, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


def _configInt(configModule: Any, name: str, default: int) -> int:
    try:
        value = int(getattr(configModule, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def _formatTaskFrame(task: asyncio.Task[Any]) -> str:
    name = task.get_name() if hasattr(task, "get_name") else ""
    label = name or repr(task)
    frames: list[FrameType] = task.get_stack(limit=1)
    if not frames:
        return f"{label}: no Python frame"
    frame = frames[-1]
    code = frame.f_code
    return f"{label}: {code.co_filename}:{frame.f_lineno} in {code.co_name}"


class EventLoopWatchdog:
    def __init__(self, *, configModule: Any) -> None:
        self.config = configModule
        self._task: asyncio.Task[None] | None = None

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "eventLoopWatchdogEnabled", True))

    def _intervalSec(self) -> float:
        return max(0.5, _configFloat(self.config, "eventLoopWatchdogIntervalSec", 5.0))

    def _warnAfterSec(self) -> float:
        return max(0.25, _configFloat(self.config, "eventLoopWatchdogWarnAfterSec", 2.0))

    def _stackLimit(self) -> int:
        return max(1, _configInt(self.config, "eventLoopWatchdogStackTaskLimit", 8))

    def start(self) -> None:
        if not self._enabled():
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="event-loop-watchdog")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        intervalSec = self._intervalSec()
        expectedAt = perf_counter() + intervalSec
        while True:
            await asyncio.sleep(intervalSec)
            now = perf_counter()
            lagSec = now - expectedAt
            expectedAt = now + intervalSec
            if lagSec < self._warnAfterSec():
                continue

            current = asyncio.current_task()
            tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not current and not task.done()
            ]
            taskLines = [_formatTaskFrame(task) for task in tasks[: self._stackLimit()]]
            log.warning(
                "Event loop lag detected: %.2fs over expected wakeup. pendingTasks=%d sample=%s",
                lagSec,
                len(tasks),
                " | ".join(taskLines) if taskLines else "none",
                extra={"skipErrorMirrorDm": True},
            )
