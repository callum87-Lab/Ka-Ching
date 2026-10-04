import asyncio


import calendar


import contextvars


import csv


import contextlib
import hashlib


import hmac


import io


import json


import logging


import math


import os


import re


import secrets


import statistics


import sqlite3


from datetime import date, datetime, timedelta, timezone


from urllib.parse import quote, urlparse


from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile


from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, PlainTextResponse


from starlette.background import BackgroundTask


from fastapi.staticfiles import StaticFiles


from fastapi.templating import Jinja2Templates


from pydantic import BaseModel


from . import db, notifications, parser


APP_DIR = os.path.dirname(__file__)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


logger = logging.getLogger("kaching")


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """Start-up: prepare the database, then start the background jobs
    (daily and weekly notifications, daily backup)."""
    startup()
    await start_notification_scheduler()
    yield


app = FastAPI(title="Ka-Ching!", lifespan=_lifespan)


templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


APP_VERSION = "3.2.0"


templates.env.globals["app_version"] = APP_VERSION


app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


CURRENCY_SYMBOLS = {"gbp": "\u00a3", "usd": "$", "eur": "\u20ac"}


_currency_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("currency_symbol", default="\u00a3")


def get_currency_symbol(cur) -> str:
    """Looks up the currency setting directly - for the handful of places
    that build plain text (push notifications, log-style messages) rather
    than rendering a template, where the Jinja global below doesn't reach."""
    code = notifications.get_setting(cur, "currency_symbol", "gbp")
    return CURRENCY_SYMBOLS.get(code, "\u00a3")


@app.middleware("http")
async def currency_context_middleware(request: Request, call_next):
    """Resolves the currency symbol once per request and stashes it in a
    contextvar, so every template can call currency() without every single
    route needing to fetch and pass it through its own context dict."""
    try:
        conn = db.get_db()
        symbol = get_currency_symbol(conn.cursor())
        conn.close()
    except Exception:
        logger.warning("CURRENCY MIDDLEWARE: couldn't read currency setting, defaulting to £", exc_info=True)
        symbol = "\u00a3"
    token = _currency_ctx.set(symbol)
    try:
        return await call_next(request)
    finally:
        _currency_ctx.reset(token)


templates.env.globals["currency"] = lambda: _currency_ctx.get()


def safe_redirect(target, default="/"):
    """Where to send someone after a form: only ever a page within Ka-Ching!
    itself. Anything else - another site (https://..., //host, /\\host),
    or a value with control characters - falls back to the default, so a
    crafted link can't bounce people off to another website."""
    if not isinstance(target, str):
        return default
    candidate = target.strip().replace("\\", "")
    if any(ord(ch) < 32 for ch in candidate):
        return default
    if not urlparse(candidate).netloc and not urlparse(candidate).scheme \
            and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return default


def set_flash(cur, message):
    """A short message shown once at the top of the next page."""
    notifications.set_setting(cur, "_flash_message", message)


def _pop_flash():
    conn = db.get_db()
    cur = conn.cursor()
    message = notifications.get_setting(cur, "_flash_message", "") or ""
    if message:
        notifications.set_setting(cur, "_flash_message", "")
        conn.commit()
    conn.close()
    return message


templates.env.globals["pop_flash"] = _pop_flash


from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def friendly_form_error(request: Request, exc: RequestValidationError):
    """A form that arrives with something missing or unreadable gets a
    normal Ka-Ching! page with a way back - not raw technical text.
    Scripts (JSON requests) still get the detailed JSON answer."""
    wants_page = "text/html" in request.headers.get("accept", "") or \
        request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
    if not wants_page:
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    back = safe_redirect(request.headers.get("referer", "").split(request.url.netloc, 1)[-1] if request.headers.get("referer") else "/", "/")
    return render("error.html", {
        "request": request,
        "heading": "That didn't go through",
        "message": "Something in the form was missing or not filled in properly, so nothing was saved. Go back, check each box, and try again.",
        "back": back,
    }, status_code=422)


def _json_for_script(value):
    """JSON placed inside a page's <script>: escape <, > and & so a name
    containing "</script>" (from an import, a shop or a category) can
    never end the script early and run as code."""
    from markupsafe import Markup
    text = value if isinstance(value, str) else json.dumps(value)
    return Markup(text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))


templates.env.filters["json_script"] = _json_for_script


def render(name, context, **kwargs):
    """Render a page. (Starlette 1.x takes the request as the first
    argument; every context here already carries it.)"""
    return templates.TemplateResponse(context["request"], name, context, **kwargs)


# ---------------------------------------------------------------------------
# Optional login. Off unless a password is set in Settings -> Security, or
# KACHING_PASSWORD is set in docker-compose (which then always applies and
# can't be switched off from the web UI - it doubles as the recovery route).
# Passwords are stored as salted PBKDF2 hashes (Python standard library).
# A sign-in is a signed cookie; changing the password signs everyone out.
# ---------------------------------------------------------------------------
LOGIN_COOKIE = "kc_session"


LOGIN_REMEMBER_DAYS = 30


LOGIN_MAX_FAILS = 5


LOGIN_LOCK_SECONDS = 60


_login_fails = {}   # client ip -> [timestamps of recent failed attempts]


def _env_password():
    return os.environ.get("KACHING_PASSWORD", "").strip() or None


