"""Loopback HTTP API: validation, auth and mapping only; services own the logic.

Routes never run downloads, scoring or report rendering inline: every handler is a
sync function doing short DB work, and scans execute on the dedicated worker thread.
"""

import hashlib
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Receive, Scope, Send

from qscan import __version__
from qscan.adapters.report_renderer import render
from qscan.application.contracts import (
    ApplicationError,
    Clock,
    Comparison,
    Run,
    SavedReview,
    ScanResult,
)
from qscan.application.daily import DailyCoordinator
from qscan.application.publication import PublicationService
from qscan.bootstrap import Application
from qscan.domain.models import ErrorCode, RunState, Stage
from qscan.domain.rules import RuleConfig
from qscan.executor import ScanExecutor
from qscan.interfaces.api import cursors
from qscan.interfaces.api.localauth import TokenAuthenticator
from qscan.interfaces.api.schemas import (
    AutomationJobOut,
    AutomationRunOut,
    AutomationStatusOut,
    CountsOut,
    CreateScan,
    CreateWatchlist,
    ErrorEnvelope,
    InstrumentOut,
    NotificationStatusOut,
    PatchWatchlist,
    ProgressOut,
    PutReview,
    ReplaceSymbols,
    ReportStatusOut,
    ResultOut,
    ResultsPage,
    ReviewsPage,
    RulesetOut,
    SavedReviewOut,
    ScanAccepted,
    ScanLinks,
    ScansPage,
    ScanStatusOut,
    SeriesOut,
    SessionOut,
    UniverseStatusOut,
    WatchlistLinks,
    WatchlistOut,
)
from qscan.runtime import instance_challenge

MAX_BODY_BYTES = 1_000_000
MAX_CURSOR_LIMIT = 200
DEFAULT_CURSOR_LIMIT = 50
MAX_KEY_LENGTH = 128
BROWSER_SESSION_COOKIE = "qscan_session"
# Cursor payload schema version: any pagination/sort change must bump it so old
# cursors die loudly instead of paginating new data with stale semantics.
_CURSOR_VERSION = 1

_STATUS_OF: dict[ErrorCode, int] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.WATCHLIST_VERSION_CONFLICT: 409,
    ErrorCode.WATCHLIST_IN_USE: 409,
    ErrorCode.SCAN_NOT_READY: 409,
    ErrorCode.SCAN_FAILED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.REVIEW_REVISION_CONFLICT: 409,
    ErrorCode.QUEUE_LIMIT_REACHED: 429,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.INVALID_AS_OF_SESSION: 400,
    ErrorCode.SESSION_NOT_COMPLETE: 400,
    ErrorCode.REPORT_ERROR: 500,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.INTERNAL_ERROR: 500,
}
_STAGE_CODES = frozenset(s.value for s in Stage)
_STATE_CODES = frozenset(s.value for s in RunState)


