"Server-rendered administration UI. All writes require a signed session and CSRF."

import fcntl
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .domain import Notification, Rule, ValidationError, ids, validate_rule
from .service import Service
from .store import Store

PACKAGE = Path(__file__).parent


def create_app(
    data_dir: Path | None = None,
    password: str | None = None,
    *,
    worker: bool = True,
    client: httpx.Client | None = None,
) -> FastAPI:
    directory = data_dir or Path(os.getenv("NOTIFIER_DATA_DIR", ".data"))
    store = Store(
        directory,
        password if password is not None else os.getenv("NOTIFIER_ADMIN_PASSWORD", ""),
    )
    http = client or httpx.Client(timeout=20, trust_env=False)
    service = Service(store, http)
    templates = Jinja2Templates(directory=PACKAGE / "templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        thread = None
        lock_file = None
        if worker:
            lock_file = (directory / "worker.lock").open("w")
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                raise RuntimeError(
                    "Run exactly one application process per data volume."
                ) from None
            thread = threading.Thread(target=service.run, args=(stop,), daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                # Stop is checked between sends; allow an in-flight request to finish.
                import asyncio

                await asyncio.to_thread(thread.join, 30)
            if lock_file:
                lock_file.close()
            if client is None:
                http.close()

    app = FastAPI(
        title="Illyriad Notifier",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.service = store, service
    app.add_middleware(
        SessionMiddleware,
        secret_key=store.session_secret,
        session_cookie="notifier_session",
        same_site="strict",
        https_only=os.getenv("NOTIFIER_SECURE_COOKIE", "false").lower() == "true",
        max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'none'; img-src "
            "'self' data:; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def render(request: Request, template: str, status_code: int = 200, **context: Any):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        notice = request.session.pop("notice", "")
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={
                "csrf": request.session["csrf"],
                "notice": notice,
                "path": request.url.path,
                "demo": store.metadata().get("demo") == "1",
                **context,
            },
            status_code=status_code,
        )

    def redirect(request: Request, path: str, notice: str = "") -> RedirectResponse:
        if notice:
            request.session["notice"] = notice
        return RedirectResponse(path, status_code=303)

    def require_user(request: Request) -> None:
        if request.session.get("user") != store.metadata().get("session_version"):
            raise HTTPException(303, headers={"Location": "/login"})

    def check_csrf(request: Request, csrf: Annotated[str, Form()]) -> None:
        if (
            not secrets.compare_digest(csrf, request.session.get("csrf", ""))
            or not csrf
        ):
            raise HTTPException(
                403, "This form expired. Reload the page and try again."
            )

    private = APIRouter(dependencies=[Depends(require_user)])
    write = APIRouter(dependencies=[Depends(require_user), Depends(check_csrf)])

    @app.exception_handler(ValidationError)
    def validation_error(request: Request, exc: ValidationError):
        return render(request, "error.html", status_code=400, error=str(exc))

    @app.get("/healthz")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login(request: Request, password: Annotated[str, Form()]):
        now = datetime.now(UTC)
        # A global single-admin limiter cannot be bypassed by spoofing IPs.
        with service.lock:
            meta = store.metadata()
            if meta.get("login_blocked_until") and now < datetime.fromisoformat(
                meta["login_blocked_until"]
            ):
                return render(
                    request,
                    "login.html",
                    status_code=429,
                    error="Too many attempts. Try again in five minutes.",
                )
            if len(password) > 200 or not store.authenticate(password):
                failures = int(meta.get("login_failures", "0")) + 1
                with store.connection() as db:
                    store.set_meta(db, "login_failures", str(failures % 5))
                    if failures >= 5:
                        store.set_meta(
                            db,
                            "login_blocked_until",
                            (now + timedelta(minutes=5)).isoformat(),
                        )
                return render(
                    request, "login.html", status_code=401, error="Incorrect password."
                )
            with store.connection() as db:
                store.set_meta(db, "login_failures", "0")
                store.set_meta(db, "login_blocked_until", "")
            request.session.clear()
            request.session.update(
                user=store.metadata()["session_version"], csrf=secrets.token_urlsafe(32)
            )
        return redirect(request, "/")

    @write.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect(request, "/login")

    @private.get("/", response_class=HTMLResponse)
    def inbox(
        request: Request,
        page: int = 1,
        town: str = "",
        type_id: str = "",
        category: str = "",
        status: str = "",
        unread: bool = False,
        since: str = "",
        until: str = "",
        q: str = "",
    ):
        page = max(1, min(page, 100000))
        try:
            since_iso = (
                datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
                if since
                else ""
            )
            until_iso = (
                (
                    datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=UTC)
                    + timedelta(days=1)
                ).isoformat()
                if until
                else ""
            )
        except ValueError:
            raise ValidationError("Use valid dates for the inbox filters.") from None
        events, total = store.list_events(
            page=page,
            town=town,
            type_id=type_id,
            category=category,
            status=status,
            unread=unread,
            since=since_iso,
            until=until_iso,
            query=q,
        )
        catalog = store.catalog()
        labels = {
            kind: {v["id"]: v["label"] for v in rows} for kind, rows in catalog.items()
        }
        return render(
            request,
            "inbox.html",
            events=events,
            total=total,
            page=page,
            stats=store.stats(),
            meta=store.metadata(),
            catalog=catalog,
            labels=labels,
            filters={
                "town": town,
                "type_id": type_id,
                "category": category,
                "status": status,
                "unread": unread,
                "since": since,
                "until": until,
                "q": q,
            },
            previous=str(request.url.include_query_params(page=page - 1)),
            following=str(request.url.include_query_params(page=page + 1)),
        )

    @private.get("/notifications/{event_id}", response_class=HTMLResponse)
    def detail(request: Request, event_id: str):
        event = store.event(event_id)
        if event is None:
            raise HTTPException(404, "Notification not found")
        return render(request, "detail.html", event=event)

    @write.post("/notifications/{event_id}/read")
    def mark_read(
        request: Request, event_id: str, read: Annotated[bool, Form()] = True
    ):
        with store.connection() as db:
            db.execute(
                "UPDATE notifications SET is_read=? WHERE id=?", (read, event_id)
            )
        return redirect(request, f"/notifications/{event_id}")

    @write.post("/notifications/{event_id}/retry")
    def retry(request: Request, event_id: str):
        changed = service.retry(event_id, datetime.now(UTC))
        return redirect(
            request,
            f"/notifications/{event_id}",
            "Queued for retry."
            if changed
            else "Only failed deliveries can be retried.",
        )

    def rule_form(request: Request, rule: Rule | None = None):
        rule = rule or Rule(name="")
        catalog = store.catalog()
        for kind, selected in (
            ("town", rule.town_ids),
            ("type", rule.type_ids),
            ("category", rule.category_ids),
        ):
            known = {r["id"] for r in catalog[kind]}
            for value in selected.split(","):
                if value and value not in known:
                    catalog[kind].append({"id": value, "label": kind.title()})
        return render(
            request, "rules.html", rules=store.rules(), rule=rule, catalog=catalog
        )

    @private.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request):
        return rule_form(request)

    @private.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
    def edit_rule(request: Request, rule_id: int):
        rule = next((r for r in store.rules() if r.id == rule_id), None)
        if rule is None:
            raise HTTPException(404, "Rule not found")
        return rule_form(request, rule)

    def submitted_rule(
        name: Annotated[str, Form()],
        position: Annotated[int, Form()] = 10,
        enabled: Annotated[bool, Form()] = False,
        type_ids: Annotated[list[str] | None, Form()] = None,
        category_ids: Annotated[list[str] | None, Form()] = None,
        town_ids: Annotated[list[str] | None, Form()] = None,
        action: Annotated[str, Form()] = "keep",
        topic: Annotated[str, Form()] = "",
        priority: Annotated[int, Form()] = 3,
        rule_id: Annotated[int, Form()] = 0,
    ) -> Rule:
        return validate_rule(
            Rule(
                name,
                position,
                enabled,
                ",".join(type_ids or []),
                ",".join(category_ids or []),
                ",".join(town_ids or []),
                action,
                topic,
                priority,
                rule_id,
            )
        )

    @write.post("/rules/save")
    def save_rule(request: Request, rule: Annotated[Rule, Depends(submitted_rule)]):
        store.save_rule(rule)
        return redirect(
            request,
            "/rules",
            "Rule saved. Changes apply to newly imported notifications.",
        )

    @write.post("/rules/preview", response_class=HTMLResponse)
    def preview_rule(request: Request, rule: Annotated[Rule, Depends(submitted_rule)]):
        with store.connection() as db:
            rows = db.execute(
                "SELECT * FROM notifications ORDER BY occurred_at DESC LIMIT 200"
            ).fetchall()
        matches = [
            dict(r)
            for r in rows
            if rule.matches(
                Notification(
                    r["id"],
                    r["type_id"],
                    r["category_id"],
                    r["town_id"],
                    r["detail"],
                    r["occurred_at"],
                )
            )
        ]
        return render(
            request, "preview.html", rule=rule, matches=matches, sampled=len(rows)
        )

    @write.post("/rules/{rule_id}/delete")
    def delete_rule(request: Request, rule_id: int):
        store.delete_rule(rule_id)
        return redirect(
            request,
            "/rules",
            "Rule deleted. Existing delivery decisions are unchanged.",
        )

    @private.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        settings = store.settings()
        configured = {k: bool(settings.pop(k)) for k in ("api_key", "ntfy_token")}
        return render(
            request,
            "settings.html",
            settings=settings,
            configured=configured,
            meta=store.metadata(),
            catalog=store.catalog(),
        )

    @write.post("/settings")
    def save_settings(
        request: Request,
        api_key: Annotated[str, Form()] = "",
        ntfy_url: Annotated[str, Form()] = "",
        ntfy_token: Annotated[str, Form()] = "",
        topic: Annotated[str, Form()] = "illyriad",
        poll_minutes: Annotated[int, Form()] = 60,
        retention_days: Annotated[int, Form()] = 90,
        delivery_enabled: Annotated[bool, Form()] = False,
        clear_ntfy_token: Annotated[bool, Form()] = False,
    ):
        service.save_settings(
            {
                "api_key": api_key,
                "ntfy_url": ntfy_url,
                "ntfy_token": ntfy_token,
                "topic": topic,
                "poll_minutes": poll_minutes,
                "retention_days": retention_days,
                "delivery_enabled": delivery_enabled,
                "clear_ntfy_token": clear_ntfy_token,
            }
        )
        return redirect(request, "/settings", "Settings saved.")

    @write.post("/settings/poll")
    def poll(request: Request):
        return redirect(request, "/settings", service.poll(datetime.now(UTC)))

    @write.post("/settings/test-ntfy")
    def test_ntfy(request: Request):
        service.send_test()
        return redirect(request, "/settings", "Test message accepted by ntfy.")

    @write.post("/settings/labels")
    def save_label(
        request: Request,
        kind: Annotated[str, Form()],
        source_id: Annotated[str, Form()],
        label: Annotated[str, Form()],
    ):
        if (
            kind not in {"town", "type", "category"}
            or not ids(source_id)
            or "," in source_id
            or len(label) > 100
        ):
            raise ValidationError(
                "Choose a valid ID and a label of up to 100 characters."
            )
        with store.connection() as db:
            if label.strip():
                db.execute(
                    (
                        "INSERT INTO labels VALUES(?,?,?) ON "
                        "CONFLICT(kind,source_id) DO "
                        "UPDATE SET label=excluded.label"
                    ),
                    (kind, source_id.strip(), label.strip()),
                )
            else:
                db.execute(
                    "DELETE FROM labels WHERE kind=? AND source_id=?",
                    (kind, source_id.strip()),
                )
        return redirect(request, "/settings", "Display label updated.")

    @write.post("/settings/password")
    def password_change(
        request: Request,
        current_password: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        confirm_password: Annotated[str, Form()],
    ):
        if len(current_password) > 200 or not store.authenticate(current_password):
            raise ValidationError("The current password is incorrect.")
        if new_password != confirm_password:
            raise ValidationError("The new passwords do not match.")
        try:
            store.change_password(new_password)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        request.session.clear()
        return redirect(request, "/login", "Password changed. Sign in again.")

    app.include_router(private)
    app.include_router(write)
    return app