def _hash_password(password, salt=None, iterations=240000):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _check_password_hash(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        return hmac.compare_digest(_hash_password(password, salt, int(iterations)).split("$")[3], digest)
    except (ValueError, AttributeError):
        return False


def _login_settings():
    conn = db.get_db()
    cur = conn.cursor()
    stored = notifications.get_setting(cur, "login_password_hash", "") or ""
    secret = notifications.get_setting(cur, "login_secret", "") or ""
    version = notifications.get_setting(cur, "login_version", "1") or "1"
    if not secret:
        secret = secrets.token_hex(32)
        notifications.set_setting(cur, "login_secret", secret)
        conn.commit()
    conn.close()
    return stored, secret, version


def login_enabled():
    if _env_password():
        return True
    stored, _, _ = _login_settings()
    return bool(stored)


SESSION_PLAIN_HOURS = 24      # "keep me signed in" unticked: ends with the browser, or after a day


def _now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def _load_sessions(cur):
    try:
        data = json.loads(notifications.get_setting(cur, "login_sessions", "{}") or "{}")
    except (ValueError, TypeError):
        return {}
    now = _now_ts()
    return {sid: exp for sid, exp in data.items() if isinstance(exp, int) and exp > now}


def _save_sessions(cur, sessions):
    notifications.set_setting(cur, "login_sessions", json.dumps(sessions))


def _session_value(remember):
    """Start a new signed-in session. Each session has its own random id,
    recorded on the server, so signing out ends it for good - a copied
    cookie stops working too (OWASP ASVS 3.3.1)."""
    _, secret, version = _login_settings()
    expiry = _now_ts() + (LOGIN_REMEMBER_DAYS * 86400 if remember else SESSION_PLAIN_HOURS * 3600)
    sid = secrets.token_urlsafe(18)
    conn = db.get_db()
    cur = conn.cursor()
    sessions = _load_sessions(cur)
    sessions[sid] = expiry
    _save_sessions(cur, sessions)
    conn.commit()
    conn.close()
    body = f"{expiry}.{version}.{sid}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _session_parts(value):
    try:
        expiry, version, sid, sig = (value or "").split(".")
        return int(expiry), version, sid, sig
    except ValueError:
        return None


def _session_valid(value):
    parts = _session_parts(value)
    if not parts:
        return False
    expiry, version, sid, sig = parts
    _, secret, current = _login_settings()
    good = hmac.new(secret.encode(), f"{expiry}.{version}.{sid}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or version != current or expiry <= _now_ts():
        return False
    conn = db.get_db()
    known = sid in _load_sessions(conn.cursor())
    conn.close()
    return known


def _end_session(value):
    """Sign this session out on the server."""
    parts = _session_parts(value)
    if not parts:
        return
    conn = db.get_db()
    cur = conn.cursor()
    sessions = _load_sessions(cur)
    if sessions.pop(parts[2], None) is not None:
        _save_sessions(cur, sessions)
        conn.commit()
    conn.close()


def _end_all_sessions(cur):
    _save_sessions(cur, {})


# The most common passwords (from public breach analyses), refused for the
# login - checked here, with no internet lookup (OWASP ASVS 2.1.7).
COMMON_PASSWORDS = frozenset("""
123456 123456789 12345678 password qwerty123 qwerty 1q2w3e4r 12345 1234567 1234567890 111111 123123
000000 abc123 password1 iloveyou 1234 qwertyuiop 123321 654321 666666 121212 dragon monkey letmein
football baseball welcome welcome1 admin admin123 administrator login princess sunshine master shadow
superman batman trustno1 passw0rd password123 qwerty1 starwars whatever freedom michael charlie
hello123 hello 1qaz2wsx zaq12wsx asdfghjkl asdfgh qazwsx 987654321 7777777 888888 55555 aaaaaa
123qwe 1q2w3e 1qazxsw2 changeme letmein1 football1 liverpool chelsea arsenal manchester password12
p@ssw0rd p@ssword pa55word secret secret123 summer2024 summer2025 summer2026 winter2025 spring2026
autumn2026 london1 kaching kaching123 kaching1 comics comicbook marvel123 starwars1 jedi123 skywalker
yoda123 darthvader 0987654321 1111111111 123456a a123456 abcd1234 abcdef abcdefg abcdefgh 12345a
iloveyou1 lovely loveme mustang access computer internet killer pokemon pikachu minecraft fortnite
football123 password1234 qwerty12345 123456789a qwertyui 11111111 00000000 12341234 147258369
""".split())


def password_problem(password):
    """Why a new password isn't good enough, or None (OWASP ASVS 2.1.1/2.1.7)."""
    if len(password) < 12:
        return "too-short"
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) <= 2 or lowered.rstrip("0123456789!?.") in COMMON_PASSWORDS:
        return "too-common"
    return None


def notify_login_change(cur, what):
    """Tell the owner when the login's details change, if notifications are
    set up (OWASP ASVS 2.2.3). Never stops the change if sending fails."""
    if notifications.get_setting(cur, "notify_provider", "none") in ("", "none"):
        return
    try:
        notifications.send_via_configured_provider(cur, "Ka-Ching! sign-in changed",
                                                   f"The Ka-Ching! login was {what}. If this wasn't you, check Settings → Security.")
    except Exception:
        logger.exception("LOGIN: couldn't send the change notification")


def _is_https(request):
    return request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _set_session_cookie(response, remember, request=None):
    kwargs = {"httponly": True, "samesite": "lax", "path": "/"}
    # Over HTTPS (directly, or behind a reverse proxy that says so) the
    # cookie is marked secure, so a browser never sends it over plain HTTP.
    if request is not None and _is_https(request):
        kwargs["secure"] = True
    if remember:
        kwargs["max_age"] = LOGIN_REMEMBER_DAYS * 86400
    response.set_cookie(LOGIN_COOKIE, _session_value(remember), **kwargs)


def _calendar_feed_key():
    """Private key added to the calendar subscription link while login is
    on - calendar apps can't sign in, so the link itself carries access."""
    conn = db.get_db()
    cur = conn.cursor()
    key = notifications.get_setting(cur, "calendar_feed_key", "") or ""
    if not key:
        key = secrets.token_urlsafe(24)
        notifications.set_setting(cur, "calendar_feed_key", key)
        conn.commit()
    conn.close()
    return key


_LOGIN_OPEN_PATHS = ("/login", "/logout", "/static/", "/sw.js", "/api/sync")


@app.middleware("http")
async def login_middleware(request: Request, call_next):
    path = request.url.path
    if any(path == p or (p.endswith("/") and path.startswith(p)) for p in _LOGIN_OPEN_PATHS):
        return await call_next(request)
    if not login_enabled():
        return await call_next(request)
    if _session_valid(request.cookies.get(LOGIN_COOKIE)):
        return await call_next(request)
    if path == "/calendar/export.ics" and request.query_params.get("key") and hmac.compare_digest(
            request.query_params.get("key"), _calendar_feed_key()):
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        target = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(url="/login?next=" + quote(target, safe=""), status_code=303)
    return PlainTextResponse("Sign in required", status_code=401)


# ---------------------------------------------------------------------------
# Security headers, cross-site request protection and request size limits.
# ---------------------------------------------------------------------------
MAX_REQUEST_BYTES = 110 * 1024 * 1024   # a little over the largest allowed upload
MAX_BACKUP_BYTES = 100 * 1024 * 1024
MAX_SMALL_UPLOAD_BYTES = 1024 * 1024

_CSP = "; ".join([
    "default-src 'self'",
    # the pages' own inline scripts and styles; nothing is ever loaded from elsewhere
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "manifest-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _request_hosts(request):
    hosts = {request.headers.get("host", "").lower()}
    fwd = request.headers.get("x-forwarded-host", "")
    if fwd:
        hosts.add(fwd.split(",")[0].strip().lower())
    return {h for h in hosts if h}


def _cross_site(request):
    """True if a state-changing request came from another website: the
    browser's own Sec-Fetch-Site header says so, or its Origin (or, failing
    that, Referer) names a different site. Requests with neither - scripts,
    the phone app - carry no cookies from a victim's browser and are
    judged by the login and sync key as usual."""
    site = request.headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):   # same-site = another subdomain
        return True
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source == "null"
    return urlparse(source).netloc.lower() not in _request_hosts(request)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
        return PlainTextResponse("Request too large", status_code=413)
    if request.method in _UNSAFE_METHODS and _cross_site(request):
        logger.warning("BLOCKED cross-site %s %s (origin=%s)", request.method, request.url.path,
                       request.headers.get("origin") or request.headers.get("referer"))
        return PlainTextResponse("Blocked: this request came from another website.", status_code=403)
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Pages, exports and backups hold personal data: never keep a copy in
    # the browser's cache (OWASP ASVS 8.2.1). Icons and the like can be.
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    # Reached over HTTPS: tell the browser to always use HTTPS here (ASVS 14.4.5)
    if _is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


async def read_upload(upload, limit):
    """Read an uploaded file, giving up (None) once it passes the limit."""
    chunks, total = [], 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# Registered after the other middleware so it wraps them all: the whole
# request - login check, page, template helpers - shares one database
# connection and one settings cache (see db.get_db).
@app.middleware("http")
async def request_scope_middleware(request: Request, call_next):
    token = db.begin_request()
    try:
        return await call_next(request)
    finally:
        db.end_request(token)


templates.env.globals["login_enabled"] = login_enabled


templates.env.globals["login_env_managed"] = lambda: bool(_env_password())


templates.env.globals["calendar_feed_key"] = lambda: _calendar_feed_key() if login_enabled() else None


DEFAULT_SHIPPING_ESTIMATE = float(os.environ.get("SHIPPING_ESTIMATE", "4.00"))


MIN_SHIPPING_SAMPLES = 3


MAX_PLAUSIBLE_SHIPPING = 15.00


RANGE_CONFIGS = {
    "week":   {"unit": "week",  "back": 4, "forward": 8},
    "month":  {"unit": "month", "back": 3, "forward": 5},
    "6month": {"unit": "month", "back": 0, "forward": 5},
}


RANGE_TABS = [("week", "Week"), ("month", "Month"), ("6month", "6M")]


DEFAULT_CHART_RANGE = "month"


DEFAULT_SOURCE = "Forbidden Planet"


# Off by default - the shipping-groups debug view is a developer
# diagnostic, not something most self-hosters need day to day. Set
# DEBUG_TOOLS_ENABLED=true in your environment to turn it on.
DEBUG_TOOLS_ENABLED = os.environ.get("DEBUG_TOOLS_ENABLED", "false").lower() == "true"


# Forbidden Planet always gets the app's primary accent colour, since it's
# the default/most common source; anything else hashes into the rest of the
# palette so it's still a stable colour across restarts (not Python's
# randomised hash()).
SOURCE_PALETTE = ["#3cf2a6", "#9b7bff", "#ff4d8d", "#ffd166", "#5ec8f2", "#ff9f5e"]


def startup():
    db.init_db()


async def _daily_notification_loop():
    """Runs forever in the background: sleeps until the configured notify
    hour, runs the due-tomorrow check, then sleeps until the next day.
    Wrapped in try/except so one bad night (network blip, bad config)
    doesn't kill the loop for good."""
    while True:
        try:
            conn = db.get_db()
            cur = conn.cursor()
            try:
                notify_hour = int(notifications.get_setting(cur, "notify_hour", "8") or 8)
            except (TypeError, ValueError):
                notify_hour = 8
            conn.close()

            now = datetime.now()
            target = now.replace(hour=notify_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = max(1.0, (target - now).total_seconds())
            logger.info("NOTIFY SCHEDULER: sleeping %.0fs until %s", wait_seconds, target.isoformat())
            await asyncio.sleep(wait_seconds)

            result = await asyncio.to_thread(notifications.check_and_notify_tomorrow)
            logger.info("NOTIFY SCHEDULER: daily check ran, result=%s", result)

            budget_result = await asyncio.to_thread(check_budget_threshold)
            try:
                cat_result = await asyncio.to_thread(check_category_limits)
                logger.info("NOTIFY SCHEDULER: category limit check ran, result=%s", cat_result)
            except Exception:
                logger.exception("NOTIFY SCHEDULER: category limit check failed")
            if budget_result is not None:
                logger.info("NOTIFY SCHEDULER: budget alert check ran, result=%s", budget_result)
        except Exception:
            logger.exception("NOTIFY SCHEDULER: daily check failed, will retry tomorrow")
            await asyncio.sleep(3600)


async def _weekly_notification_loop():
    """Runs forever in the background: once a week, on whichever day is
    configured in Settings, sends the optional weekly digest - same notify
    hour as the daily one, just a different day-of-week gate. Does nothing
    if the weekly digest is turned off."""
    while True:
        try:
            conn = db.get_db()
            cur = conn.cursor()
            try:
                notify_hour = int(notifications.get_setting(cur, "notify_hour", "8") or 8)
            except (TypeError, ValueError):
                notify_hour = 8
            try:
                digest_day = int(notifications.get_setting(cur, "weekly_digest_day", "0") or 0)
            except (TypeError, ValueError):
                digest_day = 0
            conn.close()

            now = datetime.now()
            target = now.replace(hour=notify_hour, minute=0, second=0, microsecond=0)
            days_ahead = (digest_day - now.weekday()) % 7
            target += timedelta(days=days_ahead)
            if target <= now:
                target += timedelta(days=7)
            wait_seconds = max(1.0, (target - now).total_seconds())
            logger.info("WEEKLY NOTIFY SCHEDULER: sleeping %.0fs until %s", wait_seconds, target.isoformat())
            await asyncio.sleep(wait_seconds)

            result = await asyncio.to_thread(notifications.check_and_notify_week)
            logger.info("WEEKLY NOTIFY SCHEDULER: weekly check ran, result=%s", result)
        except Exception:
            logger.exception("WEEKLY NOTIFY SCHEDULER: weekly check failed, will retry next week")
            await asyncio.sleep(3600)


def _snapshot_db(dest_path):
    """Writes a consistent copy of the live database to dest_path using
    SQLite's own backup API. A plain file copy isn't safe here: the
    database runs in WAL mode, so recent changes can still be sitting in
    kaching.db-wal rather than the main file, and a file copy silently
    leaves them out."""
    src = db.get_db()
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def _daily_backup_loop():
    """Runs forever in the background: once a day, if auto-backup is turned
    on in Settings, copies the database into a timestamped file under
    /data/backups/, keeping only the most recent 7 so this doesn't grow
    unbounded. Silently does nothing on days it's turned off."""
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = max(1.0, (target - now).total_seconds())
            await asyncio.sleep(wait_seconds)

            conn = db.get_db()
            cur = conn.cursor()
            enabled = notifications.get_setting(cur, "auto_backup", "no") == "yes"
            conn.close()
            if not enabled:
                continue

            backup_dir = os.path.join(os.path.dirname(db.DB_PATH), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = os.path.join(backup_dir, f"kaching-auto-{stamp}.db")
            _snapshot_db(dest)
            logger.info("AUTO BACKUP: saved %s", dest)

            existing = sorted(
                (f for f in os.listdir(backup_dir) if f.startswith("kaching-auto-")),
            )
            for old in existing[:-7]:
                os.remove(os.path.join(backup_dir, old))
        except Exception:
            logger.exception("AUTO BACKUP: failed, will retry tomorrow")
            await asyncio.sleep(3600)


async def start_notification_scheduler():
    asyncio.create_task(_daily_notification_loop())
    asyncio.create_task(_weekly_notification_loop())
    asyncio.create_task(_daily_backup_loop())


def source_color(name: str) -> str:
    if name == DEFAULT_SOURCE:
        return "#2fd8ff"
    if name == "Whatnot" or name.startswith("Whatnot -"):
        return "#00e0b8"
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16)
    return SOURCE_PALETTE[h % len(SOURCE_PALETTE)]


def _per_request(fn):
    """Work a value out once per page load. Several parts of a page (the
    bell, the Alerts card, the side panel, the sidebar budget box) need the
    same figures; this makes them share one calculation - and so always
    agree - instead of each working it out again."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args):
        cache = db.request_cache()
        if cache is None:
            return fn(*args)
        key = ("memo", fn.__name__) + tuple(a for a in args if isinstance(a, (str, int, float, date)))
        if key not in cache:
            cache[key] = fn(*args)
        value = cache[key]
        return list(value) if isinstance(value, list) else value
    return wrapper


@_per_request
def _postage_rows(cur):
    cur.execute("SELECT order_number, SUM(amount) AS amount FROM shipment_postage GROUP BY order_number")
    return [(r["order_number"], r["amount"]) for r in cur.fetchall()]


def _postage_by_order(cur):
    """Exact postage per order number, summed across split deliveries.
    Read once per page load (it's needed for every parcel on the page)."""
    return dict(_postage_rows(cur))


@_per_request
def get_shipping_estimate(cur, source=None):
    """Work out shipping per parcel from the person's own order history where
    possible, rather than relying on a guessed constant. Scoped per-shop
    when a source is given, since different shops charge different amounts.

    Preference order:
    1. Exact per-shipment postage figures, captured directly from pasted
       order-detail pages or confirmed on the import review screen - real
       data, no approximation involved.
    2. An approximation from Forbidden Planet's order-history pages: the
       declared order total minus the items' cost, split evenly across
       however many distinct release dates the order covers. This tier only
       applies to Forbidden Planet, since it's the only shop whose declared
       order totals get captured today.
    3. DEFAULT_SHIPPING_ESTIMATE, until there's enough real data for a shop
       to trust either of the above.
    """
    query = "SELECT order_number, amount FROM shipment_postage"
    params = []
    if source:
        query += " WHERE source = ?"
        params.append(source)
    cur.execute(query, params)
    postage_rows = cur.fetchall()
    exact_samples = [r["amount"] for r in postage_rows if r["amount"] and r["amount"] > 0]
    if len(exact_samples) >= MIN_SHIPPING_SAMPLES:
        return round(sum(exact_samples) / len(exact_samples), 2), "exact", len(exact_samples), len(exact_samples)

    if source is None or source == DEFAULT_SOURCE:
        cur.execute(
            """
            SELECT i.order_number,
                   COUNT(DISTINCT i.release_date) AS distinct_dates,
                   SUM(i.price) AS items_sum,
                   o.declared_total AS declared_total
            FROM items i
            JOIN orders o ON o.order_number = i.order_number
            WHERE i.order_number IS NOT NULL AND o.declared_total IS NOT NULL
            GROUP BY i.order_number
            """
        )
        rows = cur.fetchall()
        samples = []
        for row in rows:
            distinct_dates = row["distinct_dates"] or 1
            implied_total = round(row["declared_total"] - row["items_sum"], 2)
            if implied_total <= 0:
                continue
            per_parcel = round(implied_total / distinct_dates, 2)
            if 0 < per_parcel <= MAX_PLAUSIBLE_SHIPPING:
                samples.append(per_parcel)

        if len(samples) >= MIN_SHIPPING_SAMPLES:
            return round(sum(samples) / len(samples), 2), "calibrated", len(samples), len(rows)
        return DEFAULT_SHIPPING_ESTIMATE, "default", len(samples), len(rows)

    return DEFAULT_SHIPPING_ESTIMATE, "default", len(exact_samples), len(exact_samples)


def month_bounds(d: date):
    start = d.replace(day=1)
    last_day = calendar.monthrange(d.year, d.month)[1]
    end = d.replace(day=last_day)
    return start, end


def shift_month(d: date, delta: int) -> date:
    month_index = d.month - 1 + delta
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def fetch_items_between(cur, start: date, end: date, source: str | None = None):
    # Falls back to placed_date when release_date isn't set yet - matches
    # the app's own effectiveDate() behaviour, so an item without a
    # confirmed release date still shows up somewhere sensible on both
    # sides instead of only ever appearing in Search here.
    query = """
        SELECT * FROM items
        WHERE status != 'cancelled'
          AND COALESCE(release_date, placed_date) IS NOT NULL
          AND date(COALESCE(release_date, placed_date)) BETWEEN date(?) AND date(?)
    """
    params = [start.isoformat(), end.isoformat()]
    if source:
        clause, extra_params = source_filter_sql(source)
        query += f" AND {clause}"
        params.extend(extra_params)
    query += " ORDER BY COALESCE(release_date, placed_date), name"
    cur.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def group_by_date(items):
    """Groups items by release date (falling back to placed_date if no
    release date is set yet, same as fetch_items_between above), then
    sub-groups each date by source/retailer (Forbidden Planet, or
    wherever else), so a day with releases from more than one shop shows
    each shop's items separately rather than one undifferentiated pile."""
    groups = {}
    for it in items:
        key = it["release_date"] or it["placed_date"]
        groups.setdefault(key, []).append(it)
    result = []
    # Items with no release date AND no placed date (some Forbidden Planet
    # imports arrive like this) group together under "No date yet", last.
    for key in sorted(groups.keys(), key=lambda k: (k is None, k or "")):
        group_items = groups[key]

        by_source = {}
        for it in group_items:
            by_source.setdefault(it["source"], []).append(it)
        source_groups = [
            {
                "source": src,
                "color": source_color(src),
                "entries": entries,
                "subtotal": round(sum(i["price"] for i in entries), 2),
            }
            for src, entries in by_source.items()
        ]

        result.append({
            "date": key,
            "date_label": date.fromisoformat(key).strftime("%a %d %b") if key else "No date yet",
            "source_groups": source_groups,
            "subtotal": round(sum(i["price"] for i in group_items), 2),
            "all_paid": all(i["charge_status"] == "charged" for i in group_items),
        })
    return result


def split_spent_remaining(items):
    spent = round(sum(i["price"] for i in items if i["charge_status"] == "charged"), 2)
    remaining = round(sum(i["price"] for i in items if i["charge_status"] != "charged"), 2)
    spent_count = sum(1 for i in items if i["charge_status"] == "charged")
    return spent, remaining, spent_count, len(items) - spent_count


def compute_shipping_for_groups(cur, groups):
    """Each (release date, shop) pair is a separate physical parcel with its
    own shipping cost. Uses the EXACT postage for that specific order when
    it's known (real data captured from the order itself), rather than a
    shop-average estimate - shipping genuinely varies order to order
    (item count, weight, free-shipping thresholds), especially on eBay, so
    an average across a seller's past orders isn't a reliable stand-in for
    what a given order actually cost. Estimates only apply when the exact
    figure for that particular order isn't available at all.

    Returns (total, spent, remaining, shipment_count, primary_rate,
    primary_source, primary_tier, primary_samples, primary_checked)."""
    # SUMs every shipment_index for a given order - a split delivery (the
    # same order captured across more than one real postage figure) must
    # count all of them, not just whichever row a plain dict comprehension
    # happened to keep last. This was a real, confirmed bug: an order with
    # two real shipments was silently having one of them dropped here.
    exact_by_order = _postage_by_order(cur)

    rate_cache = {}

    def estimate_for(src):
        if src not in rate_cache:
            rate_cache[src] = get_shipping_estimate(cur, src)
        return rate_cache[src]

    total = spent = remaining = 0.0
    shipment_count = 0
    for group in groups:
        for sg in group["source_groups"]:
            order_numbers = {e["order_number"] for e in sg["entries"] if e["order_number"]}
            known = [exact_by_order[o] for o in order_numbers if o in exact_by_order]

            if order_numbers and len(known) == len(order_numbers):
                # Every order behind this shipment has its real postage on record
                rate = round(sum(known), 2)
            else:
                rate, _, _, _ = estimate_for(sg["source"])

            total += rate
            shipment_count += 1
            all_paid = all(i["charge_status"] == "charged" for i in sg["entries"])
            if all_paid:
                spent += rate
            else:
                remaining += rate

    primary_rate, primary_tier, primary_samples, primary_checked = estimate_for(DEFAULT_SOURCE)
    return (
        round(total, 2), round(spent, 2), round(remaining, 2), shipment_count,
        primary_rate, DEFAULT_SOURCE, primary_tier, primary_samples, primary_checked,
    )


BUDGET_ALERT_THRESHOLD_PCT = 80


def _category_limits(cur):
    """{category_id: limit} for categories that have a spending limit."""
    try:
        raw = json.loads(notifications.get_setting(cur, "category_limits", "{}") or "{}")
    except (ValueError, TypeError):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            amount = float(v)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            out[int(k)] = round(amount, 2)
    return out


@_per_request
def _category_cycle_spend(cur, today: date):
    """This budget cycle's spend per category, counted exactly the way the
    overall budget counts it, so the categories always add up to the
    budget figure. Shipping belongs to a parcel, not a category, so each
    parcel's shipping is shared across its items' categories by price.
    Returns (cycle_key, {category_id: {"items", "ship", "total", "due"}})."""
    cycle = notifications.get_setting(cur, "budget_cycle", "monthly")
    default_id = db.default_category_id(cur.connection)
    known = {r[0] for r in cur.execute("SELECT id FROM categories").fetchall()}

    def cat_of(it):
        cid = it.get("category_id")
        return cid if cid in known else default_id

    spend = {}

    def add(cid, items=0.0, ship=0.0, due=0.0):
        row = spend.setdefault(cid, {"items": 0.0, "ship": 0.0, "total": 0.0, "due": 0.0})
        row["items"] += items; row["ship"] += ship; row["total"] += items + ship; row["due"] += due

    if cycle == "weekly":
        cycle_key = today.strftime("%G-W%V")
        for it in fetch_items_between(cur, today, today + timedelta(days=6)):
            future = (it["release_date"] or it["placed_date"]) > today.isoformat()
            add(cat_of(it), items=it["price"], due=it["price"] if future else 0.0)
    elif cycle == "28day":
        cycle_key = "28day"
        start = today - timedelta(days=27)
        cur.execute(
            "SELECT * FROM items WHERE status != 'cancelled' AND date(release_date) BETWEEN date(?) AND date(?)",
            (start.isoformat(), today.isoformat()),
        )
        for it in [dict(r) for r in cur.fetchall()]:
            add(cat_of(it), items=it["price"])
    else:
        cycle_key = today.strftime("%Y-%m")
        month_start, month_end = month_bounds(today)
        for group in group_by_date(fetch_items_between(cur, month_start, month_end)):
            group_ship, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, [group])
            entries = [e for sg in group["source_groups"] for e in sg["entries"]]
            subtotal = sum(e["price"] for e in entries) or 0
            future = group["date"] > today.isoformat()
            for e in entries:
                share = (e["price"] / subtotal) * group_ship if subtotal else group_ship / max(1, len(entries))
                add(cat_of(e), items=e["price"], ship=share, due=(e["price"] + share) if future else 0.0)
    for row in spend.values():
        for k in row:
            row[k] = round(row[k], 2)
    return cycle_key, spend


def _category_budget_rows(cur, today: date):
    """Rows for the 'Budget by category' card and the over-limit alerts:
    every category with spend this cycle or a limit, biggest first."""
    _, spend = _category_cycle_spend(cur, today)
    limits = _category_limits(cur)
    cats = {r["id"]: dict(r) for r in cur.execute("SELECT id, name, color FROM categories ORDER BY sort_order, id").fetchall()}
    rows = []
    for cid in set(spend) | set(limits):
        if cid not in cats:
            continue
        sp = spend.get(cid, {"total": 0.0, "due": 0.0})
        lim = limits.get(cid)
        rows.append({
            "id": cid, "name": cats[cid]["name"], "color": cats[cid]["color"],
            "total": sp["total"], "due": sp.get("due", 0.0), "limit": lim,
            "over": round(sp["total"] - lim, 2) if lim and sp["total"] > lim else 0,
        })
    rows.sort(key=lambda r: (-r["total"], r["name"]))
    return rows


@_per_request
def find_category_overages(cur, today: date):
    return [r for r in _category_budget_rows(cur, today) if r["over"] > 0]


def check_category_limits():
    """Daily: one push per category per budget cycle when it goes over its
    limit, if budget alerts are switched on."""
    conn = db.get_db()
    cur = conn.cursor()
    if notifications.get_setting(cur, "budget_alert_enabled", "no") != "yes":
        conn.close()
        return None
    today = date.today()
    cycle_key, _ = _category_cycle_spend(cur, today)
    try:
        sent = json.loads(notifications.get_setting(cur, "_catlimit_sent", "{}") or "{}")
    except (ValueError, TypeError):
        sent = {}
    currency_symbol = {"gbp": "\u00a3", "usd": "$", "eur": "\u20ac"}.get(notifications.get_setting(cur, "currency_symbol", "gbp"), "\u00a3")
    results = []
    for r in find_category_overages(cur, today):
        key = str(r["id"])
        last = sent.get(key, "")
        if cycle_key == "28day":
            recent = last and (today - date.fromisoformat(last)).days < 28
        else:
            recent = last == cycle_key
        if recent:
            continue
        title = f"{r['name']} is over its limit"
        message = f"{currency_symbol}{r['total']:,.2f} of {currency_symbol}{r['limit']:,.2f} this cycle ({currency_symbol}{r['over']:,.2f} over)."
        result = notifications.send_via_configured_provider(cur, title, message)
        if result[0]:
            sent[key] = today.isoformat() if cycle_key == "28day" else cycle_key
        results.append((r["name"], result))
    notifications.set_setting(cur, "_catlimit_sent", json.dumps(sent))
    conn.commit()
    conn.close()
    return results or None


def check_budget_threshold(force: bool = False):
    """Fires once when this month's forecast total (comics + shipping)
    crosses BUDGET_ALERT_THRESHOLD_PCT of the monthly budget - deliberately
    a single fixed threshold rather than a configurable one, and reuses the
    existing notification provider/settings rather than adding a separate
    setup. Sends at most once per calendar month, tracked via a hidden
    settings key, so it doesn't repeat every day for the rest of the month
    once crossed. force=True (the manual test button) bypasses both the
    enabled check and the once-per-month gate, and always sends visible
    feedback either way - same convention as the other test buttons."""
    conn = db.get_db()
    cur = conn.cursor()

    enabled = notifications.get_setting(cur, "budget_alert_enabled", "no") == "yes"
    if not enabled and not force:
        conn.close()
        return None

    # The same budget figures the sidebar and Dashboard show - including the
    # budget cycle (monthly / weekly / 28-day) and any rolled-over budget.
    # (This used to check this calendar month against the base budget
    # regardless of the cycle setting.)
    status = _topbar_budget_status()
    if not status:
        conn.close()
        if force:
            return (False, "No monthly budget is set in Settings.")
        return None

    today = date.today()
    monthly_budget = status["monthly_budget"]
    spend = status["cycle_spend"]
    pct = status["budget_pct"]
    cycle = notifications.get_setting(cur, "budget_cycle", "monthly")
    period_label = status["budget_cycle_label"]

    if cycle == "weekly":
        period_key = today.strftime("%G-W%V")
    elif cycle == "28day":
        period_key = "28day"
    else:
        period_key = today.strftime("%Y-%m")
    last_sent = notifications.get_setting(cur, "_budget_alert_last_period", "")
    if cycle == "28day":
        # a rolling window: at most once every 28 days
        try:
            already_sent_this_period = bool(last_sent) and (today - date.fromisoformat(last_sent)).days < 28
        except ValueError:
            already_sent_this_period = False
    else:
        already_sent_this_period = last_sent == period_key

    currency_symbol = {"gbp": "\u00a3", "usd": "$", "eur": "\u20ac"}.get(
        notifications.get_setting(cur, "currency_symbol", "gbp"), "\u00a3"
    )

    if pct < BUDGET_ALERT_THRESHOLD_PCT and not force:
        conn.close()
        return None

    if already_sent_this_period and not force:
        conn.close()
        return None

    title = f"Budget alert: {pct:.0f}% of {period_label}'s budget"
    message = f"{currency_symbol}{spend:,.2f} of {currency_symbol}{monthly_budget:,.2f} spent so far {period_label}."
    if force and pct < BUDGET_ALERT_THRESHOLD_PCT:
        message += f" (Not yet at the {BUDGET_ALERT_THRESHOLD_PCT}% threshold - this is a test send.)"

    result = notifications.send_via_configured_provider(cur, title, message)
    ok, _ = result
    if ok and pct >= BUDGET_ALERT_THRESHOLD_PCT:
        notifications.set_setting(cur, "_budget_alert_last_period", today.isoformat() if cycle == "28day" else period_key)
        conn.commit()
    conn.close()
    logger.info("BUDGET ALERT: period=%s pct=%.1f spend=%.2f budget=%.2f result=%s",
                period_key, pct, spend, monthly_budget, result)
    return result


@_per_request
def find_duplicate_groups(cur):
    """Items with the same name and release date but different order numbers
    - almost always an accidental double-order rather than two genuinely
    different things releasing the same day."""
    cur.execute(
        """
        SELECT name, release_date, COUNT(DISTINCT order_number) AS n_orders
        FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL
        GROUP BY name, release_date
        HAVING n_orders > 1
        """
    )
    candidates = cur.fetchall()
    if not candidates:
        logger.info("DUPLICATE CHECK: no candidate groups this load")
        return []

    cur.execute("SELECT name, release_date FROM dismissed_duplicates")
    dismissed = {(r["name"], r["release_date"]) for r in cur.fetchall()}

    groups = []
    for row in candidates:
        key = (row["name"], row["release_date"])
        if key in dismissed:
            logger.info("DUPLICATE CHECK: skipping dismissed pair name=%r release_date=%s", row["name"], row["release_date"])
            continue
        cur.execute(
            """
            SELECT * FROM items
            WHERE name = ? AND release_date = ? AND status != 'cancelled'
            ORDER BY order_number
            """,
            key,
        )
        entries = [dict(r) for r in cur.fetchall()]
        logger.info(
            "DUPLICATE CHECK: flagged name=%r release_date=%s orders=%s ids=%s",
            row["name"], row["release_date"],
            [e["order_number"] for e in entries], [e["id"] for e in entries],
        )
        groups.append({
            "name": row["name"],
            "release_date": row["release_date"],
            "release_date_label": date.fromisoformat(row["release_date"]).strftime("%d %b %Y"),
            "entries": entries,
        })
    return groups


@_per_request
def find_ghost_items(cur):
    """Items tagged as Forbidden Planet with no order number at all - always
    a parser artifact (see README), never legitimate, since every real FP
    item comes with an order number attached. Manually-added items from
    other sources are untouched by this check, since having no order number
    is normal for those."""
    cur.execute(
        """
        SELECT * FROM items
        WHERE source = ? AND order_number IS NULL AND status != 'cancelled'
        ORDER BY release_date DESC
        """,
        (DEFAULT_SOURCE,),
    )
    return [dict(r) for r in cur.fetchall()]


@_per_request
def find_undated_items(cur):
    """Items with neither a release date nor a placed date. They can't be
    placed on the calendar or in any month, so they'd otherwise sit
    unnoticed (some Forbidden Planet imports arrive like this)."""
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NULL AND placed_date IS NULL
        ORDER BY name
        """
    )
    return [dict(r) for r in cur.fetchall()]


RESTORE_COPIES_KEPT = 3


@_per_request
def find_awaiting_charge(cur, today: date):
    """Items whose release date has already passed but are still sitting
    unpaid and unmarked - worth a look, since the retailer usually charges
    right around release day. Could just be a normal short delay, but
    surfacing it beats only noticing by chance."""
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND charge_status != 'charged'
          AND release_date IS NOT NULL AND date(release_date) < date(?)
        ORDER BY release_date ASC
        """,
        (today.isoformat(),),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        days_late = (today - date.fromisoformat(r["release_date"])).days
        r["days_late"] = days_late
    return rows


def alert_counts(cur, today: date):
    """Every open alert, counted by kind - the one place the bell, the
    Alerts page, the Dashboard's Alerts card and the side panel get their
    numbers from."""
    counts = {
        "awaiting": len(find_awaiting_charge(cur, today)),
        "duplicates": len(find_duplicate_groups(cur)),
        "ghost": len(find_ghost_items(cur)),
        "undated": len(find_undated_items(cur)),
        "over_limit": len(find_category_overages(cur, today)),
    }
    counts["total"] = sum(counts.values())
    return counts


def _topbar_alert_count():
    """The bell badge on every page."""
    conn = db.get_db()
    count = alert_counts(conn.cursor(), date.today())["total"]
    conn.close()
    return count


def _sync_feature_available():
    """Whether phone-app sync exists on this install at all. Hidden
    completely (no Sync settings tab, /api/sync refused) unless the
    container is started with KACHING_PHONE_SYNC=1 - parked until the
    Android app is ready again."""
    return os.environ.get("KACHING_PHONE_SYNC", "").strip().lower() in ("1", "true", "yes", "on")


def _sync_enabled():
    """Phone-app sync on/off (Settings -> Sync). Off by default,
    so sync stays hidden and /api/sync refuses until it's switched on. The
    sync key is kept either way, so switching back on needs no re-pairing.
    Always off while the feature itself is hidden."""
    if not _sync_feature_available():
        return False
    conn = db.get_db()
    value = notifications.get_setting(conn.cursor(), "sync_enabled", "0")
    conn.close()
    return value == "1"


def _topbar_most_recent_sync():
    """Real most-recent-sync info for the topbar, queried fresh on every
    template render - same reasoning as _topbar_alert_count above."""
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT client_label, last_synced_at FROM sync_state ORDER BY last_synced_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    synced_at = datetime.fromisoformat(row["last_synced_at"])
    return {
        "client_label": row["client_label"] or "A device",
        "label": synced_at.strftime("%d %b, %H:%M"),
    }


@_per_request
def _topbar_budget_status():
    """Real sidebar budget-status, queried fresh on every template render
    - same reasoning as the other topbar globals above. Faithfully
    replicates Dashboard's real cycle/rollover logic (weekly/monthly/
    28-day, with monthly rollover of unused budget) rather than a
    simplified approximation, but as its own self-contained query since
    Dashboard's version is tangled into that route's own in-flight
    variables (hero_items, week_total, etc)."""
    conn = db.get_db()
    cur = conn.cursor()
    today = date.today()

    monthly_budget_raw = notifications.get_setting(cur, "monthly_budget", "")
    budget_cycle = notifications.get_setting(cur, "budget_cycle", "monthly")
    budget_rollover = notifications.get_setting(cur, "budget_rollover", "no") == "yes"
    budget_cycle_label = {"monthly": "this month", "weekly": "this week", "28day": "this 28-day period"}.get(budget_cycle, "this month")

    if not monthly_budget_raw:
        conn.close()
        return None

    try:
        base_budget = float(monthly_budget_raw)
    except (ValueError, TypeError):
        conn.close()
        return None
    if base_budget <= 0:
        conn.close()
        return None

    # Split of the cycle into "spent so far" (released up to today) and
    # "still due" (scheduled later in the cycle), plus days left - used by
    # the sidebar box's display options. cycle_spend itself is unchanged.
    spent_to_date = None
    days_left = None
    if budget_cycle == "weekly":
        week_end = today + timedelta(days=6)
        week_items = fetch_items_between(cur, today, week_end)
        cycle_spend = round(sum(i["price"] for i in week_items), 2)
        spent_to_date = round(sum(i["price"] for i in week_items if (i["release_date"] or i["placed_date"]) <= today.isoformat()), 2)
        days_left = 7
    elif budget_cycle == "28day":
        twenty_eight_start = today - timedelta(days=27)
        cur.execute(
            "SELECT COALESCE(SUM(price), 0) AS s FROM items WHERE status != 'cancelled' AND date(release_date) BETWEEN date(?) AND date(?)",
            (twenty_eight_start.isoformat(), today.isoformat()),
        )
        cycle_spend = cur.fetchone()["s"]
        spent_to_date = cycle_spend
    else:
        month_start, month_end = month_bounds(today)
        month_items = fetch_items_between(cur, month_start, month_end)
        month_comics = round(sum(i["price"] for i in month_items), 2)
        month_groups = group_by_date(month_items)
        month_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, month_groups)
        cycle_spend = round(month_comics + month_shipping, 2)
        past_groups = [g for g in month_groups if g["date"] <= today.isoformat()]
        past_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, past_groups) if past_groups else (0,) * 9
        spent_to_date = round(sum(g["subtotal"] for g in past_groups) + past_shipping, 2)
        days_left = (month_end - today).days + 1

    effective_budget = base_budget
    if budget_rollover and budget_cycle == "monthly":
        prev_start, prev_end = month_bounds(shift_month(today, -1))
        prev_items = fetch_items_between(cur, prev_start, prev_end)
        prev_comics = round(sum(i["price"] for i in prev_items), 2)
        prev_groups = group_by_date(prev_items)
        prev_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, prev_groups)
        prev_total = round(prev_comics + prev_shipping, 2)
        if prev_total < base_budget:
            effective_budget = round(base_budget + (base_budget - prev_total), 2)

    display = notifications.get_setting(cur, "sidebar_budget_display", "spent_of")
    conn.close()
    budget_pct = round((cycle_spend / effective_budget) * 100, 1) if effective_budget else 0
    left = round(effective_budget - cycle_spend, 2)
    still_due = round(max(cycle_spend - (spent_to_date or 0), 0), 2)
    return {
        "monthly_budget": effective_budget,
        "cycle_spend": cycle_spend,
        "budget_bar_pct": min(100, budget_pct),
        "budget_cycle_label": budget_cycle_label,
        "budget_pct": budget_pct,
        "over": left < 0,
        "left": left,
        "spent_to_date": spent_to_date,
        "still_due": still_due,
        "spent_bar_pct": min(100, round((spent_to_date or 0) / effective_budget * 100, 1)) if effective_budget else 0,
        "days_left": days_left,
        "per_day": round(left / days_left, 2) if (days_left and left > 0) else None,
        "display": display,
    }


