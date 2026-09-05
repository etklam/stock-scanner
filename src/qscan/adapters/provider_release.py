"""One release decision for the pinned Yahoo adapter; no user bypass switches."""

from importlib.metadata import version

from qscan.domain.models import Contract


class ProviderRelease(Contract):
    status: str
    normal_fetch: bool
    blockers: tuple[str, ...]
    limitations: tuple[str, ...] = (
        "Completed US sessions only; intraday provider behavior not observed live",
        "Personal EOD trial, not exchange-certified data or a production SLA",
        "Unknown metadata rejected; cached metadata is historical evidence, "
        "not current verification",
    )
    evidence: str = "docs/phase35-provider-acceptance.json"


# Changed only after reviewing the recorded acceptance evidence, never by CLI flags.
ACCEPTED_VERSIONS: dict[str, str] = {"yfinance": "0.2.66", "pandas": "2.3.3", "numpy": "2.5.2"}


def yahoo_release() -> ProviderRelease:
    blockers = tuple(
        f"Unaccepted {name} version"
        for name, accepted in ACCEPTED_VERSIONS.items()
        if version(name) != accepted
    )
    if not ACCEPTED_VERSIONS:
        blockers = ("Dated price-basis and metadata acceptance evidence pending",)
    return ProviderRelease(
        status="BLOCKED" if blockers else "EOD_TRIAL", normal_fetch=not blockers, blockers=blockers
    )
