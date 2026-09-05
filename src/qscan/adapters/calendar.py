"""NYSE schedule with explicit UTC close overrides."""

import json
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version

import pandas_market_calendars as mcal  # type: ignore[import-untyped]

from qscan import __version__
from qscan.application.contracts import ApplicationError
from qscan.domain.models import ErrorCode, ScanContext


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("Clock must be timezone aware")
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class NYSECalendar:
    def __init__(
        self,
        buffer: timedelta = timedelta(minutes=30),
        overrides: dict[date, datetime | None] | None = None,
    ) -> None:
        self.calendar = mcal.get_calendar("NYSE")
        self.buffer = buffer
        self.overrides = dict(overrides or {})
        if buffer < timedelta(0) or any(
            close is not None and close.tzinfo is None for close in self.overrides.values()
        ):
            raise ValueError("Invalid buffer or naive override")
        self.version = (
            "NYSE/"
            + version("pandas-market-calendars")
            + "/"
            + json.dumps(
                {
                    "buffer_seconds": buffer.total_seconds(),
                    "overrides": {
                        s.isoformat(): c.astimezone(UTC).isoformat() if c else None
                        for s, c in sorted(self.overrides.items())
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def schedule(self, start: date, end: date) -> dict[date, datetime]:
        frame = self.calendar.schedule(start_date=start, end_date=end)
        result = {
            stamp.date(): close.to_pydatetime() for stamp, close in frame.market_close.items()
        }
        for session, close in self.overrides.items():
            if start <= session <= end:
                if close is None:
                    result.pop(session, None)
                else:
                    result[session] = close.astimezone(UTC)
        return dict(sorted(result.items()))

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(self.schedule(start, end))

    def resolve(self, requested: date | None, now: datetime) -> ScanContext:
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone aware")
        end = requested or now.astimezone(UTC).date()
        schedule = self.schedule(end - timedelta(days=370), end)
        if requested is not None:
            if requested not in schedule:
                raise ApplicationError(ErrorCode.INVALID_AS_OF_SESSION)
            if now < schedule[requested] + self.buffer:
                raise ApplicationError(ErrorCode.SESSION_NOT_COMPLETE)
            target = requested
        else:
            completed = [s for s, close in schedule.items() if now >= close + self.buffer]
            if not completed:
                raise ApplicationError(ErrorCode.SESSION_NOT_COMPLETE)
            target = completed[-1]
        previous = [s for s in schedule if s < target]
        if not previous:
            raise ApplicationError(ErrorCode.INVALID_AS_OF_SESSION)
        return ScanContext(
            as_of_session=target, reference_session=previous[-1], engine_version=__version__
        )