def get_categories(cur, with_counts=False):
    """All categories in display order. with_counts adds how many live
    (not deleted) items each one has, for the Settings list and to block
    removing a category that's still in use."""
    if with_counts:
        cur.execute(
            """
            SELECT c.id, c.name, c.color, c.has_series, c.sort_order,
                   (SELECT COUNT(*) FROM items i WHERE i.category_id = c.id AND i.deleted_at IS NULL) AS item_count
            FROM categories c ORDER BY c.sort_order, c.id
            """
        )
    else:
        cur.execute("SELECT id, name, color, has_series, sort_order FROM categories ORDER BY sort_order, id")
    return [dict(r) for r in cur.fetchall()]


def _create_category(cur, name):
    """Creates a category (no series by default) and returns its id. New
    ones slot in just above "Other" if it exists, otherwise at the end."""
    clean = " ".join((name or "").split())[:60]
    if not clean:
        return None
    cur.execute("SELECT id FROM categories WHERE name = ? COLLATE NOCASE", (clean,))
    existing = cur.fetchone()
    if existing:
        return existing[0]
    cur.execute("SELECT COUNT(*) FROM categories")
    used = cur.fetchone()[0]
    color = db.EXTRA_CATEGORY_COLORS[max(0, used - len(db.STARTER_CATEGORIES)) % len(db.EXTRA_CATEGORY_COLORS)]
    cur.execute("SELECT sort_order FROM categories WHERE name = 'Other' COLLATE NOCASE")
    other = cur.fetchone()
    if other:
        cur.execute("UPDATE categories SET sort_order = sort_order + 1 WHERE sort_order >= ?", (other[0],))
        sort_order = other[0]
    else:
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM categories")
        sort_order = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO categories (name, color, has_series, sort_order, created_at) VALUES (?, ?, 0, ?, ?)",
        (clean, color, sort_order, db.utc_now()),
    )
    logger.info("CATEGORY ADDED: %r", clean)
    return cur.lastrowid


