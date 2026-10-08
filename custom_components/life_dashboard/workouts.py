"""Workouts: the exercise sessions of the last two days, and the sensors read from them.

The exercise records of a sync are what the Last workout sensor shows, newest first, like
every other latest-value sensor. Exercise today and Exercise yesterday are sums over a
day, and a sync only carries what is new, so they are read from the sessions the ledger
keeps (history.py): every session of today and yesterday, by uuid, so a record that
arrives again or is edited replaces itself.

A session that crosses midnight counts on both days for the part of it that fell on
each, so a run from 23:30 to 00:30 is half an hour on the one day and half an hour on
the other. The long-term statistic `exercise_minutes` is different on purpose: it books a
whole session on the day it ended, as it always did.

The shortest workout that counts is an option. It is applied when a sensor is read, never
when a session is stored, so changing it takes effect at once and loses nothing.

Nothing here imports from Home Assistant.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Final

from .payload import (
    KEY_EXERCISE_TODAY,
    KEY_EXERCISE_YESTERDAY,
    KEY_LAST_WORKOUT,
    SensorUpdate,
    is_own_record,
    parse_instant,
)
from .workout_types import type_code, workout_type

#: How many days back a session is kept: yesterday and today.
KEPT_DAYS: Final = 2


@dataclass(frozen=True)
class Workout:
    """One exercise session."""

    #: The type exactly as the phone sent it, "56" or "running".
    code: str
    start: datetime
    end: datetime
    seconds: float
    #: The app that wrote the record to Health Connect, when the phone said so.
    source: str | None = None
    uuid: str | None = None

    @property
    def kind(self) -> str:
        """The key it is shown as: running, walking, other."""
        return workout_type(self.code)

    @property
    def minutes(self) -> float:
        return self.seconds / 60


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def parse_workout(record: Any) -> Workout | None:
    """A workout from an exercise record, or None when it cannot be read.

    Needs an end and a length; a record without a length is as long as its span. The start
    is the one that is sent, or the end minus the length when it is missing or does not
    come before the end.
    """
    if not isinstance(record, dict) or is_own_record(record):
        return None
    try:
        end = parse_instant(record.get("end_time"))
    except ValueError:
        return None

    seconds = _finite_number(record.get("duration_seconds"))
    try:
        start = parse_instant(record.get("start_time"))
    except ValueError:
        start = None
    if start is not None and start >= end:
        start = None
    if record.get("duration_seconds") is None and start is not None:
        seconds = (end - start).total_seconds()
    if seconds is None or seconds <= 0:
        return None
    if start is None:
        start = end - timedelta(seconds=seconds)

    uuid = record.get("uuid")
    source = record.get("source")
    return Workout(
        code=type_code(record.get("type")),
        start=start,
        end=end,
        seconds=seconds,
        source=source if isinstance(source, str) and source else None,
        uuid=uuid if isinstance(uuid, str) and uuid else None,
    )


def identity(workout: Workout) -> str:
    """What a session is stored under: its uuid, or for a record without one the moment,
    the length and the type, which identify it for the case that matters, the same batch
    arriving again."""
    if workout.uuid:
        return workout.uuid
    return f"{workout.end.isoformat()}:{workout.seconds}:{workout.code}"


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _cutoff(now: datetime, tz: tzinfo) -> datetime:
    """The start of the oldest day that is kept."""
    today = now.astimezone(tz).date()
    return _local_midnight(today - timedelta(days=KEPT_DAYS - 1), tz)


def keep_sessions(
    sessions: dict[str, dict[str, Any]], data: dict[str, Any], *, now: datetime, tz: tzinfo
) -> bool:
    """Fold the exercise records of a payload into the stored sessions, by uuid.

    Sessions that ended before the start of yesterday are dropped, and never stored when
    a backfill brings them. Returns whether anything changed.
    """
    records = data.get("exercise")
    changed = False
    cutoff = _cutoff(now, tz)

    if isinstance(records, list):
        for record in records:
            workout = parse_workout(record)
            if workout is None or workout.end <= cutoff:
                continue
            row = {
                "type": workout.code,
                "start": workout.start.isoformat(),
                "end": workout.end.isoformat(),
                "seconds": workout.seconds,
            }
            key = identity(workout)
            if sessions.get(key) != row:
                sessions[key] = row
                changed = True

    for key, row in list(sessions.items()):
        try:
            expired = _from_row(row).end <= cutoff
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            del sessions[key]
            changed = True
    return changed


def _from_row(row: dict[str, Any]) -> Workout:
    return Workout(
        code=row["type"],
        start=datetime.fromisoformat(row["start"]),
        end=datetime.fromisoformat(row["end"]),
        seconds=float(row["seconds"]),
    )


def stored_workouts(sessions: dict[str, dict[str, Any]]) -> list[Workout]:
    """The stored sessions, oldest first. A row that cannot be read is skipped."""
    workouts = []
    for row in sessions.values():
        try:
            workouts.append(_from_row(row))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(workouts, key=lambda workout: workout.start)


def _local(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).isoformat()


def day_sessions(
    workouts: Iterable[Workout], day: date, *, tz: tzinfo, min_minutes: float = 0
) -> list[dict[str, Any]]:
    """The sessions that have a part on a local day, each with the minutes that fell on it.

    A session shorter than `min_minutes` is left out; one that crosses midnight is listed
    on both days with its start and end whole and only its minutes split.
    """
    day_start = _local_midnight(day, tz)
    day_end = _local_midnight(day + timedelta(days=1), tz)
    rows = []
    for workout in workouts:
        if workout.minutes < min_minutes:
            continue
        span = (workout.end - workout.start).total_seconds()
        overlap = (min(workout.end, day_end) - max(workout.start, day_start)).total_seconds()
        if overlap <= 0:
            continue
        rows.append(
            {
                "type": workout.kind,
                "start": _local(workout.start, tz),
                "end": _local(workout.end, tz),
                # The length it was sent with, shared out by the time that fell on the day.
                "minutes": round(workout.minutes * min(overlap / span, 1)),
            }
        )
    return sorted(rows, key=lambda row: row["start"])


def day_updates(
    workouts: Iterable[Workout], *, now: datetime, tz: tzinfo, min_minutes: float = 0
) -> list[SensorUpdate]:
    """Exercise today and Exercise yesterday, as of `now`."""
    today = now.astimezone(tz).date()
    kept = list(workouts)
    updates = []
    for key, day in (
        (KEY_EXERCISE_TODAY, today),
        (KEY_EXERCISE_YESTERDAY, today - timedelta(days=1)),
    ):
        sessions = day_sessions(kept, day, tz=tz, min_minutes=min_minutes)
        updates.append(
            SensorUpdate(
                key=key,
                value=sum(session["minutes"] for session in sessions),
                measured_at=_local_midnight(day, tz),
                attributes={"date": day.isoformat(), "sessions": sessions},
            )
        )
    return updates


def last_workout_update(records: Any, *, tz: tzinfo, min_minutes: float = 0) -> SensorUpdate | None:
    """Last workout, from the newest session of a payload that is long enough."""
    if not isinstance(records, list):
        return None
    best: Workout | None = None
    for record in records:
        workout = parse_workout(record)
        if workout is None or workout.minutes < min_minutes:
            continue
        if best is None or workout.end > best.end:
            best = workout
    if best is None:
        return None

    attributes: dict[str, Any] = {
        "start": _local(best.start, tz),
        "end": _local(best.end, tz),
        "duration_minutes": round(best.minutes),
        "type_code": best.code,
    }
    if best.source:
        attributes["source"] = best.source
    if best.uuid:
        attributes["uuid"] = best.uuid
    return SensorUpdate(
        key=KEY_LAST_WORKOUT, value=best.kind, measured_at=best.end, attributes=attributes
    )
