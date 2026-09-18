"""Managed-universe refresh with an immutable, bounded-age last-known-good snapshot."""

import hashlib
import json
from datetime import UTC, timedelta
from uuid import NAMESPACE_URL, uuid5

from qscan.adapters.providers import resolve_us
from qscan.adapters.universe import UniverseSourceError
from qscan.application.contracts import (
    ApplicationError,
    Clock,
    Repository,
    ServiceLock,
    UniverseMember,
    UniverseSnapshot,
    UniverseSource,
)
from qscan.domain.models import ErrorCode

MANAGED_SP500_WATCHLIST_ID = uuid5(NAMESPACE_URL, "qscan:managed-watchlist:sp500")
MAX_LKG_AGE = timedelta(days=7)
MIN_MEMBER_COUNT = 450
MAX_MEMBER_COUNT = 550


class UniverseService:
    def __init__(
        self, repository: Repository, source: UniverseSource, clock: Clock, lock: ServiceLock
    ) -> None:
        self.repository, self.source, self.clock, self.lock = repository, source, clock, lock

    def refresh(self) -> UniverseSnapshot:
        try:
            observation = self.source.fetch()
            retrieved_at = self.clock.now().astimezone(UTC)
            if (
                observation.effective_date is not None
                and observation.effective_date > retrieved_at.date()
            ):
                raise UniverseSourceError("Future-effective membership cannot be applied yet")
            members = tuple(self._member(symbol) for symbol in observation.symbols)
            if not MIN_MEMBER_COUNT <= len(members) <= MAX_MEMBER_COUNT:
                raise UniverseSourceError("Suspicious S&P 500 member count")
            canonical = [
                member.model_dump(mode="json", exclude={"instrument": {"id"}}) for member in members
            ]
            content_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            snapshot = UniverseSnapshot(
                id=uuid5(
                    NAMESPACE_URL,
                    "qscan:universe:sp500:"
                    f"{observation.source_revision}:{content_hash}:{retrieved_at.isoformat()}",
                ),
                watchlist_id=MANAGED_SP500_WATCHLIST_ID,
                source_url=observation.source_url,
                source_license=observation.source_license,
                source_revision=observation.source_revision,
                observed_at=observation.observed_at,
                retrieved_at=retrieved_at,
                effective_date=observation.effective_date,
                content_hash=content_hash,
                actual_member_count=len(members),
                members=members,
            )
            with self.lock:
                self.repository.publish_universe(snapshot)
            return snapshot
        except (UniverseSourceError, ValueError):
            return self.current()

    def current(self) -> UniverseSnapshot:
        snapshot = self.repository.current_universe("sp500")
        now = self.clock.now().astimezone(UTC)
        if snapshot is None or now - snapshot.retrieved_at > MAX_LKG_AGE:
            raise ApplicationError(
                ErrorCode.STALE_DATA, "No valid S&P 500 snapshot retrieved within 7 days"
            )
        return snapshot

    @staticmethod
    def _member(symbol: str) -> UniverseMember:
        canonical = symbol.strip().upper()
        invalid = any(
            not (character.isascii() and (character.isalnum() or character in ".-"))
            for character in canonical
        )
        if not canonical or invalid:
            raise UniverseSourceError("Invalid S&P 500 symbol")
        instrument = resolve_us(canonical)
        return UniverseMember(
            canonical_symbol=canonical,
            display_symbol=canonical,
            provider_symbol=instrument.provider_symbol,
            instrument=instrument,
        )