def _categories_global():
    conn = db.get_db()
    try:
        return get_categories(conn.cursor())
    finally:
        conn.close()


def _default_category_global():
    conn = db.get_db()
    try:
        return db.default_category_id(conn)
    finally:
        conn.close()


templates.env.globals["all_categories"] = _categories_global


templates.env.globals["default_category_id"] = _default_category_global


templates.env.globals["topbar_alert_count"] = _topbar_alert_count


templates.env.globals["topbar_most_recent_sync"] = _topbar_most_recent_sync


templates.env.globals["sync_enabled"] = _sync_enabled


# Cards that can be hidden (Customise on each page, Settings -> Layout).
# "preorder" marks the cards the "I don't pre-order" preset switches off.
CARD_LAYOUT = [
    {"page": "Dashboard", "path": "/", "cards": [
        {"id": "dash.rings", "name": "Still due & budget", "desc": "This month's still-due and budget rings", "preorder": True},
        {"id": "dash.year", "name": "Your spending", "desc": "This year and all time, and this year's share", "essential": True},
        {"id": "dash.catbudget", "name": "Budget by category", "desc": "This cycle's spend split by category, with any limits"},
        {"id": "dash.week", "name": "This week", "desc": "What's due in the next 7 days, by shipment", "preorder": True},
        {"id": "dash.alerts", "name": "Alerts", "desc": "Awaiting charge, duplicates, ghost items", "essential": True},
        {"id": "dash.backup", "name": "Backup", "desc": "When the last backup was taken"},
        {"id": "dash.stillorder", "name": "Biggest still to come", "desc": "Priciest item not yet released, and your total still on order", "preorder": True},
        {"id": "dash.trend", "name": "Spend trend", "desc": "Week / Month / 6M chart", "essential": True},
    ]},
    {"page": "Calendar", "path": "/calendar", "cards": [
        {"id": "cal.mini", "name": "Mini calendar & spend by shop", "desc": "Small month view and this month's shop split"},
        {"id": "cal.big", "name": "Month calendar", "desc": "The big calendar grid", "essential": True},
        {"id": "cal.heatmap", "name": "Activity", "desc": "Year-long spend heatmap"},
        {"id": "cal.list", "name": "Releases list", "desc": "Everything releasing this month", "essential": True},
    ]},
    {"page": "Insights \u00b7 Overview", "path": "/insights", "cards": [
        {"id": "ov.ratio", "name": "Pre-order vs released", "desc": "How much is still on order", "preorder": True},
        {"id": "ov.releases", "name": "10 releases", "desc": "Recent, priciest and cheapest"},
        {"id": "ov.spend", "name": "Spend overview", "desc": "Monthly spend at a glance", "essential": True},
        {"id": "ov.issue", "name": "Per issue", "desc": "Priciest item and average issue"},
        {"id": "ov.dist", "name": "Price distribution", "desc": "Items by price bracket"},
        {"id": "ov.ship", "name": "Shipping and patterns", "desc": "Busiest day, savings, shipping vs cover"},
        {"id": "ov.cum", "name": "Cumulative spend", "desc": "This month against your budget"},
        {"id": "ov.cat", "name": "Spend by category", "desc": "All time / this year"},
        {"id": "ov.trend", "name": "12-month spend trend", "desc": "Items and shipping over the last year", "essential": True},
    ]},
    {"page": "Insights \u00b7 Spend by shop", "path": "/insights/spend-by-shop", "cards": [
        {"id": "shop.stats", "name": "Stat tiles", "desc": "The four figures along the top", "essential": True},
        {"id": "shop.donut", "name": "Share of spend", "desc": "Donut, all time / last 90 days", "essential": True},
        {"id": "shop.breakdown", "name": "By shop, all time", "desc": "Every shop's total, sellers expandable"},
        {"id": "shop.compare", "name": "Shop comparison", "desc": "Side-by-side table"},
        {"id": "shop.overtime", "name": "Spend by shop over time", "desc": "6 / 12 month chart"},
    ]},
    {"page": "Insights \u00b7 Price creep", "path": "/insights/price-creep", "cards": [
        {"id": "creep.stats", "name": "Stat tiles", "desc": "The four figures along the top", "essential": True},
        {"id": "creep.hero", "name": "Biggest jumper", "desc": "The series with the steepest rise"},
        {"id": "creep.dist", "name": "Increase distribution", "desc": "Series by size of increase"},
        {"id": "creep.table", "name": "All tracked series", "desc": "Ranked table", "essential": True},
        {"id": "creep.chart", "name": "Extra spend from creep", "desc": "Running total chart"},
    ]},
    {"page": "Insights \u00b7 Top titles", "path": "/insights/top-titles", "cards": [
        {"id": "titles.stats", "name": "Stat tiles", "desc": "The four figures along the top", "essential": True},
        {"id": "titles.hero", "name": "Priciest issue tracked", "desc": "Your single priciest item"},
        {"id": "titles.priciest", "name": "Priciest issues, all time", "desc": "Top items by price", "essential": True},
        {"id": "titles.series", "name": "Top series by total spend", "desc": "Series ranked by spend"},
        {"id": "titles.compare", "name": "All series, compared", "desc": "Full comparison table"},
    ]},
]


