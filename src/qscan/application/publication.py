"""Render terminal runs once and atomically publish owner-scoped daily reports."""

import os
import tempfile
from pathlib import Path
from uuid import UUID

from qscan.adapters.report_renderer import render
from qscan.application.contracts import ApplicationError, ReportPublication, Repository
from qscan.application.reporting import ReportService
from qscan.domain.models import ErrorCode


class PublicationService:
    def __init__(self, repository: Repository, reports: ReportService, data_dir: Path) -> None:
        self.repository = repository
        self.reports = reports
        self.root = data_dir / "reports"

    def publish(self, identity: UUID) -> ReportPublication:
        current = self.repository.report_publication(identity)
        if current is not None and current.state == "PUBLISHED" and current.relative_path:
            existing = (self.root / current.relative_path).resolve()
            if existing.is_relative_to(self.root.resolve()) and existing.is_file():
                return current
        temporary: str | None = None
        try:
            report = self.reports.build(identity)
            content = render(report, "html")
            relative = Path(report.run.context.as_of_session.isoformat()) / f"scan-{identity}.html"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            return self.repository.record_report_success(identity, relative.as_posix())
        except Exception as exc:
            self.repository.record_report_failure(identity, type(exc).__name__)
            raise ApplicationError(
                ErrorCode.REPORT_ERROR,
                "Daily report publication failed; retry by run ID without rescanning",
            ) from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