def error_response(
    status: int, code: str, message: str, request_id: UUID, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorEnvelope(
        error={
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": str(request_id),
        }
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


class SecurityMiddleware(BaseHTTPMiddleware):
    """Host/Origin checks plus Bearer or same-origin browser-session auth."""

    def __init__(
        self,
        app: ASGIApp,
        authenticator: TokenAuthenticator,
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
        exempt: frozenset[str],
        exempt_prefixes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self.authenticator = authenticator
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins
        self.exempt = exempt
        self.exempt_prefixes = exempt_prefixes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
        request.state.request_id = uuid4()
        path = request.url.path

        def secured(response: Response) -> Response:
            response.headers["X-Request-Id"] = str(request.state.request_id)
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
                if path.startswith("/api/v1/reports/")
                else "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                "base-uri 'none'"
            )
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        host = request.headers.get("host", "")
        hostname = host.rsplit(":", 1)[0] if "]:" not in host and host.count(":") == 1 else host
        if hostname not in self.allowed_hosts:
            return secured(
                error_response(
                    403, ErrorCode.FORBIDDEN.value, "Host is not allowed", request.state.request_id
                )
            )
        origin = request.headers.get("origin")
        # No CORS is offered: any Origin (including "null") outside the strict
        # allowlist is rejected for every method, preflight or not. The
        # allowlist is formed from the trusted server configuration (own
        # loopback listen origins + explicit dev origins), never reflected
        # from the request.
        if origin is not None and origin not in self.allowed_origins:
            return secured(
                error_response(
                    403,
                    ErrorCode.FORBIDDEN.value,
                    "Origin is not allowed",
                    request.state.request_id,
                )
            )
        if path == "/api/v1/auth/session" and (
            origin not in self.allowed_origins
            or request.headers.get("sec-fetch-site") not in {"same-origin", "none"}
        ):
            return secured(
                error_response(
                    403,
                    ErrorCode.FORBIDDEN.value,
                    "Browser session bootstrap requires same-origin navigation",
                    request.state.request_id,
                )
            )
        anonymous = path in self.exempt or any(
            path == prefix or path.startswith(prefix + "/") for prefix in self.exempt_prefixes
        )
        bearer = self.authenticator.check(request.headers.get("authorization"))
        csrf = self.authenticator.browser_csrf(request.cookies.get(BROWSER_SESSION_COOKIE))
        if not anonymous and not bearer and csrf is None:
            return secured(
                error_response(
                    401,
                    ErrorCode.UNAUTHORIZED.value,
                    "Missing or invalid authentication",
                    request.state.request_id,
                )
            )
        if not anonymous and not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-csrf-token")
            if (
                origin is None
                or supplied is None
                or not secrets.compare_digest(supplied, csrf or "")
            ):
                return secured(
                    error_response(
                        403,
                        ErrorCode.FORBIDDEN.value,
                        "Browser mutation requires same-origin CSRF proof",
                        request.state.request_id,
                    )
                )
        return secured(await call_next(request))


class _BodyTooLarge(StarletteHTTPException):
    """Body exceeded the limit mid-stream.

    Subclasses StarletteHTTPException(413) on purpose: FastAPI's route handler
    re-raises HTTPExceptions but wraps any other receive error in a generic
    400, so a plain Exception would surface as 400/500 instead of 413.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body exceeds the configured limit")


class BodyLimitMiddleware:
    """Enforce the request size cap on Content-Length and on actual bytes read."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4()
        length = Headers(scope=scope).get("content-length")
        if length and length.isdigit() and int(length) > self.limit:
            await self._reject(scope, receive, send, request_id)
            return
        consumed = 0

        async def capped() -> Any:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, capped, send)
        except _BodyTooLarge:
            await self._reject(scope, receive, send, request_id)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, request_id: UUID) -> None:
        await error_response(
            413,
            ErrorCode.PAYLOAD_TOO_LARGE.value,
            "Request body exceeds the configured limit",
            request_id,
        )(scope, receive, send)


def _request_id(request: Request) -> UUID:
    return getattr(request.state, "request_id", uuid4())


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = {
        "fields": [
            {"path": ".".join(str(p) for p in error["loc"][1:]), "type": error["type"]}
            for error in exc.errors()[:10]
        ]
    }
    return error_response(
        422,
        ErrorCode.VALIDATION_ERROR.value,
        "Invalid request body or parameters",
        _request_id(request),
        details,
    )


async def application_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    status = _STATUS_OF.get(exc.code, 500)
    message = str(exc) if status < 500 else "Internal error; local data retained"
    return error_response(status, exc.code.value, message, _request_id(request), exc.details)


async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    mapping = {
        400: (ErrorCode.VALIDATION_ERROR, "Malformed request"),
        404: (ErrorCode.NOT_FOUND, "Resource not found"),
        405: (ErrorCode.METHOD_NOT_ALLOWED, "HTTP method not allowed on this path"),
        413: (ErrorCode.PAYLOAD_TOO_LARGE, "Request body exceeds the configured limit"),
    }
    code, message = mapping.get(exc.status_code, (ErrorCode.INTERNAL_ERROR, "Request failed"))
    status = exc.status_code if code is not ErrorCode.INTERNAL_ERROR else 500
    return error_response(status, code.value, message, _request_id(request))


async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    # No stack traces, exception class names, or local paths in responses.
    return error_response(
        500,
        ErrorCode.INTERNAL_ERROR.value,
        "Internal error; local data retained",
        _request_id(request),
    )