CARD_IDS = {c["id"] for pg in CARD_LAYOUT for c in pg["cards"]}


# Wide landscape screens (1900px+) only. "columns" lays cards out three to
# a row using these widths (out of 12); "panel" adds the At a glance column.
WIDE_LAYOUTS = ("standard", "columns", "panel")


WIDE_SPANS = {
    "dash.rings": 4, "dash.year": 4, "dash.alerts": 4, "dash.week": 8, "dash.backup": 4,
    "dash.catbudget": 12, "dash.stillorder": 12, "dash.trend": 12,
    "cal.mini": 3, "cal.big": 9, "cal.heatmap": 12, "cal.list": 12,
    "ov.ratio": 4, "ov.releases": 8, "ov.spend": 8, "ov.issue": 4, "ov.dist": 4, "ov.ship": 4,
    "ov.cum": 4, "ov.cat": 12, "ov.trend": 12,
    "shop.stats": 12, "shop.donut": 4, "shop.breakdown": 8, "shop.compare": 12, "shop.overtime": 12,
    "creep.stats": 12, "creep.hero": 4, "creep.dist": 8, "creep.table": 12, "creep.chart": 12,
    "titles.stats": 12, "titles.hero": 4, "titles.priciest": 8, "titles.series": 12, "titles.compare": 12,
}


# Default order in three columns where it differs from the normal one
WIDE_ORDER = {
    "/": ["dash.rings", "dash.year", "dash.alerts", "dash.week", "dash.backup",
          "dash.catbudget", "dash.stillorder", "dash.trend"],
}


def _wide_layout():
    conn = db.get_db()
    v = notifications.get_setting(conn.cursor(), "wide_layout", "standard")
    conn.close()
    return v if v in WIDE_LAYOUTS else "standard"


def _glance():
    """Data for the wide-screen 'At a glance' panel: this week, alerts and
    the biggest thing still to come."""
    conn = db.get_db()
    cur = conn.cursor()
    today = date.today()
    week_items = fetch_items_between(cur, today, today + timedelta(days=6))
    week_items.sort(key=lambda i: (i["release_date"] or i["placed_date"] or "", i["name"]))
    ac = alert_counts(cur, today)
    alerts = [
        ("awaiting", ac["awaiting"], "unpaid, awaiting charge", "var(--neon-pink)"),
        ("dupes", ac["duplicates"], "possible duplicates", "var(--neon-orange, #ff9d5c)"),
        ("ghost", ac["ghost"], "with no order number", "var(--neon-violet)"),
        ("undated", ac["undated"], "with no release date", "var(--neon-blue)"),
        ("over", ac["over_limit"], "categor{} over a limit", "var(--neon-pink)"),
    ]
    cur.execute(
        "SELECT * FROM items WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) >= date(?)",
        (today.isoformat(),),
    )
    upcoming = [dict(r) for r in cur.fetchall()]
    conn.close()
    big = max(upcoming, key=lambda i: i["price"], default=None)
    if big:
        rd = date.fromisoformat(big["release_date"])
        big = {"name": big["name"], "price": big["price"], "source": big["source"],
               "due": rd.strftime("%-d %b"), "days": (rd - today).days}
    return {
        "week": [{"name": i["name"], "price": i["price"],
                  "day": date.fromisoformat((i["release_date"] or i["placed_date"])[:10]).strftime("%a %-d")} for i in week_items[:6]],
        "week_more": max(0, len(week_items) - 6),
        "week_total": round(sum(i["price"] for i in week_items), 2),
        "alerts": [{"key": k, "count": n, "label": (lbl.format("y" if n == 1 else "ies") if "{}" in lbl else lbl), "color": c}
                   for k, n, lbl, c in alerts if n],
        "big": big,
        "upcoming_count": len(upcoming),
        "upcoming_total": round(sum(i["price"] for i in upcoming), 2),
    }