def create_app(
    scanner: Application,
    *,
    token_path: Path,
    queue_limit: int = 20,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
    allowed_origins: tuple[str, ...] = (),
    dev_openapi: bool = False,
    executor: ScanExecutor | None = None,
    coordinator: DailyCoordinator | None = None,
    ui_assets: Path | None = None,
) -> FastAPI:
    """Build the FastAPI app without touching the database or network.

    create_app performs no I/O beyond reading the local token file; lifespan
    startup owns recovery and the worker thread.
    """
    authenticator = TokenAuthenticator(token_path)
    worker = executor
    daily = coordinator
    exempt = frozenset(
        {"/health/live", "/health/ready", "/api/v1/auth/session", "/api/v1/instance/challenge"}
    )
    # The compiled UI shell is static build output (no data, no secrets); it is
    # the only anonymous path besides health. Every /api route still requires
    # the bearer token, and /api 404s are never swallowed by an SPA fallback.
    exempt_prefixes: tuple[str, ...] = ("/ui",)
    if dev_openapi:
        exempt |= frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect"})

    owns_worker = worker is None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal worker, daily
        if worker is None:
            worker = ScanExecutor(scanner)
        if owns_worker:
            worker.start()
        if daily is None:
            daily = DailyCoordinator(scanner, executor=worker)
        if scanner.repository.automation_enabled():
            daily.start()
            daily.request_tick()
        yield
        if daily is not None and daily.alive:
            daily.stop()
        # An injected executor belongs to its caller (serve/tests manage it);
        # only a self-created one is stopped with the app.
        if owns_worker and worker.stop():
            scanner.close()
        # A stuck worker keeps the ownership lock and engine; process exit hands
        # recovery to the next startup.

    app = FastAPI(
        title="qscan local API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if dev_openapi else None,
        redoc_url=None,
        openapi_url="/openapi.json" if dev_openapi else None,
    )
    app.state.queue_limit = queue_limit
    app.state.daily = daily
    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)
    app.add_middleware(
        SecurityMiddleware,
        authenticator=authenticator,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        exempt=exempt,
        exempt_prefixes=exempt_prefixes,
    )
    app.add_exception_handler(RequestValidationError, cast(Any, validation_handler))
    app.add_exception_handler(ApplicationError, cast(Any, application_handler))
    app.add_exception_handler(StarletteHTTPException, cast(Any, http_handler))
    app.add_exception_handler(Exception, cast(Any, unexpected_handler))

    # --- compiled local UI (same origin; assets are the only mount) ---

    # Compiled UI assets ship inside the package: interfaces/web/dist.
    web_dist = ui_assets if ui_assets is not None else Path(__file__).parent.parent / "web" / "dist"
    if web_dist.is_dir() and (web_dist / "index.html").is_file():
        app.mount("/ui", StaticFiles(directory=web_dist, html=True), name="ui")
    else:

        @app.get("/ui/{rest:path}", include_in_schema=False)
        def ui_not_built(rest: str) -> JSONResponse:
            return error_response(
                404,
                ErrorCode.NOT_FOUND.value,
                "The compiled UI is not present at this installation. Build it once "
                "with `npm ci && npm run build` inside web/ (or install a wheel, "
                "which ships the built assets); qscan serve needs no Node at runtime.",
                uuid4(),
            )

    def scoped() -> Application:
        return scanner.for_principal(authenticator.principal)

    def secret() -> str:
        return authenticator.cursor_secret

    Principal = Annotated[Application, Depends(scoped)]
    CursorSecret = Annotated[str, Depends(secret)]

    def daily_coordinator() -> DailyCoordinator:
        value = daily
        if value is None:
            raise ApplicationError(ErrorCode.INTERNAL_ERROR, "Coordinator is unavailable")
        return value

    def automation_out(value: Any) -> AutomationStatusOut:
        run = value.latest_run
        terminal = run is not None and run.state in (
            RunState.SUCCEEDED,
            RunState.PARTIAL,
            RunState.FAILED,
        )
        publication = value.report
        delivery = value.notification
        return AutomationStatusOut(
            enabled=value.enabled,
            latest_completed_session=value.latest_job.session if value.latest_job else None,
            job=AutomationJobOut(
                id=value.latest_job.id,
                session=value.latest_job.session,
                attempt=value.latest_job.attempt,
                run_id=value.latest_job.run_id,
            )
            if value.latest_job
            else None,
            run=AutomationRunOut(
                id=run.id,
                state=run.state.value,
                progress=ProgressOut(**run.progress.model_dump()) if run.progress else None,
                counts=CountsOut(**run.counts.model_dump()) if terminal else None,
            )
            if run
            else None,
            next_due_session=value.next_due_session,
            next_due_time=value.next_due_time,
            universe=UniverseStatusOut(
                snapshot_id=value.universe.id,
                source_url=value.universe.source_url,
                source_license=value.universe.source_license,
                source_revision=value.universe.source_revision,
                retrieved_at=value.universe.retrieved_at,
                member_count=value.universe.actual_member_count,
                freshness=value.universe_freshness,
            )
            if value.universe
            else None,
            report=ReportStatusOut(
                run_id=publication.run_id,
                state=publication.state,
                attempts=publication.attempts,
                url=(
                    f"/api/v1/reports/{publication.run_id}"
                    if publication.state == "PUBLISHED"
                    else None
                ),
                error=publication.error,
            )
            if publication
            else None,
            notification=NotificationStatusOut(
                outcome=delivery.outcome,
                attempts=delivery.attempts,
                detail=delivery.detail,
            )
            if delivery
            else None,
        )

    @app.post("/api/v1/auth/session", include_in_schema=False)
    def browser_session(response: Response) -> dict[str, str]:
        session, csrf = authenticator.issue_browser_session()
        response.set_cookie(
            BROWSER_SESSION_COOKIE,
            session,
            max_age=15 * 60,
            httponly=True,
            samesite="strict",
            path="/api",
        )
        return {"csrf_token": csrf}

    @app.get("/api/v1/instance/challenge", include_in_schema=False)
    def challenge(
        nonce: Annotated[str, Query(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")],
    ) -> dict[str, str]:
        return instance_challenge(
            authenticator.instance_id,
            authenticator.instance_secret,
            scanner.data_dir,
            nonce,
        ) | {"provider": scanner.provider.name}

    # --- packaged managed daily workflow ---

    @app.get("/api/v1/automation/status")
    def automation_status(api: Principal) -> AutomationStatusOut:
        return automation_out(daily_coordinator().status())

    @app.post("/api/v1/automation/enable")
    def automation_enable(api: Principal) -> AutomationStatusOut:
        coordinator_value = daily_coordinator()
        value = coordinator_value.enable()
        coordinator_value.start()
        coordinator_value.request_tick()
        return automation_out(value)

    @app.post("/api/v1/automation/pause")
    def automation_pause(api: Principal) -> AutomationStatusOut:
        coordinator_value = daily_coordinator()
        value = coordinator_value.pause()
        coordinator_value.stop()
        return automation_out(value)

    @app.post("/api/v1/automation/run-now")
    def automation_run_now(api: Principal) -> AutomationStatusOut:
        coordinator_value = daily_coordinator()
        coordinator_value.start()
        return automation_out(coordinator_value.run_now())

    @app.get("/api/v1/reports/latest")
    def latest_report(api: Principal) -> ReportStatusOut:
        publication = api.repository.latest_report_publication()
        if publication is None:
            return ReportStatusOut(state="MISSING")
        return ReportStatusOut(
            run_id=publication.run_id,
            state=publication.state,
            attempts=publication.attempts,
            url=(
                f"/api/v1/reports/{publication.run_id}"
                if publication.state == "PUBLISHED"
                else None
            ),
            error=publication.error,
        )

    @app.get("/api/v1/reports/{identity}", include_in_schema=False)
    def report_file(api: Principal, identity: UUID) -> FileResponse:
        publication = api.repository.report_publication(identity)
        if publication is None or publication.state != "PUBLISHED" or not publication.relative_path:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        root = (api.data_dir / "reports").resolve()
        target = (root / publication.relative_path).resolve()
        if not target.is_relative_to(root):
            raise ApplicationError(ErrorCode.NOT_FOUND)
        if not target.is_file():
            publication = PublicationService(api.repository, api.reports, api.data_dir).publish(
                identity
            )
            if not publication.relative_path:
                raise ApplicationError(ErrorCode.NOT_FOUND)
            target = (root / publication.relative_path).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ApplicationError(ErrorCode.NOT_FOUND)
        return FileResponse(target, media_type="text/html; charset=utf-8")

    # --- health (minimal by design: no paths, versions or secrets) ---

    @app.get("/health/live")
    def live() -> dict[str, str]:
        # Provider identity lets scheduler wrappers refuse a mismatched
        # --provider flag instead of silently deduplicating against it.
        return {"status": "alive", "provider": scanner.provider.name}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        database = "ok"
        try:
            with scanner.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
        except Exception:
            database = "unavailable"
        worker_ok = worker is not None and worker.alive
        if database != "ok" or not worker_ok:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "database": database,
                    "executor": "ok" if worker_ok else "stopped",
                },
            )
        return JSONResponse(content={"status": "ready"})

    # --- watchlists ---

    def watchlist_out(value: Any) -> WatchlistOut:
        identity = str(value.id)
        return WatchlistOut(
            id=value.id,
            name=value.name,
            revision=value.revision,
            symbols=tuple(i.display_symbol for i in value.instruments),
            links=WatchlistLinks(
                self=f"/api/v1/watchlists/{identity}",
                symbols=f"/api/v1/watchlists/{identity}/symbols",
            ),
        )

    @app.get("/api/v1/watchlists")
    def list_watchlists(api: Principal) -> list[WatchlistOut]:
        return [watchlist_out(w) for w in api.watchlists.list()]

    @app.post("/api/v1/watchlists", status_code=201)
    def create_watchlist(api: Principal, body: CreateWatchlist) -> WatchlistOut:
        return watchlist_out(api.watchlists.create(body.name, body.symbols))

    @app.get("/api/v1/watchlists/{identity}")
    def get_watchlist(api: Principal, identity: UUID) -> WatchlistOut:
        return watchlist_out(api.watchlists.lookup(str(identity)))

    @app.patch("/api/v1/watchlists/{identity}")
    def patch_watchlist(api: Principal, identity: UUID, body: PatchWatchlist) -> WatchlistOut:
        return watchlist_out(api.watchlists.rename(identity, body.name, body.expected_revision))

    @app.delete("/api/v1/watchlists/{identity}", status_code=204)
    def delete_watchlist(api: Principal, identity: UUID) -> Response:
        api.watchlists.delete(identity)
        return Response(status_code=204)

    @app.put("/api/v1/watchlists/{identity}/symbols")
    def put_symbols(api: Principal, identity: UUID, body: ReplaceSymbols) -> WatchlistOut:
        current = api.watchlists.lookup(str(identity))
        value = api.watchlists.import_content(
            current.name,
            "\n".join(body.symbols).encode("utf-8"),
            "txt",
            watchlist_id=identity,
            expected_revision=body.expected_revision,
        )
        return watchlist_out(value)

    # --- rulesets ---

    @app.get("/api/v1/rulesets")
    def rulesets() -> list[RulesetOut]:
        config = RuleConfig()
        return [
            RulesetOut(
                id=config.ruleset_id,
                version=config.version,
                windows=tuple(config.windows),
                minimum_history=config.minimum_history,
                description="Close-only breakout setup pre-screen (V1 baseline)",
            )
        ]

    # --- scans ---

    def links_for(run: Run) -> ScanLinks:
        base = f"/api/v1/scans/{run.id}"
        return ScanLinks(
            self=base,
            results=base + "/results",
            changes=base + "/changes",
            export=base + "/export?format=csv",
            watchlist=f"/api/v1/watchlists/{run.watchlist.id}",
        )

    def status_out(run: Run) -> ScanStatusOut:
        terminal = run.state in (RunState.SUCCEEDED, RunState.PARTIAL, RunState.FAILED)
        return ScanStatusOut(
            id=run.id,
            state=run.state.value,
            as_of_session=run.context.as_of_session,
            reference_session=run.context.reference_session,
            watchlist_id=run.watchlist.id,
            watchlist_revision=run.watchlist.revision,
            ruleset_id=run.rules.ruleset_id,
            ruleset_version=run.rules.version,
            engine_version=run.context.engine_version,
            data_mode=run.data_mode.value,
            requested_at=run.requested_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            progress=ProgressOut(
                phase=run.progress.phase,
                processed_symbols=run.progress.processed_symbols,
                total_symbols=run.progress.total_symbols,
                updated_at=run.progress.updated_at,
            )
            if run.progress
            else None,
            counts=CountsOut(**run.counts.model_dump()) if terminal else None,
            error=run.error.value if run.error else None,
            warnings=run.warnings,
            source_run_id=run.source_run_id,
            links=links_for(run),
        )

    def request_hash(body: CreateScan) -> str:
        canonical = json.dumps(
            {
                "endpoint": "/api/v1/scans",
                "watchlist_id": str(body.watchlist_id),
                "ruleset_id": body.ruleset_id,
                "as_of_session": body.as_of_session.isoformat() if body.as_of_session else None,
                "data_mode": body.data_mode.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def replay(api: Application, winner: tuple[UUID, str], digest: str) -> JSONResponse:
        identity, winner_hash = winner
        if winner_hash != digest:
            raise ApplicationError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was already used with a different request",
            )
        run = api.repository.run_summary(identity)
        # Retries show the real current state; a finished job is never faked as QUEUED.
        return JSONResponse(
            status_code=200,
            content=status_out(run).model_dump(mode="json"),
            headers={"Location": f"/api/v1/scans/{identity}"},
        )

    @app.post(
        "/api/v1/scans",
        status_code=202,
        responses={
            202: {
                "model": ScanAccepted,
                "description": "Scan accepted for asynchronous execution",
            },
            200: {
                "model": ScanStatusOut,
                "description": "Idempotency-key replay: current status of the original scan",
            },
        },
    )
    def submit_scan(
        api: Principal,
        request: Request,
        body: CreateScan,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=16, max_length=MAX_KEY_LENGTH)
        ],
    ) -> JSONResponse:
        digest = request_hash(body)
        existing = api.repository.run_by_idempotency(idempotency_key)
        # An existing key wins over re-resolving mutable defaults (today's session,
        # current watchlist revision): the originally accepted job is returned.
        if existing is not None:
            return replay(api, existing, digest)
        run = api.scans.prepare(body.watchlist_id, as_of=body.as_of_session, mode=body.data_mode)
        created, winner = api.repository.enqueue(
            run, idempotency_key, digest, request.app.state.queue_limit
        )
        if not created:
            assert winner is not None
            return replay(api, winner, digest)
        if worker is not None:
            worker.poke()
        return JSONResponse(
            status_code=202,
            content=ScanAccepted(
                id=run.id,
                as_of_session=run.context.as_of_session,
                watchlist_revision=run.watchlist.revision,
                ruleset_version=run.rules.version,
                links=links_for(run),
            ).model_dump(mode="json"),
            headers={"Location": f"/api/v1/scans/{run.id}"},
        )

    @app.get("/api/v1/sessions/current")
    def sessions_current(api: Principal) -> SessionOut:
        """Latest completed market session; read-only scheduling input."""
        context = api.scans.current_session()
        return SessionOut(
            as_of_session=context.as_of_session, reference_session=context.reference_session
        )

    @app.get("/api/v1/scans")
    def list_scans(
        api: Principal,
        secret: CursorSecret,
        limit: Annotated[int, Query(ge=1, le=MAX_CURSOR_LIMIT)] = DEFAULT_CURSOR_LIMIT,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        state: Annotated[str | None, Query()] = None,
    ) -> ScansPage:
        filters: dict[str, Any] = {}
        if state is not None:
            if state not in _STATE_CODES:
                raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Unknown scan state filter")
            filters["state"] = state
        after = None
        if cursor is not None:
            payload = cursors.decode_cursor(cursor, secret, "scans", authenticator.principal)
            if payload.get("v") != _CURSOR_VERSION or payload.get("f") != filters:
                raise ApplicationError(
                    ErrorCode.VALIDATION_ERROR, "Cursor does not match this query"
                )
            after = (payload["k"][0], payload["k"][1])
        page, more = api.queries.page(after=after, limit=limit, state=state)
        next_cursor = None
        if more and page:
            last = page[-1]
            key = [last.requested_at.isoformat(), str(last.id)]
            next_cursor = cursors.encode_cursor(
                {
                    "r": "scans",
                    "v": _CURSOR_VERSION,
                    "p": authenticator.principal,
                    "f": filters,
                    "k": key,
                },
                secret,
            )
        return ScansPage(items=[status_out(run) for run in page], next_cursor=next_cursor)

    @app.get("/api/v1/scans/{identity}")
    def get_scan(api: Principal, identity: UUID) -> ScanStatusOut:
        return status_out(api.repository.run_summary(identity))

    def published(run: Run) -> Run:
        if run.state in (RunState.QUEUED, RunState.RUNNING):
            raise ApplicationError(ErrorCode.SCAN_NOT_READY, "Scan has not published results yet")
        if run.state == RunState.FAILED:
            raise ApplicationError(ErrorCode.SCAN_FAILED, "Scan failed; see status diagnostics")
        return run

    def result_out(value: ScanResult) -> ResultOut:
        return ResultOut(
            instrument=InstrumentOut(
                id=value.instrument.id,
                display_symbol=value.instrument.display_symbol,
                exchange=value.instrument.exchange,
                currency=value.instrument.currency,
                instrument_type=value.instrument.instrument_type,
            ),
            analysis=value.analysis,
            reasons=value.analysis.reasons,
            warnings=value.warnings,
            category=value.category,
            rank=value.rank,
            alternative_windows=value.analysis.alternative_windows,
        )

    @app.get("/api/v1/scans/{identity}/results")
    def results_page(
        api: Principal,
        identity: UUID,
        secret: CursorSecret,
        limit: Annotated[int, Query(ge=1, le=MAX_CURSOR_LIMIT)] = DEFAULT_CURSOR_LIMIT,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        stage: Annotated[str | None, Query()] = None,
        candidate: Annotated[bool | None, Query()] = None,
    ) -> ResultsPage:
        published(api.repository.run_summary(identity))
        if stage is not None and stage not in _STAGE_CODES:
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Unknown stage filter")
        after = None
        if cursor is not None:
            payload = cursors.decode_cursor(cursor, secret, "results", authenticator.principal)
            if payload.get("v") != _CURSOR_VERSION or payload.get("s") != str(identity):
                raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Cursor does not match this run")
            if payload.get("f") != {"stage": stage, "candidate": candidate}:
                raise ApplicationError(
                    ErrorCode.VALIDATION_ERROR, "Cursor does not match this query"
                )
            after = (payload["k"][0], payload["k"][1], payload["k"][2])
        page, more = api.repository.results_page(
            identity, after=after, limit=limit, stage=stage, candidate=candidate
        )
        next_cursor = None
        if more and page:
            last = page[-1]
            score = last.analysis.score
            key = [1 if score is None else 0, score, str(last.instrument.id)]
            next_cursor = cursors.encode_cursor(
                {
                    "r": "results",
                    "v": _CURSOR_VERSION,
                    "s": str(identity),
                    "p": authenticator.principal,
                    "f": {"stage": stage, "candidate": candidate},
                    "k": key,
                },
                secret,
            )
        return ResultsPage(items=[result_out(r) for r in page], next_cursor=next_cursor)

    @app.get("/api/v1/scans/{identity}/results/{instrument_id}")
    def result_detail(api: Principal, identity: UUID, instrument_id: UUID) -> ResultOut:
        run = published(api.repository.run(identity))
        matches = [r for r in run.results if r.instrument.id == instrument_id]
        if not matches:
            raise ApplicationError(ErrorCode.NOT_FOUND)
        return result_out(matches[0])

    @app.get("/api/v1/scans/{identity}/series/{instrument_id}")
    def series(
        api: Principal,
        identity: UUID,
        instrument_id: UUID,
        limit: Annotated[int, Query(ge=1, le=504)] = 126,
    ) -> SeriesOut:
        run = api.repository.run_summary(identity)
        if run.state in (RunState.QUEUED, RunState.RUNNING):
            raise ApplicationError(ErrorCode.SCAN_NOT_READY, "Run has no snapshot yet")
        chart = api.reports.series(identity, instrument_id)
        sessions = chart.sessions[-limit:]
        start = len(chart.sessions) - len(sessions)
        # SMAs are computed on the full run history, then the display range is cut.
        return SeriesOut(
            run_id=run.id,
            instrument_id=chart.instrument_id,
            symbol=chart.symbol,
            sessions=sessions,
            closes=chart.closes[start:],
            sma={n: values[start:] for n, values in chart.sma.items()},
            window_start=chart.window_start,
            window_end=chart.window_end,
            close_resistance=chart.close_resistance,
            displayed_sessions=len(sessions),
            price_basis=run.price_basis,
            as_of_session=run.context.as_of_session,
        )

    def review_out(value: SavedReview) -> SavedReviewOut:
        return SavedReviewOut(
            run_id=value.run_id,
            instrument_id=value.instrument_id,
            label=value.label,
            note=value.note,
            revision=value.revision,
            updated_at=value.updated_at,
        )

    @app.get("/api/v1/scans/{identity}/reviews")
    def list_reviews(api: Principal, identity: UUID) -> ReviewsPage:
        # One batched query for the whole table: no per-row requests.
        return ReviewsPage(items=tuple(review_out(r) for r in api.reviews.list(identity)))

    @app.put("/api/v1/scans/{identity}/reviews/{instrument_id}")
    def put_review(
        api: Principal,
        identity: UUID,
        instrument_id: UUID,
        body: PutReview,
    ) -> SavedReviewOut:
        # Only instruments with a valid evaluation can be labeled; a conflicting
        # expected revision raises 409 REVIEW_REVISION_CONFLICT (never a silent
        # overwrite). Scores/ranks/hashes are untouched by definition.
        review: SavedReview = api.reviews.save(
            identity,
            instrument_id,
            body.label,
            body.note,
            body.expected_revision,
        )
        return review_out(review)

    @app.get(
        "/api/v1/scans/{identity}/changes",
        responses={
            200: {
                "model": Comparison,
                "description": "Persisted comparison against the bound baseline run",
            }
        },
    )
    def changes(api: Principal, identity: UUID) -> JSONResponse:
        published(api.repository.run_summary(identity))
        comparison = api.comparisons.get(identity)
        # The persisted binding is returned as-is; baselines are never reselected.
        return JSONResponse(content=comparison.model_dump(mode="json"))

    @app.get(
        "/api/v1/scans/{identity}/export",
        response_class=Response,
        responses={
            200: {
                "description": "CSV report download",
                "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
                "headers": {
                    "Content-Disposition": {
                        "schema": {"type": "string"},
                        "description": "Attachment filename for the scan report",
                    },
                    "X-Scan-State": {"schema": {"type": "string"}},
                    "X-Result-Count": {"schema": {"type": "integer"}},
                },
            }
        },
    )
    def export_csv(
        api: Principal,
        identity: UUID,
        format: Annotated[Literal["csv"], Query()] = "csv",
        all_results: Annotated[bool, Query()] = False,
    ) -> Response:
        if format != "csv":
            raise ApplicationError(ErrorCode.VALIDATION_ERROR, "Only format=csv is supported")
        run = api.repository.run_summary(identity)
        if run.state in (RunState.QUEUED, RunState.RUNNING):
            raise ApplicationError(ErrorCode.SCAN_NOT_READY, "Scan has not published results yet")
        report = api.reports.build(identity, top=50, include_charts=False)
        content = render(report, "csv", all_results=all_results)
        rows = (
            len(report.run.results)
            if all_results
            else sum(1 for r in report.run.results if r.rank is not None)
        )
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="scan-{run.id}.csv"',
                "X-Scan-State": run.state.value,
                "X-Result-Count": str(rows),
            },
        )

    # The default OpenAPI document describes 422s as Starlette's HTTPValidationError,
    # but every validation failure actually returns the ErrorEnvelope below; fix the
    # contract in one place so the served and snapshotted schemas match the wire.
    original_openapi = app.openapi

    def openapi_with_error_envelope() -> dict[str, Any]:
        schema = original_openapi()
        components = schema["components"]["schemas"]
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        envelope = ErrorEnvelope.model_json_schema(ref_template="#/components/schemas/{model}")
        for key in ("$defs", "definitions"):
            components.update(envelope.pop(key, {}))
        components["ErrorEnvelope"] = envelope

        def replace(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("$ref") == "#/components/schemas/HTTPValidationError":
                    node["$ref"] = "#/components/schemas/ErrorEnvelope"
                for value in node.values():
                    replace(value)
            elif isinstance(node, list):
                for value in node:
                    replace(value)

        replace(schema)
        return schema

    app.openapi = openapi_with_error_envelope  # type: ignore[method-assign]
    return app


def run_serve(
    provider: Any,
    *,
    data_dir: Path,
    token_path: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    queue_limit: int = 20,
    stop_grace: float = 30.0,
    clock: Clock | None = None,
    dev_openapi: bool = False,
    dev_origins: tuple[str, ...] = (),
    automation: bool = False,
    log_level: str = "warning",
) -> int:
    """Entry point for `qscan serve`: ownership, recovery, executor, HTTP, shutdown."""
    from filelock import FileLock, Timeout

    from qscan.bootstrap import bootstrap
    from qscan.executor import NullLock
    from qscan.interfaces.api.localauth import ensure_token
    from qscan.runtime import is_loopback

    if not is_loopback(host):
        emit_error("FORBIDDEN", "serve is restricted to loopback")
        return 2

    created = ensure_token(token_path)[1]
    try:
        scanner = bootstrap(
            provider, data_dir=data_dir, initialize=False, service_lock=NullLock(), clock=clock
        )
    except ApplicationError as exc:
        emit_error(exc.code.value, str(exc) or "Run qscan init to create the data directory")
        return 2
    ownership = FileLock(scanner.data_dir / "executor.lock", timeout=5)
    try:
        ownership.acquire(timeout=5)
    except Timeout:
        emit_error("EXECUTOR_LOCKED", "Executor already owned by another local process")
        scanner.close()
        return 4
    executor = ScanExecutor(scanner, stop_grace=stop_grace)
    recovered = executor.start()
    origin_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    coordinator = DailyCoordinator(
        scanner,
        executor=executor,
        report_base_url=f"http://{origin_host}:{port}/ui/",
    )
    if automation:
        coordinator.enable()
    print(
        f"qscan serve: engine {__version__}, provider {provider.name}, "
        f"recovered {recovered} interrupted run(s)",
        file=sys.stderr,
        flush=True,
    )
    # The Origin allowlist is formed from the TRUSTED server configuration:
    # the loopback origins this server itself listens on, plus explicit dev
    # origins (e.g. the Vite dev server) given by the operator. The request's
    # Host/Origin is never reflected into it.
    origins = tuple(f"http://{listen_host}:{port}" for listen_host in ("127.0.0.1", "localhost"))
    app = create_app(
        scanner,
        token_path=token_path,
        queue_limit=queue_limit,
        allowed_origins=(*origins, *dev_origins),
        dev_openapi=dev_openapi,
        executor=executor,
        coordinator=coordinator,
    )
    if created:
        print(f"Local API token created at {token_path}", file=sys.stderr, flush=True)
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=log_level,
            access_log=False,
            timeout_graceful_shutdown=int(stop_grace),
        )
    finally:
        if coordinator.alive:
            coordinator.stop()
        if executor.stop():
            with suppress(Exception):
                ownership.release()
            scanner.close()
        else:
            # The worker thread is non-daemon and still writing: returning would
            # drop the lock reference, filelock's __del__ would force-release it,
            # and a second serve could own the directory mid-write. Exit the
            # process so worker and lock die together; the RUNNING scan is
            # recovered as WORKER_INTERRUPTED by the next startup.
            print(
                "Worker still busy after the stop grace; exiting now. The running "
                "scan is recovered as WORKER_INTERRUPTED on the next startup.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(1)
    return 0


def emit_error(code: str, message: str) -> None:
    from qscan.interfaces.cli import emit

    emit({"error": {"code": code, "message": message}})