templates.env.globals["wide_layout"] = _wide_layout


templates.env.globals["glance"] = _glance


templates.env.globals["wide_layout_json"] = lambda: json.dumps({"spans": WIDE_SPANS, "order": WIDE_ORDER})


def _hidden_cards():
    conn = db.get_db()
    raw = notifications.get_setting(conn.cursor(), "hidden_cards", "[]")
    conn.close()
    try:
        return [c for c in json.loads(raw) if c in CARD_IDS]
    except (ValueError, TypeError):
        return []


def _card_order():
    """Saved card order per page: {page path: [card ids]}."""
    conn = db.get_db()
    raw = notifications.get_setting(conn.cursor(), "card_order", "{}")
    conn.close()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    paths = {pg["path"] for pg in CARD_LAYOUT}
    return {k: [c for c in v if c in CARD_IDS] for k, v in data.items() if k in paths and isinstance(v, list)}


templates.env.globals["hidden_cards"] = _hidden_cards


templates.env.globals["card_order"] = _card_order


templates.env.globals["card_layout"] = lambda: CARD_LAYOUT


def _sidebar_budget_display():
    conn = db.get_db()
    value = notifications.get_setting(conn.cursor(), "sidebar_budget_display", "spent_of")
    conn.close()
    return value


templates.env.globals["sidebar_budget_display"] = _sidebar_budget_display


templates.env.globals["sync_feature_available"] = _sync_feature_available


templates.env.globals["topbar_budget_status"] = _topbar_budget_status


def get_all_sources(cur):
    cur.execute("SELECT DISTINCT source FROM items WHERE status != 'cancelled' ORDER BY source")
    return [r["source"] for r in cur.fetchall()]


def is_ebay_group(source: str | None) -> bool:
    return source == "eBay"


def source_filter_sql(source: str):
    """Returns (sql_fragment, extra_params) for filtering items by source.
    'eBay' is treated as a group covering every per-seller eBay source."""
    if is_ebay_group(source):
        return "(source = 'eBay' OR source LIKE 'eBay -%')", []
    return "source = ?", [source]


SEARCH_SORT_OPTIONS = {
    "date_desc": ("release_date DESC, name", "Release date (newest)"),
    "date_asc": ("release_date ASC, name", "Release date (oldest)"),
    "price_desc": ("price DESC, name", "Price (highest)"),
    "price_asc": ("price ASC, name", "Price (lowest)"),
    "name_asc": ("name ASC", "Name (A-Z)"),
    "added_desc": ("imported_at DESC", "Recently added"),
}


SEARCH_PER_PAGE = 50


def _csv_safe(value):
    """Guards against CSV/formula injection: if a value starts with a
    character a spreadsheet would interpret as the start of a formula
    (=, +, -, @), prefix it with a single quote so Excel/Sheets treats it
    as plain text instead of trying to evaluate it. Low realistic risk
    for a single-user app, but a cheap, standard precaution worth having
    on any export - item names ultimately come from pasted third-party
    text, not just what's typed in directly."""
    text = str(value) if value is not None else ""
    if text and text[0] in ("=", "+", "-", "@"):
        return "'" + text
    return text


SIDEBAR_BUDGET_DISPLAYS = ("spent_of", "left", "percent", "daily", "due", "hidden")


NOTIFICATION_SETTINGS_KEYS = [
    "notify_provider", "notify_hour", "ntfy_url", "ntfy_topic",
    "gotify_url", "gotify_token", "telegram_bot_token", "telegram_chat_id",
    "webhook_url", "webhook_json_template", "notify_on_quiet_days",
    "weekly_digest_enabled", "weekly_digest_day",
]


_AUTO_BACKUP_NAME_RE = re.compile(r"^kaching-auto-\d{8}-\d{6}\.db$")


SYNC_ITEM_FIELDS = [
    "uuid", "name", "order_number", "placed_date", "status", "release_date",
    "charge_status", "price", "note", "source", "tracking_number",
]


class SyncPushItem(BaseModel):
    uuid: str
    name: str
    order_number: str | None = None
    placed_date: str | None = None
    status: str = "preorder"
    release_date: str | None = None
    charge_status: str = "not_charged"
    price: float
    note: str | None = None
    source: str = "Unknown shop"
    tracking_number: str | None = None
    manual_override: bool = False
    updated_at: str
    deleted: bool = False
    # Category by NAME, not id - ids differ between the app and this
    # server. Missing or null (an app from before categories existed)
    # means "don't touch": an existing item keeps its category, a new one
    # gets the default. Never used to clear a category.
    category: str | None = None


class SyncPushShipping(BaseModel):
    order_number: str
    shipment_index: int = 0
    amount: float
    captured_at: str
    source: str | None = None


class SyncPushOrder(BaseModel):
    order_number: str
    declared_total: float
    last_seen_at: str


class SyncRequest(BaseModel):
    client_id: str
    client_label: str | None = None
    since: str | None = None
    push: list[SyncPushItem] = []
    push_shipping: list[SyncPushShipping] = []
    push_orders: list[SyncPushOrder] = []


def _sync_category_names(cur) -> dict:
    """category id -> name for the sync payload. The None key holds the
    default category's name, used for any item whose category is missing
    or points at one that no longer exists."""
    cur.execute("SELECT id, name FROM categories")
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    default_id = db.default_category_id(cur.connection)
    names[None] = names.get(default_id)
    return names


# Everything above is shared with the route modules (pages, items,
# settings_pages, auth, api), which import it with "from .core import *".
__all__ = [n for n in dir() if not n.startswith("__")]

