import asyncio
import calendar
import contextvars
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import db, notifications, parser

APP_DIR = os.path.dirname(__file__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kaching")

app = FastAPI(title="Ka-Ching!")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))

APP_VERSION = "3.0.0"
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


@app.on_event("startup")
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


@app.on_event("startup")
async def start_notification_scheduler():
    asyncio.create_task(_daily_notification_loop())
    asyncio.create_task(_weekly_notification_loop())
    asyncio.create_task(_daily_backup_loop())


# --- Shared helpers ----------------------------------------------------------

def source_color(name: str) -> str:
    if name == DEFAULT_SOURCE:
        return "#2fd8ff"
    if name == "Whatnot" or name.startswith("Whatnot -"):
        return "#00e0b8"
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16)
    return SOURCE_PALETTE[h % len(SOURCE_PALETTE)]


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
    cur.execute("SELECT order_number, SUM(amount) AS amount FROM shipment_postage GROUP BY order_number")
    # SUMs every shipment_index for a given order - a split delivery (the
    # same order captured across more than one real postage figure) must
    # count all of them, not just whichever row a plain dict comprehension
    # happened to keep last. This was a real, confirmed bug: an order with
    # two real shipments was silently having one of them dropped here.
    exact_by_order = {r["order_number"]: r["amount"] for r in cur.fetchall()}

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

    monthly_budget_raw = notifications.get_setting(cur, "monthly_budget", "")
    monthly_budget = None
    if monthly_budget_raw:
        try:
            monthly_budget = float(monthly_budget_raw)
        except (TypeError, ValueError):
            monthly_budget = None

    if not monthly_budget or monthly_budget <= 0:
        conn.close()
        if force:
            return (False, "No monthly budget is set in Settings.")
        return None

    today = date.today()
    start, end = month_bounds(today)
    items = fetch_items_between(cur, start, end)
    groups = group_by_date(items)
    comics_total = round(sum(i["price"] for i in items), 2)
    shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, groups)
    spend = round(comics_total + shipping_total, 2)
    pct = round((spend / monthly_budget) * 100, 1)

    period_key = today.strftime("%Y-%m")
    last_sent = notifications.get_setting(cur, "_budget_alert_last_period", "")
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

    title = f"Budget alert: {pct:.0f}% of this month's budget"
    message = f"{currency_symbol}{spend:.2f} of {currency_symbol}{monthly_budget:.2f} spent so far this month."
    if force and pct < BUDGET_ALERT_THRESHOLD_PCT:
        message += f" (Not yet at the {BUDGET_ALERT_THRESHOLD_PCT}% threshold - this is a test send.)"

    result = notifications.send_via_configured_provider(cur, title, message)
    ok, _ = result
    if ok and pct >= BUDGET_ALERT_THRESHOLD_PCT:
        notifications.set_setting(cur, "_budget_alert_last_period", period_key)
        conn.commit()
    conn.close()
    logger.info("BUDGET ALERT: period=%s pct=%.1f spend=%.2f budget=%.2f result=%s",
                period_key, pct, spend, monthly_budget, result)
    return result


def annotate_group_shipping(cur, groups):
    """Attaches the correct shipping amount to each source-group within
    date-groups, for direct display - exact per-order postage when known,
    an estimate only when it genuinely isn't. Mirrors the same logic
    compute_shipping_for_groups uses for the totals, so what's shown next
    to each shipment always matches what's counted in the numbers above it."""
    cur.execute("SELECT order_number, SUM(amount) AS amount FROM shipment_postage GROUP BY order_number")
    # SUMs every shipment_index for a given order - a split delivery (the
    # same order captured across more than one real postage figure) must
    # count all of them, not just whichever row a plain dict comprehension
    # happened to keep last. This was a real, confirmed bug: an order with
    # two real shipments was silently having one of them dropped here.
    exact_by_order = {r["order_number"]: r["amount"] for r in cur.fetchall()}
    rate_cache = {}

    def estimate_for(src):
        if src not in rate_cache:
            rate_cache[src] = get_shipping_estimate(cur, src)
        return rate_cache[src]

    for group in groups:
        for sg in group["source_groups"]:
            order_numbers = {e["order_number"] for e in sg["entries"] if e["order_number"]}
            known = [exact_by_order[o] for o in order_numbers if o in exact_by_order]
            if order_numbers and len(known) == len(order_numbers):
                sg["shipping_amount"] = round(sum(known), 2)
                sg["shipping_is_exact"] = True
            else:
                sg["shipping_amount"] = estimate_for(sg["source"])[0]
                sg["shipping_is_exact"] = False
    return groups


def _smooth_svg_path(points):
    """Cubic-bezier path through a series of points using Catmull-Rom-derived
    control points, for a smooth curve rather than sharp straight segments."""
    d = f"M{points[0][0]},{points[0][1]}"
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1x = round(p1[0] + (p2[0] - p0[0]) / 6, 1)
        c1y = round(p1[1] + (p2[1] - p0[1]) / 6, 1)
        c2x = round(p2[0] - (p3[0] - p1[0]) / 6, 1)
        c2y = round(p2[1] - (p3[1] - p1[1]) / 6, 1)
        d += f" C{c1x},{c1y} {c2x},{c2y} {p2[0]},{p2[1]}"
    return d


def render_progress_ring_svg(percent: float, is_over: bool = False, size: int = 96, stroke: int = 10, font_size: int = 20) -> str:
    """A single value against a fixed ceiling (e.g. budget) - same visual
    language as the app's own dashboard rings, generated server-side
    since this app has no client-side JS layer to do it in the browser."""
    r = (size - stroke) / 2
    circumference = 2 * math.pi * r
    clamped = min(percent, 100)
    offset = circumference * (1 - clamped / 100)
    color = "var(--neon-pink)" if is_over else "var(--neon-blue)"
    return f'''<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" class="progress-ring-svg">
  <circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="var(--border)" stroke-width="{stroke}"/>
  <circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{circumference}" stroke-dashoffset="{offset}"
    transform="rotate(-90 {size / 2} {size / 2})"/>
  <text x="{size / 2}" y="{size / 2}" text-anchor="middle" dominant-baseline="central"
    class="progress-ring-label" style="font-size:{font_size}px" fill="{color}">{round(percent)}%</text>
</svg>'''


def render_two_segment_ring_svg(pct1: float, color1: str, pct2: float, color2: str, center_value, center_label: str) -> str:
    """A genuine part-to-whole split (e.g. pre-order vs released) rather
    than a value against a ceiling - distinct from the single-value ring
    above, same as the app's own two ring helpers."""
    size, stroke = 100, 12
    r = (size - stroke) / 2
    circumference = 2 * math.pi * r
    offset1 = circumference * (1 - pct1 / 100)
    offset2 = -(circumference * pct1 / 100)
    return f'''<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" class="progress-ring-svg">
  <circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="var(--border)" stroke-width="{stroke}"/>
  <circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{color1}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{circumference}" stroke-dashoffset="{offset1}"
    transform="rotate(-90 {size / 2} {size / 2})"/>
  <circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{color2}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{circumference}" stroke-dashoffset="{offset2}"
    transform="rotate(-90 {size / 2} {size / 2})"/>
  <text x="{size / 2}" y="{size / 2 - 4}" text-anchor="middle" font-size="20" font-weight="700" fill="var(--text)">{center_value}</text>
  <text x="{size / 2}" y="{size / 2 + 12}" text-anchor="middle" font-size="8" fill="var(--text-muted)">{center_label}</text>
</svg>'''


def _step_svg_path(points):
    """Step-after interpolation - a horizontal line to each new x, then a
    vertical jump to the new y, for a staircase look. Suits a running
    total where each day's real jump should read as a clear step rather
    than a smoothed slope blurring together several days' worth of
    separate purchases."""
    d = f"M{points[0][0]},{points[0][1]}"
    for i in range(1, len(points)):
        prev_y = points[i - 1][1]
        x, y = points[i]
        d += f" L{x},{prev_y} L{x},{y}"
    return d


def render_trend_svg(chart_data, range_key, style="curve", label_font_size=12):
    """Self-contained SVG spend-trend chart - no external chart library, so
    the dashboard never needs to reach the internet to render it. Three
    toggleable series sharing one Y-axis (comics + shipping are genuine
    parts of total, not separate scales): a gradient area + glowing line
    for the combined total, plus two glowing lines (no area fill, to
    avoid clutter when overlaid) for comics and shipping alone. A thin
    bar strip beneath shows item count per period - a second real
    metric, not decoration. Hover reveals the exact breakdown regardless
    of which lines are toggled on; handled by a small shared script in
    base.html. Line visibility itself is pure CSS, toggled by the
    checkboxes below the chart - the underlying data for all three is
    always present in the markup."""
    if len(chart_data) < 2:
        return ""

    W, H = 900, 230
    pad_l, pad_r, pad_top, area_h, gap, bars_h = 44, 16, 14, 128, 10, 34
    n = len(chart_data)
    plot_w = W - pad_l - pad_r
    step = plot_w / (n - 1)

    max_total = max((c["total"] for c in chart_data), default=0) or 1
    max_count = max((c["count"] for c in chart_data), default=0) or 1

    def x_at(i):
        return round(pad_l + i * step, 1)

    def y_at(value):
        return round(pad_top + area_h - (value / max_total) * area_h, 1)

    baseline_y = pad_top + area_h
    grad_id = f"trendgrad-{range_key}"
    glow_id = f"trendglow-{range_key}"
    bars_top = pad_top + area_h + gap
    label_y = bars_top + bars_h + 16
    bar_w = min(18.0, step * 0.5)

    total_points = [(x_at(i), y_at(c["total"])) for i, c in enumerate(chart_data)]
    comics_points = [(x_at(i), y_at(c["comics_total"])) for i, c in enumerate(chart_data)]
    shipping_points = [(x_at(i), y_at(c["shipping_total"])) for i, c in enumerate(chart_data)]

    path_fn = _step_svg_path if style == "step" else _smooth_svg_path
    total_line = path_fn(total_points)
    comics_line = path_fn(comics_points)
    shipping_line = path_fn(shipping_points)
    total_area = f"{total_line} L{total_points[-1][0]},{baseline_y} L{total_points[0][0]},{baseline_y} Z"

    parts = [
        f'<svg viewBox="0 0 {W} {H}" class="trend-svg" preserveAspectRatio="none" '
        f'role="img" aria-label="Spend trend over time, with item counts below">',
        '<defs>',
        f'<linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="var(--neon-blue)" stop-opacity="0.4"/>'
        f'<stop offset="55%" stop-color="#6f8fff" stop-opacity="0.14"/>'
        f'<stop offset="100%" stop-color="var(--neon-violet)" stop-opacity="0.02"/>'
        f'</linearGradient>',
        f'<linearGradient id="{grad_id}-line" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0%" stop-color="var(--neon-blue)"/><stop offset="100%" stop-color="var(--neon-violet)"/>'
        f'</linearGradient>',
        f'<filter id="{glow_id}" x="-30%" y="-30%" width="160%" height="160%">'
        f'<feGaussianBlur stdDeviation="3.2" result="blur"/>'
        f'<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>'
        f'</filter>',
        '</defs>',
    ]

    for frac in (0, 0.5, 1):
        gy = round(pad_top + area_h * (1 - frac), 1)
        parts.append(f'<line x1="{pad_l}" y1="{gy}" x2="{W - pad_r}" y2="{gy}" '
                      f'stroke="var(--border)" stroke-width="1"/>')

    # Total: area fill + gradient glowing line + dot per point
    parts.append(f'<g class="trend-series trend-series-total">')
    parts.append(f'<path d="{total_area}" fill="url(#{grad_id})" stroke="none"/>')
    parts.append(f'<path d="{total_line}" fill="none" stroke="url(#{grad_id}-line)" '
                  f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" filter="url(#{glow_id})"/>')
    for i, (px, py) in enumerate(total_points):
        r = "6" if i == len(total_points) - 1 else "4"
        parts.append(f'<circle cx="{px}" cy="{py}" r="{r}" fill="var(--bg-1)" stroke="var(--neon-blue)" stroke-width="2.5"/>')
    parts.append('</g>')

    # Comics alone: glowing line + dots, no area (kept clean when overlaid with the others)
    parts.append(f'<g class="trend-series trend-series-comics">')
    parts.append(f'<path d="{comics_line}" fill="none" stroke="var(--neon-green)" '
                  f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.9" filter="url(#{glow_id})"/>')
    for px, py in comics_points:
        parts.append(f'<circle cx="{px}" cy="{py}" r="3.5" fill="var(--bg-1)" stroke="var(--neon-green)" stroke-width="2"/>')
    parts.append('</g>')

    # Shipping alone: same treatment, in pink to match its color elsewhere in the app
    parts.append(f'<g class="trend-series trend-series-shipping">')
    parts.append(f'<path d="{shipping_line}" fill="none" stroke="var(--neon-pink)" '
                  f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.9" filter="url(#{glow_id})"/>')
    for px, py in shipping_points:
        parts.append(f'<circle cx="{px}" cy="{py}" r="3.5" fill="var(--bg-1)" stroke="var(--neon-pink)" stroke-width="2"/>')
    parts.append('</g>')

    for i, c in enumerate(chart_data):
        bx = x_at(i)
        bh = round((c["count"] / max_count) * bars_h, 1) if max_count else 0
        by = round(bars_top + bars_h - bh, 1)
        parts.append(f'<rect x="{round(bx - bar_w / 2, 1)}" y="{by}" width="{round(bar_w, 1)}" '
                      f'height="{bh}" rx="2" fill="var(--neon-violet)" opacity="0.55"/>')

    for i, c in enumerate(chart_data):
        weight = "700" if c["is_current"] else "400"
        color = "var(--neon-blue)" if c["is_current"] else "var(--text-muted)"
        alt_class = " trend-axis-label-alt" if i % 2 == 1 else ""
        parts.append(f'<text class="trend-axis-label{alt_class}" x="{x_at(i)}" y="{label_y}" text-anchor="middle" font-size="{label_font_size}" '
                      f'font-weight="{weight}" fill="{color}">{c["label"]}</text>')

    hit_w = round(step, 1)
    for i, c in enumerate(chart_data):
        cx, cy = total_points[i]
        zx = round(cx - hit_w / 2, 1)
        parts.append(
            f'<rect class="trend-hit" x="{zx}" y="0" width="{hit_w}" height="{bars_top + bars_h}" '
            f'fill="transparent" data-label="{c["label"]}" data-total="{c["total"]:.2f}" '
            f'data-comics="{c["comics_total"]:.2f}" data-shipping="{c["shipping_total"]:.2f}" '
            f'data-count="{c["count"]}" data-cx="{cx}" data-cy="{cy}"/>'
        )

    parts.append('<line class="trend-guide" y1="14" x2="0" stroke="var(--neon-blue)" '
                 f'stroke-width="1" stroke-dasharray="3,3" opacity="0.4" style="display:none;" y2="{bars_top + bars_h}"/>')
    parts.append('<circle class="trend-dot" r="6" fill="var(--neon-blue)" '
                 'stroke="var(--bg-1)" stroke-width="2.5" style="display:none;"/>')
    parts.append('</svg>')
    return "".join(parts)


def build_chart_data(cur, today: date, range_key: str = DEFAULT_CHART_RANGE):
    cfg = RANGE_CONFIGS.get(range_key, RANGE_CONFIGS[DEFAULT_CHART_RANGE])
    unit = cfg["unit"]
    chart = []

    def totals_between(start: date, end: date):
        cur.execute(
            """
            SELECT * FROM items
            WHERE status != 'cancelled'
              AND date(COALESCE(release_date, placed_date)) BETWEEN date(?) AND date(?)
            """,
            (start.isoformat(), end.isoformat()),
        )
        period_items = [dict(r) for r in cur.fetchall()]
        comics_total = round(sum(i["price"] for i in period_items), 2)
        # group_by_date groups by release_date, so only items that actually
        # have one can go through it - an item with no release date yet
        # (counted here via its placed date instead) has no shipment date
        # to group shipping by, but its price still counts toward the total.
        dated_items = [i for i in period_items if i["release_date"]]
        groups = group_by_date(dated_items)
        shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, groups)
        return comics_total, shipping_total, len(period_items)

    if unit == "week":
        for delta in range(-cfg["back"], cfg["forward"] + 1):
            w_start = today + timedelta(days=delta * 7)
            w_end = w_start + timedelta(days=6)
            comics_total, shipping_total, n = totals_between(w_start, w_end)
            chart.append({
                "label": w_start.strftime("%d %b"),
                "comics_total": comics_total,
                "shipping_total": shipping_total,
                "total": round(comics_total + shipping_total, 2),
                "count": n,
                "is_current": delta == 0,
                "is_future": delta > 0,
            })
    else:  # month
        for delta in range(-cfg["back"], cfg["forward"] + 1):
            m_start = shift_month(today, delta)
            m_end = m_start.replace(day=calendar.monthrange(m_start.year, m_start.month)[1])
            comics_total, shipping_total, n = totals_between(m_start, m_end)
            chart.append({
                "label": m_start.strftime("%b"),
                "comics_total": comics_total,
                "shipping_total": shipping_total,
                "total": round(comics_total + shipping_total, 2),
                "count": n,
                "is_current": delta == 0,
                "is_future": delta > 0,
            })

    max_total = max((c["total"] for c in chart), default=0) or 1
    for c in chart:
        c["height_pct"] = round(min(100, (c["total"] / max_total) * 100), 1) if max_total else 0
    return chart


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


def _topbar_alert_count():
    """Real alert count for the topbar bell badge, queried fresh on every
    template render - a Jinja global rather than per-route context, so
    every single page shows the same real count regardless of whether
    that route's own Python function happens to compute duplicate/ghost/
    awaiting-charge data for its own purposes."""
    conn = db.get_db()
    cur = conn.cursor()
    today = date.today()
    count = len(find_duplicate_groups(cur)) + len(find_ghost_items(cur)) + len(find_awaiting_charge(cur, today))
    conn.close()
    return count


def _sync_feature_available():
    """Whether phone-app sync exists on this install at all. Hidden
    completely (no Sync settings tab, /api/sync refused) unless the
    container is started with KACHING_PHONE_SYNC=1 - parked until the
    Android app is ready again."""
    return os.environ.get("KACHING_PHONE_SYNC", "").strip().lower() in ("1", "true", "yes", "on")


def _sync_enabled():
    """Phone-app sync on/off (Settings -> Sync on the v2 UI). Off by default,
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


def _valid_category_id(cur, raw):
    """A submitted category id if it's real, otherwise None."""
    try:
        cat_id = int(raw)
    except (TypeError, ValueError):
        return None
    cur.execute("SELECT 1 FROM categories WHERE id = ?", (cat_id,))
    return cat_id if cur.fetchone() else None


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


def _resolve_category(cur, raw):
    """What a category box submitted -> a category id. The v2 forms let you
    pick an existing category or type a new one, so this accepts an id, an
    existing name (any capitalisation), or a brand-new name, which gets
    created on the spot. Blank or unusable input returns None."""
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if text.isdigit():
        valid = _valid_category_id(cur, text)
        if valid:
            return valid
    return _create_category(cur, text)


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


def _sidebar_budget_display():
    conn = db.get_db()
    value = notifications.get_setting(conn.cursor(), "sidebar_budget_display", "spent_of")
    conn.close()
    return value


templates.env.globals["sidebar_budget_display"] = _sidebar_budget_display
templates.env.globals["sync_feature_available"] = _sync_feature_available
templates.env.globals["topbar_budget_status"] = _topbar_budget_status


def get_year_to_date(cur, today: date):
    year_start = date(today.year, 1, 1)
    year_end = date(today.year, 12, 31)
    cur.execute(
        """
        SELECT charge_status, price FROM items
        WHERE status != 'cancelled'
          AND date(COALESCE(release_date, placed_date)) BETWEEN date(?) AND date(?)
        """,
        (year_start.isoformat(), year_end.isoformat()),
    )
    rows = cur.fetchall()
    spent = round(sum(r["price"] for r in rows if r["charge_status"] == "charged"), 2)
    total = round(sum(r["price"] for r in rows), 2)
    return {"year": today.year, "spent": spent, "total": total, "count": len(rows)}


def get_all_time_stats(cur):
    cur.execute("SELECT charge_status, price FROM items WHERE status != 'cancelled'")
    rows = cur.fetchall()
    spent = round(sum(r["price"] for r in rows if r["charge_status"] == "charged"), 2)
    total = round(sum(r["price"] for r in rows), 2)
    return {"spent": spent, "total": total, "count": len(rows)}


def get_all_sources(cur):
    cur.execute("SELECT DISTINCT source FROM items WHERE status != 'cancelled' ORDER BY source")
    return [r["source"] for r in cur.fetchall()]


def source_tab_group(source: str) -> str:
    """Groups per-seller eBay sources ('eBay - sad_lemon_comics', 'eBay -
    bearsgames', ...) into one 'eBay' entry for filter tabs - a separate
    tab per eBay seller gets unwieldy fast, and the specific seller still
    shows in the grouped item lists themselves, just not as its own tab."""
    if source == "eBay" or source.startswith("eBay -"):
        return "eBay"
    return source


def is_ebay_group(source: str | None) -> bool:
    return source == "eBay"


def get_filter_tab_sources(cur):
    """Top-level shop groups for the filter tabs - see source_tab_group."""
    raw = get_all_sources(cur)
    groups = []
    seen = set()
    for s in raw:
        g = source_tab_group(s)
        if g not in seen:
            seen.add(g)
            groups.append(g)
    return groups


def source_filter_sql(source: str):
    """Returns (sql_fragment, extra_params) for filtering items by source.
    'eBay' is treated as a group covering every per-seller eBay source."""
    if is_ebay_group(source):
        return "(source = 'eBay' OR source LIKE 'eBay -%')", []
    return "source = ?", [source]


# --- Dashboard ---------------------------------------------------------------

@app.get("/alerts")
def alerts_v2(request: Request, test_result: str | None = None, test_error: str | None = None, notif_import_result: str | None = None):
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()
    duplicate_groups = find_duplicate_groups(cur)
    ghost_items = find_ghost_items(cur)
    awaiting_charge = find_awaiting_charge(cur, today)
    cur.execute("SELECT * FROM notification_log ORDER BY id DESC LIMIT 20")
    notification_log = [dict(r) for r in cur.fetchall()]
    values = notifications.get_all_settings(cur)

    # Alert history log: real dismissed-duplicate events, real
    # awaiting-charge resolutions (item_history's charge_status ->
    # 'charged' entries), and real ghost-item removals (logged just
    # before the hard delete, since the item row itself won't exist
    # afterward to join against) - merged and sorted by date.
    alert_events = []
    cur.execute("SELECT name, release_date, dismissed_at FROM dismissed_duplicates ORDER BY dismissed_at DESC")
    for r in cur.fetchall():
        alert_events.append({
            "at": r["dismissed_at"],
            "kind": "duplicate",
            "text": f'Duplicate dismissed — "{r["name"]}"',
        })
    cur.execute(
        """
        SELECT ih.changed_at, i.name FROM item_history ih
        JOIN items i ON i.id = ih.item_id
        WHERE ih.field_name = 'charge_status' AND ih.new_value = 'charged'
        ORDER BY ih.changed_at DESC
        """
    )
    for r in cur.fetchall():
        alert_events.append({
            "at": r["changed_at"],
            "kind": "awaiting_charge",
            "text": f'Awaiting charge resolved — "{r["name"]}" marked paid',
        })
    cur.execute(
        """
        SELECT changed_at, old_value AS name FROM item_history
        WHERE field_name = 'removed'
        ORDER BY changed_at DESC
        """
    )
    for r in cur.fetchall():
        alert_events.append({
            "at": r["changed_at"],
            "kind": "ghost_item",
            "text": f'Ghost item removed — "{r["name"]}"',
        })
    alert_events.sort(key=lambda e: e["at"], reverse=True)
    alert_history = alert_events

    month_start = today.replace(day=1).isoformat()
    resolved_this_month = sum(1 for e in alert_events if e["at"] >= month_start)
    currently_open = len(duplicate_groups) + len(ghost_items) + len(awaiting_charge)
    if alert_events:
        last_event_date = date.fromisoformat(alert_events[0]["at"][:10])
        days_since_last_alert = (today - last_event_date).days
    else:
        days_since_last_alert = None

    conn.close()
    return templates.TemplateResponse("v2/alerts.html", {
        "request": request,
        "duplicate_groups": duplicate_groups,
        "ghost_items": ghost_items,
        "awaiting_charge": awaiting_charge,
        "notification_log": notification_log,
        "values": values,
        "alert_history": alert_history,
        "resolved_this_month": resolved_this_month,
        "currently_open": currently_open,
        "days_since_last_alert": days_since_last_alert,
        "test_result": test_result,
        "test_error": test_error,
        "notif_import_result": notif_import_result,
    })


@app.get("/classic")
@app.get("/classic/")
def dashboard(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None):
    ctx = _build_dashboard_context(request, month, chart_range, source, apply_landing_redirect=False)
    if isinstance(ctx, RedirectResponse):
        return ctx
    return templates.TemplateResponse("dashboard.html", ctx)


def _build_dashboard_context(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None, apply_landing_redirect: bool = True):
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    # Only redirect on a clean, unparameterised visit to "/" - once someone's
    # actively navigating the dashboard (a month, chart range, or shop filter
    # in the URL), respect that rather than bouncing them away mid-browse.
    # Skipped entirely for the /v2/ test routes (apply_landing_redirect=False)
    # since "go straight to Calendar" is a preference about the live site,
    # not something that should hijack a deliberate visit to the test UI.
    if apply_landing_redirect and not month and not chart_range and not source:
        landing = notifications.get_setting(cur, "default_landing_page", "dashboard")
        landing_paths = {"calendar": "/calendar", "search": "/search", "add": "/items/new"}
        if landing in landing_paths:
            conn.close()
            return RedirectResponse(url=landing_paths[landing], status_code=303)

    active_source = source if source else None

    cur.execute("SELECT client_label, last_synced_at FROM sync_state ORDER BY last_synced_at DESC LIMIT 1")
    most_recent_sync_row = cur.fetchone()
    most_recent_sync = None
    if most_recent_sync_row:
        synced_at = datetime.fromisoformat(most_recent_sync_row["last_synced_at"])
        most_recent_sync = {
            "client_label": most_recent_sync_row["client_label"] or "A device",
            "label": synced_at.strftime("%d %b, %H:%M"),
        }

    most_recent_backup = None
    backup_dir = os.path.join(os.path.dirname(db.DB_PATH), "backups")
    if os.path.isdir(backup_dir):
        backup_files = sorted(
            (f for f in os.listdir(backup_dir) if _AUTO_BACKUP_NAME_RE.match(f)),
            reverse=True,
        )
        if backup_files:
            fpath = os.path.join(backup_dir, backup_files[0])
            size_bytes = os.path.getsize(fpath)
            size_label = f"{size_bytes / 1024 / 1024:.1f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes / 1024:.1f} KB"
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            most_recent_backup = {
                "label": mtime.strftime("%d %b, %H:%M"),
                "size_label": size_label,
            }

    date_changes_flash = None
    cur.execute("SELECT value FROM settings WHERE key = '_flash_date_changes'")
    flash_row = cur.fetchone()
    if flash_row and flash_row["value"]:
        try:
            date_changes_flash = json.loads(flash_row["value"])
            for c in date_changes_flash:
                c["old_label"] = date.fromisoformat(c["old_date"]).strftime("%d %b")
                c["new_label"] = date.fromisoformat(c["new_date"]).strftime("%d %b")
        except (ValueError, TypeError):
            date_changes_flash = None
        cur.execute("DELETE FROM settings WHERE key = '_flash_date_changes'")
        conn.commit()

    week_end = today + timedelta(days=6)
    week_items = fetch_items_between(cur, today, week_end, active_source)
    week_groups = group_by_date(week_items)
    week_total = round(sum(i["price"] for i in week_items), 2)
    week_spent, week_remaining, week_spent_count, week_remaining_count = split_spent_remaining(week_items)

    # --- Hero: always the TRUE current month, all sources, regardless of what's being browsed below ---
    hero_start, hero_end = month_bounds(today)
    hero_items = fetch_items_between(cur, hero_start, hero_end)
    hero_groups = group_by_date(hero_items)
    hero_comics_total = round(sum(i["price"] for i in hero_items), 2)
    (hero_shipping_total, hero_spent_shipping, hero_remaining_shipping, hero_shipments,
     shipping_estimate, shipping_primary_source, shipping_source, shipping_samples, shipping_orders_checked
     ) = compute_shipping_for_groups(cur, hero_groups)
    hero_grand_total = round(hero_comics_total + hero_shipping_total, 2)

    monthly_budget_raw = notifications.get_setting(cur, "monthly_budget", "")
    budget_cycle = notifications.get_setting(cur, "budget_cycle", "monthly")
    budget_rollover = notifications.get_setting(cur, "budget_rollover", "no") == "yes"
    monthly_budget = None
    budget_pct = None
    budget_bar_pct = None
    cycle_spend = None
    budget_cycle_label = {"monthly": "this month", "weekly": "this week", "28day": "this 28-day period"}.get(budget_cycle, "this month")

    if monthly_budget_raw:
        try:
            base_budget = float(monthly_budget_raw)
            if base_budget > 0:
                if budget_cycle == "weekly":
                    cycle_spend = week_total
                elif budget_cycle == "28day":
                    twenty_eight_start = today - timedelta(days=27)
                    cur.execute(
                        "SELECT COALESCE(SUM(price), 0) AS s FROM items WHERE status != 'cancelled' AND date(release_date) BETWEEN date(?) AND date(?)",
                        (twenty_eight_start.isoformat(), today.isoformat()),
                    )
                    cycle_spend = cur.fetchone()["s"]
                else:
                    cycle_spend = hero_grand_total

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

                monthly_budget = effective_budget
                budget_pct = round((cycle_spend / effective_budget) * 100, 1)
                budget_bar_pct = min(100, budget_pct)
        except (ValueError, TypeError):
            monthly_budget = None

    hero_spent_comics, hero_remaining_comics, hero_spent_count, hero_remaining_count = split_spent_remaining(hero_items)
    hero_spent_total = round(hero_spent_comics + hero_spent_shipping, 2)
    hero_remaining_total = round(hero_remaining_comics + hero_remaining_shipping, 2)

    # Still Due ring: what fraction of this month's forecast total is
    # already paid off, same visual language as the app's own dashboard.
    hero_paid_pct = round((hero_spent_total / hero_grand_total) * 100) if hero_grand_total > 0 else 0
    still_due_ring_svg = render_progress_ring_svg(hero_paid_pct, is_over=False)
    budget_ring_svg = render_progress_ring_svg(budget_pct, is_over=budget_pct > 100) if monthly_budget else None

    nm_start = shift_month(today, 1)
    nm_end = nm_start.replace(day=calendar.monthrange(nm_start.year, nm_start.month)[1])
    next_month_items = fetch_items_between(cur, nm_start, nm_end)
    next_month_groups = group_by_date(next_month_items)
    next_month_comics_total = round(sum(i["price"] for i in next_month_items), 2)
    next_month_shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, next_month_groups)
    next_month_total = round(next_month_comics_total + next_month_shipping_total, 2)

    # --- "This month, by shipment": browsable to any month via ?month=YYYY-MM ---
    if month:
        try:
            viewed_month = datetime.strptime(month, "%Y-%m").date().replace(day=1)
        except ValueError:
            viewed_month = today.replace(day=1)
    else:
        viewed_month = today.replace(day=1)

    v_start, v_end = month_bounds(viewed_month)
    viewed_items = fetch_items_between(cur, v_start, v_end, active_source)
    viewed_groups = group_by_date(viewed_items)
    annotate_group_shipping(cur, viewed_groups)
    viewed_comics_total = round(sum(i["price"] for i in viewed_items), 2)
    (viewed_shipping_total, v_spent_shipping, v_remaining_shipping, viewed_shipments,
     _, _, _, _, _) = compute_shipping_for_groups(cur, viewed_groups)
    viewed_grand_total = round(viewed_comics_total + viewed_shipping_total, 2)

    v_spent_comics, v_remaining_comics, viewed_spent_count, viewed_remaining_count = split_spent_remaining(viewed_items)
    viewed_spent_total = round(v_spent_comics + v_spent_shipping, 2)
    viewed_remaining_total = round(v_remaining_comics + v_remaining_shipping, 2)

    prev_month_param = shift_month(viewed_month, -1).strftime("%Y-%m")
    next_month_param = shift_month(viewed_month, 1).strftime("%Y-%m")
    viewed_month_param = viewed_month.strftime("%Y-%m")
    is_current_month = (viewed_month.year == today.year and viewed_month.month == today.month)

    active_chart_range = chart_range if chart_range in RANGE_CONFIGS else DEFAULT_CHART_RANGE
    chart_data_all = {key: build_chart_data(cur, today, key) for key in RANGE_CONFIGS}
    chart_svg_all = {key: render_trend_svg(data, key) for key, data in chart_data_all.items()}

    year_stats = get_year_to_date(cur, today)
    all_time_stats = get_all_time_stats(cur)

    duplicate_groups = find_duplicate_groups(cur)
    ghost_items = find_ghost_items(cur)
    awaiting_charge = find_awaiting_charge(cur, today)
    all_sources = get_all_sources(cur)
    filter_tab_sources = get_filter_tab_sources(cur)
    source_colors = {s: source_color(s) for s in all_sources}
    source_shipping_rates = {s: get_shipping_estimate(cur, s)[0] for s in all_sources}

    cur.execute("SELECT COUNT(*) AS n FROM items")
    total_items_tracked = cur.fetchone()["n"]

    # Recently cancelled: last 15, but also dropped after 30 days so a
    # single cancellation from ages ago doesn't just sit here forever if
    # nothing newer has cancelled since. updated_at is set at the moment
    # the cancel action happens, so it doubles as "when cancelled" here.
    recently_cancelled_cutoff = (today - timedelta(days=30)).isoformat()
    cur.execute(
        "SELECT * FROM items WHERE status = 'cancelled' AND manual_override = 1 "
        "AND date(updated_at) >= date(?) ORDER BY id DESC LIMIT 15",
        (recently_cancelled_cutoff,),
    )
    recently_cancelled = [dict(r) for r in cur.fetchall()]

    # Next Up: the single nearest upcoming item, same filter convention
    # "Still to come" summary: the pipeline of everything not yet
    # released (not cancelled, has a release date in the future),
    # regardless of paid status - a pre-order you've already paid for
    # is still "on order" from a pipeline point of view. Replaces an
    # earlier single-item "Next Up" card, which turned out to just be a
    # smaller, less accurate copy of This Week directly below it -
    # picking one of several same-day items and only showing its own
    # price understated what was actually due that day. This instead
    # answers two questions nothing else on the page covers: what's the
    # single biggest thing still ahead, and how big is the whole
    # pipeline right now.
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) >= date(?)
        """,
        (today.isoformat(),),
    )
    still_to_come_items = [dict(r) for r in cur.fetchall()]
    biggest_still_to_come = max(still_to_come_items, key=lambda i: i["price"], default=None)
    if biggest_still_to_come:
        biggest_still_to_come["due_label"] = date.fromisoformat(biggest_still_to_come["release_date"]).strftime("%-d %b")
        biggest_still_to_come["days_until_due"] = (date.fromisoformat(biggest_still_to_come["release_date"]) - today).days
    still_to_come_count = len(still_to_come_items)
    still_to_come_total = round(sum(i["price"] for i in still_to_come_items), 2)

    conn.close()

    return {
        "request": request,
        "today": today,
        "current_month_label": today.strftime("%B %Y"),
        "next_month_label": nm_start.strftime("%B"),
        "week_groups": week_groups,
        "week_total": week_total,
        "week_spent": week_spent,
        "week_remaining": week_remaining,
        "hero_spent_total": hero_spent_total,
        "hero_remaining_total": hero_remaining_total,
        "hero_grand_total": hero_grand_total,
        "still_due_ring_svg": still_due_ring_svg,
        "budget_ring_svg": budget_ring_svg,
        "monthly_budget": monthly_budget,
        "budget_pct": budget_pct,
        "budget_bar_pct": budget_bar_pct,
        "budget_cycle_label": budget_cycle_label,
        "cycle_spend": cycle_spend,
        "date_changes_flash": date_changes_flash,
        "most_recent_sync": most_recent_sync,
        "most_recent_backup": most_recent_backup,
        "hero_spent_count": hero_spent_count,
        "next_month_total": next_month_total,
        "viewed_month_label": viewed_month.strftime("%B %Y"),
        "viewed_groups": viewed_groups,
        "viewed_grand_total": viewed_grand_total,
        "viewed_remaining_total": viewed_remaining_total,
        "viewed_item_count": len(viewed_items),
        "prev_month_param": prev_month_param,
        "next_month_param": next_month_param,
        "viewed_month_param": viewed_month_param,
        "is_current_month": is_current_month,
        "chart_data_all": chart_data_all,
        "chart_svg_all": chart_svg_all,
        "range_tabs": RANGE_TABS,
        "active_chart_range": active_chart_range,
        "shipping_estimate": shipping_estimate,
        "source_shipping_rates": source_shipping_rates,
        "shipping_source": shipping_source,
        "shipping_samples": shipping_samples,
        "shipping_orders_checked": shipping_orders_checked,
        "year_stats": year_stats,
        "all_time_stats": all_time_stats,
        "duplicate_groups": duplicate_groups,
        "ghost_items": ghost_items,
        "awaiting_charge": awaiting_charge,
        "all_sources": all_sources,
        "filter_tab_sources": filter_tab_sources,
        "source_colors": source_colors,
        "active_source": active_source,
        "is_ebay_filter": is_ebay_group(active_source) if active_source else False,
        "total_items_tracked": total_items_tracked,
        "has_any_data": total_items_tracked > 0,
        "recently_cancelled": recently_cancelled,
        "biggest_still_to_come": biggest_still_to_come,
        "still_to_come_count": still_to_come_count,
        "still_to_come_total": still_to_come_total,
    }


# --- v2 redesign (test routes, separate from the live UI above) -------------
# Reuses the exact same data-building logic as the real dashboard route -
# same queries, same numbers - just rendered into the new template set
# living in templates/v2/. Nothing here changes what "/" or any other
# existing route does.

@app.get("/v2")
@app.get("/v2/")
@app.get("/v2/dashboard")
def v2_home_redirect(request: Request):
    q = request.url.query
    return RedirectResponse(url="/" + ("?" + q if q else ""), status_code=301)


@app.get("/v2/{rest:path}")
def v2_redirect(rest: str, request: Request):
    """The redesign used to live under /v2/ while it was being tested;
    it's the main UI now, so old /v2/ links go to the same page without
    the prefix."""
    q = request.url.query
    return RedirectResponse(url="/" + rest + ("?" + q if q else ""), status_code=301)


@app.get("/")
def dashboard_v2(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None):
    ctx = _build_dashboard_context(request, month, chart_range, source)
    if isinstance(ctx, RedirectResponse):
        return ctx
    return templates.TemplateResponse("v2/dashboard.html", ctx)


@app.get("/orders")
def orders_v2(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None):
    ctx = _build_dashboard_context(request, month, chart_range, source, apply_landing_redirect=False)
    if isinstance(ctx, RedirectResponse):
        return ctx

    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND charge_status != 'charged'
        """
    )
    unpaid_items = [dict(r) for r in cur.fetchall()]
    awaiting_charge_rows = find_awaiting_charge(cur, today)
    awaiting_charge_ids = {r["id"] for r in awaiting_charge_rows}
    awaiting_charge_days = {r["id"]: r["days_late"] for r in awaiting_charge_rows}
    for it in unpaid_items:
        it["is_awaiting_charge"] = it["id"] in awaiting_charge_ids
    unpaid_groups = group_by_date(unpaid_items)
    for g in unpaid_groups:
        g_date = date.fromisoformat(g["date"]) if g["date"] else None
        days_late = max((awaiting_charge_days.get(it["id"], 0) for sg in g["source_groups"] for it in sg["entries"]), default=0)
        g["days_late"] = days_late if (g_date and g_date < today) else 0
    unpaid_total = round(sum(i["price"] for i in unpaid_items), 2)
    unpaid_count = len(unpaid_items)

    year_start = date(today.year, 1, 1).isoformat()
    cur.execute(
        """
        SELECT id, name, price, updated_at FROM items
        WHERE status = 'cancelled' AND date(updated_at) >= date(?)
        ORDER BY updated_at DESC
        """,
        (year_start,),
    )
    cancelled_this_year = [dict(r) for r in cur.fetchall()]
    for it in cancelled_this_year:
        it["cancelled_label"] = datetime.fromisoformat(it["updated_at"]).strftime("%-d %b")
    cancelled_saved_total = round(sum(i["price"] for i in cancelled_this_year), 2)

    # Recently cancelled for the v2 Orders card: same 30-day window and filter
    # as the shared dashboard context's recently_cancelled, but without its
    # LIMIT 15 - the v2 card paginates, so it can show everything in the window.
    # Kept separate so the old dashboard's capped list is left untouched.
    recent_cutoff = (today - timedelta(days=30)).isoformat()
    cur.execute(
        "SELECT * FROM items WHERE status = 'cancelled' AND manual_override = 1 "
        "AND date(updated_at) >= date(?) ORDER BY id DESC",
        (recent_cutoff,),
    )
    recently_cancelled_all = [dict(r) for r in cur.fetchall()]

    conn.close()

    ctx["unpaid_groups"] = unpaid_groups
    ctx["unpaid_total"] = unpaid_total
    ctx["unpaid_count"] = unpaid_count
    ctx["cancelled_this_year"] = cancelled_this_year
    ctx["cancelled_saved_total"] = cancelled_saved_total
    ctx["recently_cancelled_all"] = recently_cancelled_all

    return templates.TemplateResponse("v2/orders.html", ctx)


@app.post("/items/{item_id}/mark")
def mark_item(item_id: int, action: str = Form(...), next: str | None = Form(None)):
    conn = db.get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, name, order_number, release_date, status, charge_status, manual_override FROM items WHERE id = ?",
        (item_id,),
    )
    before = cur.fetchone()
    before_dict = dict(before) if before else None
    logger.info("MARK request: item_id=%s action=%s before=%s", item_id, action, before_dict)

    now = db.utc_now()

    if action == "paid":
        cur.execute(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                charge_status = 'charged',
                manual_override = 1,
                updated_at = ?
            WHERE id = ?
            """,
            (now, item_id),
        )
        # This quick toggle is how most items actually get marked paid day
        # to day - the full edit form's own history logging (elsewhere in
        # this file) never sees this path at all, which silently starved
        # the per-shop charging-pattern insight of virtually all its real
        # data despite people genuinely using this button constantly.
        if before_dict and before_dict["charge_status"] != "charged":
            cur.execute(
                "INSERT INTO item_history (item_id, changed_at, field_name, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
                (item_id, now, "charge_status", before_dict["charge_status"], "charged"),
            )
    elif action == "cancel":
        cur.execute(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                status = 'cancelled',
                manual_override = 1,
                updated_at = ?
            WHERE id = ?
            """,
            (now, item_id),
        )
    elif action == "undo":
        cur.execute(
            """
            UPDATE items
            SET status = COALESCE(prev_status, status),
                charge_status = COALESCE(prev_charge_status, charge_status),
                manual_override = 0,
                prev_status = NULL,
                prev_charge_status = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, item_id),
        )
    elif action == "remove":
        # Permanent delete - for bad data (duplicate line items, parsing
        # artifacts) rather than a real-world cancellation. No Undo.
        # Still hard-deleted for now - soft-delete (deleted_at) so removals
        # propagate over sync instead of just vanishing locally is coming
        # in the sync endpoint work itself, not this schema pass.
        #
        # Log the removal before deleting, so the Alerts page's history
        # has something real to show - the item's name is stored directly
        # in the log row (not just item_id) since a join to the items
        # table won't find anything once this row is gone.
        cur.execute("SELECT name FROM items WHERE id = ?", (item_id,))
        removed_row = cur.fetchone()
        if removed_row:
            cur.execute(
                "INSERT INTO item_history (item_id, changed_at, field_name, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
                (item_id, now, "removed", removed_row["name"], None),
            )
        cur.execute("DELETE FROM items WHERE id = ?", (item_id,))
    else:
        logger.warning("MARK request with unknown action=%s item_id=%s - no update applied", action, item_id)

    rowcount = cur.rowcount
    conn.commit()

    cur.execute(
        "SELECT id, name, order_number, release_date, status, manual_override FROM items WHERE id = ?",
        (item_id,),
    )
    after = cur.fetchone()
    after_dict = dict(after) if after else None
    logger.info("MARK result: item_id=%s action=%s rowcount=%s after=%s", item_id, action, rowcount, after_dict)

    conn.close()
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/duplicates/dismiss")
def dismiss_duplicate(name: str = Form(...), release_date: str = Form(...), next: str | None = Form(None)):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO dismissed_duplicates (name, release_date, dismissed_at) VALUES (?, ?, ?)",
        (name, release_date, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/items/bulk-action")
def bulk_item_action(
    item_ids: list[str] = Form(...),
    bulk_action: str = Form(...),
    next: str | None = Form(None),
):
    conn = db.get_db()
    cur = conn.cursor()
    now_ids = [int(i) for i in item_ids if i.isdigit()]
    now = db.utc_now()

    if bulk_action == "remove":
        cur.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in now_ids])
        logger.info("BULK ACTION: removed %d items: %s", cur.rowcount, now_ids)
    elif bulk_action == "cancel":
        cur.executemany(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                status = 'cancelled',
                manual_override = 1,
                updated_at = ?
            WHERE id = ?
            """,
            [(now, i) for i in now_ids],
        )
        logger.info("BULK ACTION: cancelled %d items: %s", len(now_ids), now_ids)
    elif bulk_action == "paid":
        cur.executemany(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                charge_status = 'charged',
                manual_override = 1,
                updated_at = ?
            WHERE id = ?
            """,
            [(now, i) for i in now_ids],
        )
        logger.info("BULK ACTION: marked %d items paid: %s", len(now_ids), now_ids)
    else:
        logger.warning("BULK ACTION: unknown action=%s, no changes made", bulk_action)

    conn.commit()
    conn.close()
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/ghost-items/remove-all")
def remove_all_ghost_items():
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, price FROM items WHERE source = ? AND order_number IS NULL AND status != 'cancelled'",
        (DEFAULT_SOURCE,),
    )
    to_remove = cur.fetchall()
    logger.info(
        "GHOST BULK REMOVE: deleting %d items: %s",
        len(to_remove), [(r["id"], r["name"], r["price"]) for r in to_remove],
    )
    cur.execute(
        "DELETE FROM items WHERE source = ? AND order_number IS NULL AND status != 'cancelled'",
        (DEFAULT_SOURCE,),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


# --- Manual add / edit items --------------------------------------------------

def _parse_item_form_date(raw: str, fallback: date) -> str:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()
    except (ValueError, AttributeError):
        return fallback.isoformat()


@app.get("/classic/items/new")
def new_items_form(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_all_sources(cur)
    cur.execute("SELECT source FROM items ORDER BY imported_at DESC LIMIT 1")
    last_row = cur.fetchone()
    last_used_source = last_row["source"] if last_row else DEFAULT_SOURCE
    conn.close()
    return templates.TemplateResponse("add_items.html", {
        "request": request,
        "all_sources": all_sources,
        "last_used_source": last_used_source,
        "result": None,
    })


@app.get("/items/new")
def new_items_form_v2(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_all_sources(cur)
    cur.execute("SELECT source FROM items ORDER BY imported_at DESC LIMIT 1")
    last_row = cur.fetchone()
    last_used_source = last_row["source"] if last_row else DEFAULT_SOURCE
    conn.close()
    return templates.TemplateResponse("v2/logorders.html", {
        "request": request,
        "all_sources": all_sources,
        "last_used_source": last_used_source,
        "result": None,
    })


@app.post("/items/new")
async def create_items(request: Request):
    form = await request.form()
    names = form.getlist("name")
    prices = form.getlist("price")
    category_ids = form.getlist("category_id")
    release_date = form.get("release_date", "")
    source = (form.get("source") or "").strip() or DEFAULT_SOURCE
    already_paid = form.get("already_paid")
    order_number = (form.get("order_number") or "").strip() or None
    shipping_cost_raw = (form.get("shipping_cost") or "").strip()
    tracking_number = (form.get("tracking_number") or "").strip() or None
    next_url = form.get("next") or "/"

    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    charge_status = "charged" if already_paid else "not_charged"
    now = datetime.now(timezone.utc).isoformat()

    conn = db.get_db()
    cur = conn.cursor()
    created = []
    fallback_category = db.default_category_id(conn)
    for idx, (raw_name, raw_price) in enumerate(zip(names, prices)):
        clean_name = raw_name.strip()
        if not clean_name:
            continue
        try:
            price_val = float(raw_price)
        except (TypeError, ValueError):
            continue
        category_id = _resolve_category(cur, category_ids[idx] if idx < len(category_ids) else None) or fallback_category
        cur.execute(
            """
            INSERT INTO items
                (name, order_number, placed_date, status, release_date, charge_status,
                 price, note, imported_at, manual_override, source, tracking_number, uuid, updated_at, category_id)
            VALUES (?, ?, ?, 'preorder', ?, ?, ?, NULL, ?, 1, ?, ?, ?, ?, ?)
            """,
            (clean_name, order_number, today.isoformat(), release_iso, charge_status, price_val, now, source,
             tracking_number, db.new_uuid(), now, category_id),
        )
        created.append((cur.lastrowid, clean_name, price_val))

    if shipping_cost_raw and order_number:
        try:
            shipping_val = float(shipping_cost_raw)
            cur.execute(
                """
                INSERT INTO shipment_postage (order_number, shipment_index, amount, captured_at, source)
                VALUES (?, 0, ?, ?, ?)
                ON CONFLICT(order_number, shipment_index) DO UPDATE SET
                    amount = excluded.amount, captured_at = excluded.captured_at, source = excluded.source
                """,
                (order_number, shipping_val, now, source),
            )
        except ValueError:
            pass

    conn.commit()
    conn.close()
    logger.info(
        "MANUAL BATCH ADD: release_date=%s source=%r order_number=%s shipping=%s created=%s",
        release_iso, source, order_number, shipping_cost_raw, created,
    )
    return RedirectResponse(url=next_url, status_code=303)


@app.get("/classic/items/{item_id}/edit")
def edit_item_form(request: Request, item_id: int):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()
    all_sources = get_all_sources(cur)
    cur.execute(
        "SELECT * FROM item_history WHERE item_id = ? ORDER BY id DESC LIMIT 20",
        (item_id,),
    )
    edit_history = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not item:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("item_form.html", {
        "request": request,
        "item": dict(item),
        "form_action": f"/items/{item_id}/edit",
        "all_sources": all_sources,
        "heading": "Edit item",
        "submit_label": "Save changes",
        "edit_history": edit_history,
    })


@app.get("/items/{item_id}/edit")
def edit_item_form_v2(request: Request, item_id: int):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()
    all_sources = get_all_sources(cur)
    cur.execute(
        "SELECT * FROM item_history WHERE item_id = ? ORDER BY id DESC LIMIT 20",
        (item_id,),
    )
    edit_history = [dict(r) for r in cur.fetchall()]
    for h in edit_history:
        try:
            h["date_label"] = datetime.fromisoformat(h["changed_at"]).strftime("%d %b")
        except (ValueError, TypeError):
            h["date_label"] = h["changed_at"][:10] if h["changed_at"] else ""
    conn.close()
    if not item:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("v2/edititem.html", {
        "request": request,
        "item": dict(item),
        "form_action": f"/items/{item_id}/edit",
        "form_next": "/",
        "all_sources": all_sources,
        "heading": "Edit item",
        "submit_label": "Save changes",
        "edit_history": edit_history,
    })


@app.post("/items/{item_id}/edit")
def update_item(
    item_id: int,
    name: str = Form(...),
    price: float = Form(...),
    release_date: str = Form(...),
    source: str = Form(...),
    already_paid: str | None = Form(None),
    tracking_number: str = Form(""),
    note: str = Form(""),
    next: str = Form("/"),
    category_id: str | None = Form(None),
):
    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    charge_status = "charged" if already_paid else "not_charged"
    source_clean = source.strip() or DEFAULT_SOURCE
    tracking_clean = tracking_number.strip() or None
    note_clean = note.strip() or None

    conn = db.get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    existing = cur.fetchone()
    now = db.utc_now()

    new_values = {
        "name": name.strip(), "price": price, "release_date": release_iso,
        "source": source_clean, "charge_status": charge_status,
        "tracking_number": tracking_clean, "note": note_clean,
    }
    # Category is only changed when the form actually sent one (the v2 edit
    # form). The old UI's form has no category field, so it leaves it alone.
    new_category_id = _resolve_category(cur, category_id) if category_id is not None else None
    if existing is not None and new_category_id and new_category_id != existing["category_id"]:
        cur.execute("SELECT id, name FROM categories WHERE id IN (?, ?)", (existing["category_id"], new_category_id))
        cat_names = {r["id"]: r["name"] for r in cur.fetchall()}
        cur.execute(
            "INSERT INTO item_history (item_id, changed_at, field_name, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
            (item_id, now, "category", cat_names.get(existing["category_id"]), cat_names.get(new_category_id)),
        )
        cur.execute("UPDATE items SET category_id = ? WHERE id = ?", (new_category_id, item_id))
    if existing is not None:
        for field, new_val in new_values.items():
            old_val = existing[field]
            if str(old_val or "") != str(new_val or ""):
                cur.execute(
                    "INSERT INTO item_history (item_id, changed_at, field_name, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
                    (item_id, now, field, old_val, new_val),
                )

    cur.execute(
        """
        UPDATE items
        SET name = ?, price = ?, release_date = ?, source = ?, charge_status = ?, manual_override = 1,
            tracking_number = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (name.strip(), price, release_iso, source_clean, charge_status, tracking_clean, note_clean, now, item_id),
    )
    conn.commit()
    conn.close()
    logger.info(
        "MANUAL EDIT: id=%s name=%r price=%s release_date=%s source=%r",
        item_id, name, price, release_iso, source_clean,
    )
    return RedirectResponse(url=next, status_code=303)


@app.post("/items/{item_id}/delay")
def delay_item(item_id: int, days: int = Form(...), next: str | None = Form(None)):
    """One-tap manufacturer-delay handling: push a release date back by a
    fixed number of days (+30/+60/+90) without opening the full edit
    form. Only +30/+60/+90 are accepted - anything else is a genuinely
    custom date change, which the full edit form already handles."""
    if days not in (30, 60, 90):
        return RedirectResponse(url=next or "/", status_code=303)

    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT release_date FROM items WHERE id = ?", (item_id,))
    existing = cur.fetchone()
    if existing is None or not existing["release_date"]:
        conn.close()
        return RedirectResponse(url=next or "/", status_code=303)

    old_date = date.fromisoformat(existing["release_date"])
    new_date = (old_date + timedelta(days=days)).isoformat()
    now = db.utc_now()

    cur.execute(
        "INSERT INTO item_history (item_id, changed_at, field_name, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
        (item_id, now, "release_date", existing["release_date"], new_date),
    )
    cur.execute(
        "UPDATE items SET release_date = ?, manual_override = 1, updated_at = ? WHERE id = ?",
        (new_date, now, item_id),
    )
    conn.commit()
    conn.close()
    logger.info("DELAY: id=%s old_date=%s new_date=%s (+%s days)", item_id, existing["release_date"], new_date, days)
    return RedirectResponse(url=next or f"/items/{item_id}/edit", status_code=303)

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


def build_search_query(q, source, status, start_date, end_date, min_price=None, max_price=None, has_tracking=None):
    """Returns (where_clause, params) shared by the search page and CSV
    export, so both stay in sync with exactly the same filtering logic."""
    conditions = []
    params = []
    if q and q.strip():
        term = f"%{q.strip()}%"
        conditions.append("(name LIKE ? OR order_number LIKE ? OR source LIKE ? OR tracking_number LIKE ?)")
        params.extend([term, term, term, term])
    if source:
        clause, extra_params = source_filter_sql(source)
        conditions.append(clause)
        params.extend(extra_params)
    if status == "paid":
        conditions.append("charge_status = 'charged' AND status != 'cancelled'")
    elif status == "unpaid":
        conditions.append("charge_status != 'charged' AND status != 'cancelled'")
    elif status == "cancelled":
        conditions.append("status = 'cancelled'")
    if has_tracking == "yes":
        conditions.append("tracking_number IS NOT NULL AND tracking_number != ''")
    elif has_tracking == "no":
        conditions.append("(tracking_number IS NULL OR tracking_number = '')")
    if start_date:
        conditions.append("release_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("release_date <= ?")
        params.append(end_date)
    if min_price is not None:
        conditions.append("price >= ?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("price <= ?")
        params.append(max_price)
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/debug/shipping-groups")
def debug_shipping_groups(request: Request, source: str = DEFAULT_SOURCE, key: str | None = None):
    """Ad-hoc diagnostic mirroring compute_shipping_for_groups exactly,
    but exposing the per-group detail (date, orders, real-vs-estimated,
    rate) instead of just the aggregate total - added specifically to
    compare directly against the app's own equivalent debug view when
    the two totals didn't match and aggregate figures alone weren't
    enough to find out why.

    Off by default (DEBUG_TOOLS_ENABLED) and requires the same sync key
    used for /api/sync as a ?key= query param, since this is a developer
    diagnostic rather than something most self-hosters need day to day."""
    if not DEBUG_TOOLS_ENABLED:
        raise HTTPException(status_code=404)
    conn = db.get_db()
    cur = conn.cursor()
    stored_key = notifications.get_setting(cur, "sync_api_key", None)
    if not stored_key or not key or not secrets.compare_digest(key, stored_key):
        conn.close()
        raise HTTPException(status_code=401, detail="Missing or invalid ?key= - use the same key shown in Settings \u2192 Sync.")

    cur.execute("SELECT * FROM items WHERE status != 'cancelled' AND source = ?", (source,))
    items = [dict(r) for r in cur.fetchall()]
    dated_items = [i for i in items if i["release_date"]]

    cur.execute("SELECT order_number, SUM(amount) AS amount FROM shipment_postage GROUP BY order_number")
    # SUMs every shipment_index for a given order - a split delivery (the
    # same order captured across more than one real postage figure) must
    # count all of them, not just whichever row a plain dict comprehension
    # happened to keep last. This was a real, confirmed bug: an order with
    # two real shipments was silently having one of them dropped here.
    exact_by_order = {r["order_number"]: r["amount"] for r in cur.fetchall()}

    by_date = {}
    for it in dated_items:
        by_date.setdefault(it["release_date"], []).append(it)

    rate_cache = {}
    def estimate_for(src):
        if src not in rate_cache:
            rate_cache[src] = get_shipping_estimate(cur, src)
        return rate_cache[src]

    rows = []
    for release_date in sorted(by_date.keys()):
        group_items = by_date[release_date]
        order_numbers = {i["order_number"] for i in group_items if i["order_number"]}
        known = [exact_by_order[o] for o in order_numbers if o in exact_by_order]
        if order_numbers and len(known) == len(order_numbers):
            rate = round(sum(known), 2)
            src_label = "real"
        else:
            rate, _, _, _ = estimate_for(source)
            src_label = "estimated"
        rows.append({
            "date": release_date,
            "orders": ",".join(sorted(order_numbers)) or "none",
            "item_count": len(group_items),
            "source": src_label,
            "rate": rate,
        })

    total_real = round(sum(r["rate"] for r in rows if r["source"] == "real"), 2)
    total_estimated = round(sum(r["rate"] for r in rows if r["source"] == "estimated"), 2)
    real_count = sum(1 for r in rows if r["source"] == "real")
    estimated_count = sum(1 for r in rows if r["source"] == "estimated")

    lines = [f"{len(rows)} shipment groups \u00b7 {real_count} real (£{total_real}) \u00b7 {estimated_count} estimated (£{total_estimated})", ""]
    for r in rows:
        lines.append(f"{r['date']}  [{r['source'].upper():<9}]  £{r['rate']:.2f}  ({r['item_count']} items, order {r['orders']})")

    conn.close()
    return PlainTextResponse("\n".join(lines))


@app.get("/classic/insights")
def insights_page(request: Request):
    ctx = _build_insights_context(request)
    return templates.TemplateResponse("insights.html", ctx)


def _insights_category_chips(cats_param):
    """Category filter chips for the v2 Price creep / Top titles pages.

    Only categories that currently have (non-cancelled) items get a chip.
    With no ?cats= at all, the default is every category with "has
    series" switched on (or all of them, if none of those have items).
    ?cats= present but empty means the user switched every chip off.
    Returns (chips, selected_ids)."""
    conn = db.get_db()
    default_id = db.default_category_id(conn)
    cats = [dict(r) for r in conn.execute(
        "SELECT id, name, color, has_series FROM categories ORDER BY sort_order, id"
    ).fetchall()]
    known_ids = {c["id"] for c in cats}
    counts = {}
    for row in conn.execute(
        "SELECT category_id, COUNT(*) FROM items WHERE status != 'cancelled' GROUP BY category_id"
    ).fetchall():
        cid = row[0] if row[0] in known_ids else default_id
        counts[cid] = counts.get(cid, 0) + row[1]
    conn.close()

    chips = [c for c in cats if counts.get(c["id"], 0) > 0]
    chip_ids = [c["id"] for c in chips]
    defaults = [c["id"] for c in chips if c["has_series"]] or list(chip_ids)
    for c in chips:
        c["is_default"] = c["id"] in defaults

    if cats_param is None:
        selected = set(defaults)
    else:
        wanted = set()
        for part in cats_param.split(","):
            part = part.strip()
            if part.isdigit():
                wanted.add(int(part))
        selected = wanted & set(chip_ids)
        # Every requested category has since been deleted or emptied -
        # fall back to the default rather than showing nothing.
        if wanted and not selected:
            selected = set(defaults)
    for c in chips:
        c["on"] = c["id"] in selected
    return chips, selected


def _build_insights_context(request: Request, category_ids=None):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE status != 'cancelled'")
    all_items = [dict(r) for r in cur.fetchall()]
    if category_ids is not None:
        # Items with no (or a since-deleted) category count as the default
        # category, same as the backfill would make them.
        _default_cat = db.default_category_id(conn)
        _known_cats = {r[0] for r in cur.execute("SELECT id FROM categories").fetchall()}
        all_items = [
            i for i in all_items
            if (i.get("category_id") if i.get("category_id") in _known_cats else _default_cat) in category_ids
        ]
    dated_items = [i for i in all_items if i["release_date"]]

    by_month = {}
    for it in dated_items:
        month_key = it["release_date"][:7]
        by_month.setdefault(month_key, []).append(it)

    month_stats = []
    for month_key, items in by_month.items():
        comics_total = round(sum(i["price"] for i in items), 2)
        groups = group_by_date(items)
        shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, groups)
        month_stats.append({
            "month_key": month_key,
            "comics_total": comics_total,
            "shipping_total": shipping_total,
            "total": round(comics_total + shipping_total, 2),
            "count": len(items),
        })

    top_month = max(month_stats, key=lambda m: m["total"]) if month_stats else None
    if top_month:
        top_month["label"] = datetime.strptime(top_month["month_key"], "%Y-%m").strftime("%B %Y")

    # 12-month rolling trend - same chart style as the dashboard, filling
    # in any quiet months with zero rather than skipping them so the
    # spacing along the axis stays even.
    today_month = date.today().replace(day=1)
    twelve_month_data = []
    by_month_key = {m["month_key"]: m for m in month_stats}
    for i in range(11, -1, -1):
        m = shift_month(today_month, -i)
        key = m.strftime("%Y-%m")
        found = by_month_key.get(key)
        twelve_month_data.append({
            "label": m.strftime("%b"),
            "total": found["total"] if found else 0.0,
            "comics_total": found["comics_total"] if found else 0.0,
            "shipping_total": found["shipping_total"] if found else 0.0,
            "count": found["count"] if found else 0,
            "is_current": (i == 0),
        })
    twelve_month_svg = render_trend_svg(twelve_month_data, "twelvemonth")

    total_issues = len(all_items)
    priciest_item = max(all_items, key=lambda i: i["price"]) if all_items else None
    if priciest_item:
        priciest_item["release_date_label"] = (
            date.fromisoformat(priciest_item["release_date"]).strftime("%d %b %Y")
            if priciest_item["release_date"] else "no date set"
        )
    top_titles = sorted(all_items, key=lambda i: -i["price"])[:5]

    # Price creep: group items into a "series" by stripping the issue
    # number and anything after it (variant info usually follows the
    # issue number), then compare the earliest vs most recent price for
    # any series with real history. A one-shot with no "#N" in its name
    # just won't match anything else, which is correct - there's no
    # series to track creep across.
    series_groups = {}
    for it in all_items:
        m = re.match(r"^(.*?)\s*#\d+", it["name"])
        if not m:
            continue
        series_key = m.group(1).strip()
        sort_key = it["release_date"] or it["placed_date"] or ""
        series_groups.setdefault(series_key, []).append({"sort_key": sort_key, "price": it["price"], "name": it["name"], "release_date": it["release_date"]})

    # Real per-series stats for EVERY series with 2+ tracked issues (not
    # just the ones that got pricier) - powers the stat cards, the
    # distribution buckets, and the full ranked table with real
    # sparklines built from each series' actual price history.
    all_series_stats = []
    for series_key, entries in series_groups.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: e["sort_key"])
        first_price = entries[0]["price"]
        latest_price = entries[-1]["price"]
        change_pct = round(((latest_price - first_price) / first_price) * 100) if first_price else 0
        prices = [e["price"] for e in entries]
        min_p, max_p = min(prices), max(prices)
        spread = (max_p - min_p) or 1
        spark_pts = []
        n = len(prices)
        for i, p in enumerate(prices):
            sx = round((i / (n - 1)) * 70, 1) if n > 1 else 0
            sy = round(22 - ((p - min_p) / spread) * 20, 1)
            spark_pts.append(f"{sx},{sy}")
        all_series_stats.append({
            "series": series_key,
            "issue_count": len(entries),
            "first_price": first_price,
            "latest_price": latest_price,
            "change_pct": change_pct,
            "sparkline_points": " ".join(spark_pts),
            "entries": entries,
        })

    price_creep = []
    for series_key, entries in series_groups.items():
        entries.sort(key=lambda e: e["sort_key"])
        first_price = entries[0]["price"]
        latest_price = entries[-1]["price"]
        if latest_price > first_price + 0.01:
            price_creep.append({
                "series": series_key,
                "first_price": first_price,
                "latest_price": latest_price,
                "increase_pct": round(((latest_price - first_price) / first_price) * 100) if first_price else 0,
                "issue_count": len(entries),
            })
    price_creep.sort(key=lambda p: -(p["latest_price"] - p["first_price"]))
    price_creep_all = price_creep  # full list, before the top-3 cap below
    price_creep = price_creep[:3]

    # Real stat-card figures
    creep_series_tracked = len(all_series_stats)
    creep_avg_increase = round(sum(s["change_pct"] for s in all_series_stats) / creep_series_tracked, 1) if creep_series_tracked else 0
    creep_biggest_jumper = max(all_series_stats, key=lambda s: s["change_pct"], default=None)

    # Real distribution buckets, counting only series that actually increased
    creep_buckets = [
        {"label": "0\u20135%", "min": 0, "max": 5, "count": 0, "color": "#3cf2a6"},
        {"label": "5\u201310%", "min": 5, "max": 10, "count": 0, "color": "#67e6c4"},
        {"label": "10\u201315%", "min": 10, "max": 15, "count": 0, "color": "#7bb8f0"},
        {"label": "15\u201325%", "min": 15, "max": 25, "count": 0, "color": "#a487ff"},
        {"label": "25%+", "min": 25, "max": 999999, "count": 0, "color": "#ff4d8d"},
    ]
    for p in price_creep_all:
        for b in creep_buckets:
            if b["min"] <= p["increase_pct"] < b["max"]:
                b["count"] += 1
                break
    creep_bucket_max = max((b["count"] for b in creep_buckets), default=0) or 1
    for b in creep_buckets:
        b["bar_pct"] = round((b["count"] / creep_bucket_max) * 100)

    # Real "Extra spend from creep" - for every series with genuine price
    # creep, every issue bought AFTER the price rose above its own
    # first-tracked price counts its markup (price - first_price) toward
    # the month it was bought in, then run a real cumulative total
    # across the last 12 months.
    creep_series_keys = {p["series"] for p in price_creep_all}
    creep_month_extra = {}
    creep_total_extra = 0.0
    for s in all_series_stats:
        if s["series"] not in creep_series_keys:
            continue
        first_price = s["first_price"]
        for e in s["entries"]:
            if e["price"] > first_price + 0.01 and e["release_date"]:
                extra = e["price"] - first_price
                month_key = e["release_date"][:7]
                creep_month_extra[month_key] = creep_month_extra.get(month_key, 0) + extra
                creep_total_extra += extra
    creep_total_extra = round(creep_total_extra, 2)

    creep_month_keys = []
    creep_today = date.today()
    creep_range_start = creep_today.replace(day=1) - timedelta(days=365)
    creep_range_start = creep_range_start.replace(day=1)
    creep_cursor = creep_range_start
    while creep_cursor <= creep_today.replace(day=1):
        creep_month_keys.append(creep_cursor.strftime("%Y-%m"))
        creep_cursor = (creep_cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    creep_cumulative = []
    running = 0.0
    for mk in creep_month_keys:
        running += creep_month_extra.get(mk, 0)
        creep_cumulative.append({
            "month_key": mk,
            "label": datetime.strptime(mk, "%Y-%m").strftime("%b"),
            "value": round(running, 2),
        })
    creep_cumulative_json = json.dumps(creep_cumulative)

    # Every series ranked by total spend (not just the ones with price
    # creep) - reuses the same series_groups grouping above, just a
    # different cut of it: total money spent per series, for the "top
    # series by spend" view rather than "which series got pricier".
    series_totals = []
    for series_key, entries in series_groups.items():
        total_spent = round(sum(e["price"] for e in entries), 2)
        prices = [e["price"] for e in entries]
        dates = [e["sort_key"] for e in entries if e["sort_key"]]
        series_totals.append({
            "series": series_key,
            "total": total_spent,
            "count": len(entries),
            "avg_price": round(total_spent / len(entries), 2) if entries else 0,
            "first_date": min(dates) if dates else None,
            "latest_date": max(dates) if dates else None,
        })
    all_series_totals = list(series_totals)
    for s in all_series_totals:
        s["first_date_label"] = date.fromisoformat(s["first_date"]).strftime("%b %Y") if s["first_date"] else "\u2014"
        s["latest_date_label"] = date.fromisoformat(s["latest_date"]).strftime("%b %Y") if s["latest_date"] else "\u2014"
    series_totals.sort(key=lambda s: -s["total"])
    series_totals = series_totals[:10]
    top_series_by_spend = series_totals[0] if series_totals else None

    most_collected_series = max(all_series_totals, key=lambda s: s["count"], default=None)
    longest_running_series = min((s for s in all_series_totals if s["first_date"]), key=lambda s: s["first_date"], default=None)
    if longest_running_series:
        years_running = (date.today() - date.fromisoformat(longest_running_series["first_date"])).days / 365.25
        longest_running_series = dict(longest_running_series)
        longest_running_series["years_running"] = round(years_running, 1)
        longest_running_series["first_date_label"] = date.fromisoformat(longest_running_series["first_date"]).strftime("%b %Y")

    for t in top_titles:
        t["release_date_label"] = (
            date.fromisoformat(t["release_date"]).strftime("%d %b %Y") if t["release_date"] else "no date set"
        )
        m = re.match(r"^(.*?)\s*#\d+", t["name"])
        t["series"] = m.group(1).strip() if m else t["name"]

    # Spend by category: same item set as every other total on this page
    # (everything not cancelled). "This year" goes by release date, since
    # that's when the money actually goes out.
    cur.execute("SELECT id, name, color FROM categories ORDER BY sort_order, id")
    category_rows = [dict(r) for r in cur.fetchall()]
    this_year_prefix = str(date.today().year)
    cat_all, cat_year = {}, {}
    for it in all_items:
        cid = it.get("category_id")
        cat_all[cid] = cat_all.get(cid, 0.0) + it["price"]
        if (it["release_date"] or "").startswith(this_year_prefix):
            cat_year[cid] = cat_year.get(cid, 0.0) + it["price"]

    def _category_spend(totals):
        rows = [
            {"name": c["name"], "color": c["color"], "total": round(totals.get(c["id"], 0.0), 2)}
            for c in category_rows if totals.get(c["id"], 0.0) > 0
        ]
        rows.sort(key=lambda r: -r["total"])
        return rows

    spend_by_category_all = _category_spend(cat_all)
    spend_by_category_year = _category_spend(cat_year)

    months_with_data = len(month_stats) or 1
    total_all_comics = round(sum(i["price"] for i in all_items), 2)
    total_all_shipping = round(sum(m["shipping_total"] for m in month_stats), 2)
    total_all_spend = round(total_all_comics + total_all_shipping, 2)
    avg_per_month = round(total_all_spend / months_with_data, 2)
    avg_per_issue = round(total_all_comics / total_issues, 2) if total_issues else 0.0
    shipping_ratio_pct = round((total_all_shipping / total_all_comics) * 100, 1) if total_all_comics else 0.0

    preorder_count = sum(1 for i in all_items if i["status"] == "preorder")
    released_count = total_issues - preorder_count
    preorder_pct = round((preorder_count / total_issues) * 100, 1) if total_issues else 0.0
    released_pct = round(100 - preorder_pct, 1) if total_issues else 0.0
    preorder_ring_svg = render_two_segment_ring_svg(
        preorder_pct, "var(--neon-blue)", released_pct, "var(--neon-violet)",
        preorder_count + released_count, "issues",
    )

    # Semi-circle gauge split point, for the pre-order/released gauge shape
    # matching the approved design (a half-circle, not the full ring above -
    # preorder_ring_svg is kept for anywhere a full ring is still wanted).
    _gauge_cx, _gauge_cy, _gauge_r = 88, 92, 74
    _gauge_frac = (preorder_pct / 100) if total_issues else 0.5
    _gauge_theta = math.radians(180 * (1 - _gauge_frac))
    preorder_gauge_split_x = round(_gauge_cx + _gauge_r * math.cos(_gauge_theta), 1)
    preorder_gauge_split_y = round(_gauge_cy - _gauge_r * math.sin(_gauge_theta), 1)
    preorder_gauge_has_preorder = preorder_count > 0
    preorder_gauge_has_released = released_count > 0

    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) <= date(?)
        ORDER BY release_date DESC, name ASC LIMIT 10
        """,
        (date.today().isoformat(),),
    )
    recent_releases = [dict(r) for r in cur.fetchall()]
    for r in recent_releases:
        r["release_date_label"] = date.fromisoformat(r["release_date"]).strftime("%d %b")

    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) <= date(?)
        ORDER BY price DESC, name ASC LIMIT 10
        """,
        (date.today().isoformat(),),
    )
    most_expensive_releases = [dict(r) for r in cur.fetchall()]
    for r in most_expensive_releases:
        r["release_date_label"] = date.fromisoformat(r["release_date"]).strftime("%d %b")

    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) <= date(?)
        ORDER BY price ASC, name ASC LIMIT 10
        """,
        (date.today().isoformat(),),
    )
    lowest_releases = [dict(r) for r in cur.fetchall()]
    for r in lowest_releases:
        r["release_date_label"] = date.fromisoformat(r["release_date"]).strftime("%d %b")


    # Trend badge: how the last fully-completed month compares to the
    # all-time monthly average. The current month is deliberately excluded
    # from this comparison since it's always partial (still accumulating),
    # which would make every month look artificially "down" against the
    # average purely from being incomplete rather than genuinely cheaper.
    month_trend = None
    if len(twelve_month_data) >= 2 and avg_per_month:
        last_complete = twelve_month_data[-2]
        if last_complete["total"] > 0:
            diff_pct = round(((last_complete["total"] - avg_per_month) / avg_per_month) * 100)
            if diff_pct != 0:
                month_trend = {
                    "pct": abs(diff_pct),
                    "above_average": diff_pct > 0,
                    "label": last_complete["label"],
                }

    # Next 2 upcoming releases - a genuinely different kind of info than
    # anything else on this page (what's coming up, not another spend
    # figure), capped at 2 so it doesn't turn into a duplicate of the
    # Dashboard's own "This Week" list.
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) >= date(?)
        ORDER BY release_date ASC, name ASC LIMIT 2
        """,
        (date.today().isoformat(),),
    )
    upcoming_releases = [dict(r) for r in cur.fetchall()]
    for r in upcoming_releases:
        days_until = (date.fromisoformat(r["release_date"]) - date.today()).days
        r["days_until_label"] = "Today" if days_until == 0 else ("Tomorrow" if days_until == 1 else f"{days_until} days")

    avg_issue_bar_pct = (
        round((avg_per_issue / priciest_item["price"]) * 100, 1)
        if priciest_item and priciest_item["price"] else 0.0
    )

    # Comics vs shipping split of all-time spend - reuses totals already
    # computed above, no new aggregation.
    comics_share_pct = round((total_all_comics / total_all_spend) * 100, 1) if total_all_spend else 0.0
    shipping_share_pct = round(100 - comics_share_pct, 1) if total_all_spend else 0.0

    # This month vs budget - a simpler, month-only comparison than the
    # Dashboard's own budget ring (which also handles rollover/cycles);
    # this just answers "how does the current month look against the
    # plain monthly figure", reusing the current month's total from the
    # 12-month data built above rather than re-querying.
    monthly_budget_raw = notifications.get_setting(cur, "monthly_budget", "")
    monthly_budget = None
    if monthly_budget_raw:
        try:
            monthly_budget = float(monthly_budget_raw)
        except ValueError:
            monthly_budget = None
    current_month_spend = twelve_month_data[-1]["total"] if twelve_month_data else 0.0
    budget_vs_month_pct = (
        round((current_month_spend / monthly_budget) * 100) if monthly_budget else None
    )
    budget_vs_month_bar_pct = min(budget_vs_month_pct, 100) if budget_vs_month_pct is not None else None

    cur.execute("SELECT price FROM items WHERE status = 'cancelled'")
    cancelled_saved = round(sum(r["price"] for r in cur.fetchall()), 2)

    # Next month forecast - same calculation the dashboard hero uses, just
    # surfaced here too as its own stat card.
    today = date.today()
    nm_start = shift_month(today.replace(day=1), 1)
    nm_end = nm_start.replace(day=calendar.monthrange(nm_start.year, nm_start.month)[1])
    next_month_items = fetch_items_between(cur, nm_start, nm_end)
    next_month_groups = group_by_date(next_month_items)
    next_month_comics_total = round(sum(i["price"] for i in next_month_items), 2)
    next_month_shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, next_month_groups)
    next_month_forecast = round(next_month_comics_total + next_month_shipping_total, 2)
    next_month_forecast_label = nm_start.strftime("%B %Y")

    avg_shipping_per_month = round(total_all_shipping / months_with_data, 2)

    # Busiest release day of the week - which weekday has the most issues
    # released on it, across everything dated.
    weekday_counts = {}
    for it in dated_items:
        wd = date.fromisoformat(it["release_date"]).strftime("%A")
        weekday_counts[wd] = weekday_counts.get(wd, 0) + 1
    busiest_weekday = max(weekday_counts, key=weekday_counts.get) if weekday_counts else None
    busiest_weekday_count = weekday_counts.get(busiest_weekday, 0) if busiest_weekday else 0

    # Same weekday_counts, reshaped into a Mon-Sun chart: each day's count
    # normalized against whichever day is busiest, so the chart always has
    # one full-height bar rather than being scaled to some arbitrary max.
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    max_weekday_count = max(weekday_counts.values()) if weekday_counts else 0
    weekday_chart = [
        {
            "label": day[:3],
            "count": weekday_counts.get(day, 0),
            "bar_pct": round((weekday_counts.get(day, 0) / max_weekday_count) * 100) if max_weekday_count else 0,
            "is_busiest": day == busiest_weekday,
        }
        for day in weekday_order
    ]

    # Same weekday breakdown, but split per shop so each day's bar can show
    # a stacked segment per shop, with a toggle to show/hide each one.
    weekday_shop_counts = {day: {} for day in weekday_order}
    for it in dated_items:
        wd = date.fromisoformat(it["release_date"]).strftime("%A")
        shop = source_tab_group(it["source"])
        weekday_shop_counts[wd][shop] = weekday_shop_counts[wd].get(shop, 0) + 1
    weekday_shops = sorted({source_tab_group(it["source"]) for it in dated_items})
    weekday_shop_colors = {s: source_color(s) for s in weekday_shops}
    weekday_chart_by_shop = []
    for day in weekday_order:
        day_total = weekday_counts.get(day, 0)
        segments = []
        for shop in weekday_shops:
            cnt = weekday_shop_counts[day].get(shop, 0)
            if cnt:
                segments.append({"shop": shop, "count": cnt, "pct_of_day": round(cnt / day_total * 100, 1) if day_total else 0})
        weekday_chart_by_shop.append({
            "label": day[:3],
            "bar_pct": round((day_total / max_weekday_count) * 100) if max_weekday_count else 0,
            "is_busiest": day == busiest_weekday,
            "segments": segments,
        })

    # Biggest and cheapest single shipping charge ever actually captured -
    # real per-shipment amounts, not an estimate.
    cur.execute("SELECT amount, source FROM shipment_postage ORDER BY amount DESC LIMIT 1")
    biggest_shipping_row = cur.fetchone()
    biggest_shipping = dict(biggest_shipping_row) if biggest_shipping_row else None
    cur.execute("SELECT amount, source FROM shipment_postage ORDER BY amount ASC LIMIT 1")
    cheapest_shipping_row = cur.fetchone()
    cheapest_shipping = dict(cheapest_shipping_row) if cheapest_shipping_row else None

    by_shop = {}
    for it in all_items:
        by_shop.setdefault(it["source"], []).append(it)
    raw_shop_stats = []
    for source, items in by_shop.items():
        comics_total = round(sum(i["price"] for i in items), 2)
        dated_shop_items = [i for i in items if i["release_date"]]
        groups = group_by_date(dated_shop_items)
        shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, groups)
        raw_shop_stats.append({
            "source": source,
            "color": source_color(source),
            "total": round(comics_total + shipping_total, 2),
            "count": len(items),
        })

    # Group per-seller eBay entries into one row (a list with hundreds of
    # eBay sellers would be unreadable), keeping the individual sellers
    # available as an expandable sub-list rather than losing that detail.
    grouped = {}
    for s in raw_shop_stats:
        group_name = source_tab_group(s["source"])
        if group_name not in grouped:
            grouped[group_name] = {
                "source": group_name,
                "color": source_color(group_name),
                "total": 0.0,
                "count": 0,
                "sub_shops": [],
            }
        grouped[group_name]["total"] += s["total"]
        grouped[group_name]["count"] += s["count"]
        if group_name != s["source"]:
            grouped[group_name]["sub_shops"].append(s)

    shop_stats = list(grouped.values())
    for s in shop_stats:
        s["total"] = round(s["total"], 2)
        s["sub_shops"].sort(key=lambda x: -x["total"])
    shop_stats.sort(key=lambda s: -s["total"])
    max_shop_total = max((s["total"] for s in shop_stats), default=0) or 1
    for s in shop_stats:
        s["pct"] = round((s["total"] / max_shop_total) * 100, 1)
        for sub in s["sub_shops"]:
            sub["pct"] = round((sub["total"] / max_shop_total) * 100, 1)

    # Real 90-day trend per shop group - current 90 days' spend vs the
    # 90 days before that, so both "fastest growing" and the comparison
    # table's trend arrows are backed by the same real comparison rather
    # than two separate ad-hoc calculations.
    today_ins = date.today()
    window_start = today_ins - timedelta(days=90)
    prev_window_start = today_ins - timedelta(days=180)
    shop_90d_total = {}
    shop_prev90d_total = {}
    for it in dated_items:
        rd = date.fromisoformat(it["release_date"])
        group_name = source_tab_group(it["source"])
        if window_start <= rd <= today_ins:
            shop_90d_total[group_name] = shop_90d_total.get(group_name, 0) + it["price"]
        elif prev_window_start <= rd < window_start:
            shop_prev90d_total[group_name] = shop_prev90d_total.get(group_name, 0) + it["price"]
    for s in shop_stats:
        cur_90 = round(shop_90d_total.get(s["source"], 0), 2)
        prev_90 = round(shop_prev90d_total.get(s["source"], 0), 2)
        s["last_90d_total"] = cur_90
        if prev_90 > 0:
            s["trend_pct"] = round(((cur_90 - prev_90) / prev_90) * 100)
            s["trend_up"] = cur_90 >= prev_90
        elif cur_90 > 0:
            s["trend_pct"] = None
            s["trend_up"] = True
        else:
            s["trend_pct"] = None
            s["trend_up"] = None
        avg_shipping_shop_items = [i for i in dated_items if source_tab_group(i["source"]) == s["source"] and i["release_date"]]
        if avg_shipping_shop_items:
            shop_groups = group_by_date(avg_shipping_shop_items)
            shop_shipping_total, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, shop_groups)
            s["avg_shipping"] = round(shop_shipping_total / s["count"], 2) if s["count"] else 0
        else:
            s["avg_shipping"] = 0

    shop_90d_grand_total = round(sum(shop_90d_total.values()), 2)
    _shop_all_time_grand_total = round(sum(s["total"] for s in shop_stats), 2)
    donut_data = {
        "all": {
            "total": _shop_all_time_grand_total,
            "shops": [{"source": s["source"], "value": s["total"], "pct": round((s["total"] / max(_shop_all_time_grand_total, 1)) * 100, 1)} for s in shop_stats[:6]],
        },
        "90": {
            "total": shop_90d_grand_total,
            "shops": [{"source": s["source"], "value": round(shop_90d_total.get(s["source"], 0), 2), "pct": round((shop_90d_total.get(s["source"], 0) / shop_90d_grand_total) * 100, 1) if shop_90d_grand_total else 0} for s in shop_stats[:6]],
        },
    }
    donut_data_json = json.dumps(donut_data)

    shop_total_all_time = round(sum(s["total"] for s in shop_stats), 2)
    shop_total_issues = sum(s["count"] for s in shop_stats)
    top_shop = shop_stats[0] if shop_stats else None
    top_shop_pct_of_total = round((top_shop["total"] / shop_total_all_time) * 100, 1) if top_shop and shop_total_all_time else 0
    fastest_growing_shop = max(
        (s for s in shop_stats if s["trend_pct"] is not None and s["trend_up"]),
        key=lambda s: s["trend_pct"],
        default=None,
    )
    avg_per_shop = round(shop_total_all_time / len(shop_stats), 2) if shop_stats else 0

    # Spend by shop, over the last 12 months - same top-level shop grouping
    # as shop_stats above (eBay sellers folded together), one total per
    # shop per month, for the "spend by shop over time" multi-line chart.
    shop_over_time_start = today.replace(day=1) - timedelta(days=365)
    shop_over_time_start = shop_over_time_start.replace(day=1)
    cur.execute(
        """
        SELECT strftime('%Y-%m', COALESCE(release_date, placed_date)) AS ym, source, SUM(price) AS total
        FROM items
        WHERE status != 'cancelled'
          AND COALESCE(release_date, placed_date) IS NOT NULL
          AND date(COALESCE(release_date, placed_date)) >= date(?)
        GROUP BY ym, source
        """,
        (shop_over_time_start.isoformat(),),
    )
    monthly_shop_raw = {}
    for r in cur.fetchall():
        group_name = source_tab_group(r["source"])
        monthly_shop_raw.setdefault(r["ym"], {})
        monthly_shop_raw[r["ym"]][group_name] = monthly_shop_raw[r["ym"]].get(group_name, 0) + r["total"]

    month_keys = []
    cursor_month = shop_over_time_start
    while cursor_month <= today.replace(day=1):
        month_keys.append(cursor_month.strftime("%Y-%m"))
        cursor_month = (cursor_month.replace(day=28) + timedelta(days=4)).replace(day=1)

    top_shop_names = [s["source"] for s in shop_stats[:5]]
    shop_over_time = {
        "months": [datetime.strptime(m, "%Y-%m").strftime("%b") for m in month_keys],
        "series": [
            {
                "name": name,
                "color": source_color(name),
                "data": [round(monthly_shop_raw.get(m, {}).get(name, 0), 2) for m in month_keys],
            }
            for name in top_shop_names
        ],
    }
    shop_over_time_json = json.dumps(shop_over_time)

    # Cumulative spend curve: reuses the exact same chart component as
    # the 12-month trend above (gradient glow line, permanent dots, the
    # existing hover tooltip) rather than a separate hand-rolled mini
    # chart - same real interactivity, same polish, just fed day-by-day
    # data for the current month instead of month-by-month for the
    # year. Comics/shipping/count are tracked as running cumulative
    # sums, not each day's own figure, so hovering any point reads as
    # "as of this day" - matching what the line itself is showing.
    today = date.today()
    month_start = today.replace(day=1)
    month_items = [i for i in dated_items if i["release_date"][:7] == today.strftime("%Y-%m")]
    month_groups = group_by_date(month_items)
    daily = {}
    for group in month_groups:
        day_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, [group])
        daily[group["date"]] = {
            "comics": group["subtotal"],
            "shipping": day_shipping,
            "count": sum(len(sg["entries"]) for sg in group["source_groups"]),
        }

    days_so_far = (today - month_start).days + 1
    label_every = 5 if days_so_far > 15 else (2 if days_so_far > 7 else 1)
    cumulative_curve = []
    running_comics = running_shipping = 0.0
    running_count = 0
    for offset in range(days_so_far):
        day = month_start + timedelta(days=offset)
        iso = day.isoformat()
        d = daily.get(iso)
        if d:
            running_comics += d["comics"]
            running_shipping += d["shipping"]
            running_count += d["count"]
        is_last = offset == days_so_far - 1
        show_label = offset == 0 or is_last or (offset + 1) % label_every == 0
        cumulative_curve.append({
            "label": str(day.day) if show_label else "",
            "total": round(running_comics + running_shipping, 2),
            "comics_total": round(running_comics, 2),
            "shipping_total": round(running_shipping, 2),
            "count": running_count,
            "is_current": is_last,
        })
    cumulative_svg = render_trend_svg(cumulative_curve, "cumulative", style="step") if len(cumulative_curve) > 1 else ""
    cumulative_month_label = today.strftime("%B")

    # Weekly-sampled version of the same cumulative curve - same running
    # totals, just checkpointed at the end of each week instead of every
    # day, so the step chart has room to breathe on a narrow screen
    # rather than cramming ~20-30 daily steps into one card.
    cumulative_curve_weekly = []
    for offset in range(days_so_far):
        day = month_start + timedelta(days=offset)
        week_of_month = (offset // 7) + 1
        is_last_of_week = (offset % 7 == 6) or (offset == days_so_far - 1)
        if is_last_of_week:
            entry = cumulative_curve[offset]
            cumulative_curve_weekly.append({
                "label": f"Wk {week_of_month}",
                "total": entry["total"],
                "comics_total": entry["comics_total"],
                "shipping_total": entry["shipping_total"],
                "count": entry["count"],
                "is_current": entry["is_current"],
            })
    cumulative_svg_weekly = render_trend_svg(cumulative_curve_weekly, "cumulative-weekly", style="step", label_font_size=22) if len(cumulative_curve_weekly) > 1 else ""

    # v2 Overview "Cumulative spend" card (budget bar + release-day bars).
    # Every release day of the whole month, including ones still to come,
    # so the card can show "spent so far" and "still due". The budget is
    # the same figure the sidebar shows (rollover included), and only when
    # the budget cycle is monthly - a weekly/28-day budget doesn't line up
    # with a calendar-month card.
    cm_days = {}
    for group in month_groups:
        day_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, [group])
        cm_days[group["date"]] = {
            "items": round(group["subtotal"], 2),
            "ship": round(day_shipping, 2),
            "count": sum(len(sg["entries"]) for sg in group["source_groups"]),
            "names": [e["name"] for sg in group["source_groups"] for e in sg["entries"]],
        }
    cm_budget = None
    if notifications.get_setting(cur, "budget_cycle", "monthly") == "monthly":
        _bs = _topbar_budget_status()
        if _bs:
            cm_budget = _bs["monthly_budget"]
    cumulative_month_json = json.dumps({
        "ym": today.strftime("%Y-%m"),
        "month_name": today.strftime("%B"),
        "days_in_month": calendar.monthrange(today.year, today.month)[1],
        "today": today.day,
        "days": cm_days,
        "budget": cm_budget,
    })

    # Price distribution: fixed £10 buckets up to £50, then a single
    # £50+ catch-all - fixed rather than dynamically sized so the shape
    # reads consistently release to release rather than the bucket
    # boundaries themselves shifting around.
    price_buckets = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]
    bucket_counts = [0] * (len(price_buckets) + 1)
    for it in all_items:
        placed = False
        for idx, (low, high) in enumerate(price_buckets):
            if low <= it["price"] < high:
                bucket_counts[idx] += 1
                placed = True
                break
        if not placed:
            bucket_counts[-1] += 1
    price_distribution = [
        {"label": f"£{low}-{high}", "count": bucket_counts[idx]}
        for idx, (low, high) in enumerate(price_buckets)
    ]
    price_distribution.append({"label": "£50+", "count": bucket_counts[-1]})
    max_bucket_count = max((b["count"] for b in price_distribution), default=0) or 1

    conn.close()
    return {
        "request": request,
        "top_month": top_month,
        "priciest_item": priciest_item,
        "shop_stats": shop_stats,
        "donut_data_json": donut_data_json,
        "shop_total_all_time": shop_total_all_time,
        "shop_total_issues": shop_total_issues,
        "top_shop": top_shop,
        "top_shop_pct_of_total": top_shop_pct_of_total,
        "fastest_growing_shop": fastest_growing_shop,
        "avg_per_shop": avg_per_shop,
        "shop_over_time_json": shop_over_time_json,
        "cumulative_svg": cumulative_svg,
        "cumulative_svg_weekly": cumulative_svg_weekly,
        "cumulative_month_label": cumulative_month_label,
        "cumulative_month_json": cumulative_month_json,
        "price_distribution": price_distribution,
        "max_bucket_count": max_bucket_count,
        "has_data": bool(all_items),
        "total_issues": total_issues,
        "twelve_month_svg": twelve_month_svg,
        "top_titles": top_titles,
        "price_creep": price_creep,
        "price_creep_all": price_creep_all,
        "all_series_stats": all_series_stats,
        "creep_series_tracked": creep_series_tracked,
        "creep_avg_increase": creep_avg_increase,
        "creep_biggest_jumper": creep_biggest_jumper,
        "creep_buckets": creep_buckets,
        "creep_total_extra": creep_total_extra,
        "creep_cumulative_json": creep_cumulative_json,
        "series_totals": series_totals,
        "all_series_totals": all_series_totals,
        "most_collected_series": most_collected_series,
        "longest_running_series": longest_running_series,
        "top_series_by_spend": top_series_by_spend,
        "avg_per_month": avg_per_month,
        "avg_per_issue": avg_per_issue,
        "preorder_count": preorder_count,
        "released_count": released_count,
        "preorder_pct": preorder_pct,
        "released_pct": released_pct,
        "preorder_ring_svg": preorder_ring_svg,
        "preorder_gauge_split_x": preorder_gauge_split_x,
        "preorder_gauge_split_y": preorder_gauge_split_y,
        "preorder_gauge_has_preorder": preorder_gauge_has_preorder,
        "preorder_gauge_has_released": preorder_gauge_has_released,
        "recent_releases": recent_releases,
        "most_expensive_releases": most_expensive_releases,
        "lowest_releases": lowest_releases,
        "month_trend": month_trend,
        "upcoming_releases": upcoming_releases,
        "avg_issue_bar_pct": avg_issue_bar_pct,
        "comics_share_pct": comics_share_pct,
        "shipping_share_pct": shipping_share_pct,
        "monthly_budget": monthly_budget,
        "current_month_spend": current_month_spend,
        "budget_vs_month_pct": budget_vs_month_pct,
        "budget_vs_month_bar_pct": budget_vs_month_bar_pct,
        "weekday_chart": weekday_chart,
        "weekday_chart_by_shop": weekday_chart_by_shop,
        "weekday_shops": weekday_shops,
        "weekday_shop_colors": weekday_shop_colors,
        "cancelled_saved": cancelled_saved,
        "shipping_ratio_pct": shipping_ratio_pct,
        "total_all_spend": total_all_spend,
        "total_all_comics": total_all_comics,
        "spend_by_category_all": spend_by_category_all,
        "spend_by_category_year": spend_by_category_year,
        "total_all_shipping": total_all_shipping,
        "next_month_forecast": next_month_forecast,
        "next_month_forecast_label": next_month_forecast_label,
        "avg_shipping_per_month": avg_shipping_per_month,
        "busiest_weekday": busiest_weekday,
        "busiest_weekday_count": busiest_weekday_count,
        "biggest_shipping": biggest_shipping,
        "cheapest_shipping": cheapest_shipping,
    }


@app.get("/insights")
def insights_overview_v2(request: Request):
    ctx = _build_insights_context(request)
    return templates.TemplateResponse("v2/insights_overview.html", ctx)


@app.get("/insights/spend-by-shop")
def insights_shop_v2(request: Request):
    ctx = _build_insights_context(request)
    return templates.TemplateResponse("v2/insights_shop.html", ctx)


def _cover_price_creep(ctx):
    """v2 Price creep, based on COVER price: the cheapest copy bought of
    each issue number stands in for that issue's standard cover, and the
    trend runs first issue -> latest issue. Variants are counted
    separately (how much extra they cost on average) instead of making
    the trend spike. Returns overrides for the page's context; the old UI
    keeps its original first-copy/latest-copy figures untouched."""
    series_rows = []
    for st in ctx["all_series_stats"]:
        by_issue = {}
        for e in st["entries"]:
            m = re.match(r"^.*?#(\d+)", e["name"])
            if not m:
                continue
            by_issue.setdefault(int(m.group(1)), []).append(e)
        if len(by_issue) < 2:
            continue  # one issue (just variants of it) has no trend
        issues = []
        premiums = []
        for num in sorted(by_issue):
            copies = sorted(by_issue[num], key=lambda e: e["price"])
            cover = copies[0]["price"]
            issues.append({"issue": num, "cover": cover, "copies": copies})
            # A second copy at the cover price is a duplicate, not a variant
            premiums.extend(c["price"] - cover for c in copies[1:] if c["price"] - cover > 0.005)
        first, latest = issues[0]["cover"], issues[-1]["cover"]
        series_rows.append({
            "series": st["series"],
            "issue_count": len(issues),
            "first_price": first,
            "latest_price": latest,
            "change_pct": round((latest - first) / first * 100) if first else 0,
            "variant_count": len(premiums),
            "variant_premium": round(sum(premiums) / len(premiums), 2) if premiums else 0,
            "issues": issues,
        })
    max_premium = max((r["variant_premium"] for r in series_rows), default=0) or 1
    for r in series_rows:
        r["premium_bar_pct"] = round(r["variant_premium"] / max_premium * 100) if r["variant_count"] else 0

    tracked = len(series_rows)
    increased = [r for r in series_rows if r["latest_price"] > r["first_price"] + 0.01]
    buckets = [dict(b, count=0) for b in ctx["creep_buckets"]]
    for r in increased:
        for b in buckets:
            if b["min"] <= r["change_pct"] < b["max"]:
                b["count"] += 1
                break
    bmax = max((b["count"] for b in buckets), default=0) or 1
    for b in buckets:
        b["bar_pct"] = round(b["count"] / bmax * 100)

    # Extra spend: every copy of an issue whose cover price had risen above
    # the series' first cover price counts that rise (not a variant's own
    # mark-up) toward the month it released in.
    month_extra = {}
    total_extra = 0.0
    for r in increased:
        for iss in r["issues"]:
            rise = iss["cover"] - r["first_price"]
            if rise <= 0.01:
                continue
            for c in iss["copies"]:
                if c["release_date"]:
                    mk = c["release_date"][:7]
                    month_extra[mk] = month_extra.get(mk, 0) + rise
                    total_extra += rise
    cumulative = []
    running = 0.0
    for pt in json.loads(ctx["creep_cumulative_json"]):
        running += month_extra.get(pt["month_key"], 0)
        cumulative.append(dict(pt, value=round(running, 2)))

    return {
        "all_series_stats": series_rows,
        "creep_series_tracked": tracked,
        "creep_avg_increase": round(sum(r["change_pct"] for r in series_rows) / tracked, 1) if tracked else 0,
        "creep_biggest_jumper": max(series_rows, key=lambda r: r["change_pct"], default=None),
        "creep_buckets": buckets,
        "creep_total_extra": round(total_extra, 2),
        "creep_cumulative_json": json.dumps(cumulative),
    }


def _insights_category_ctx(request: Request):
    cats_param = request.query_params.get("cats")
    chips, selected = _insights_category_chips(cats_param)
    ctx = _build_insights_context(request, category_ids=selected)
    ctx["category_chips"] = chips
    ctx["category_none_on"] = bool(chips) and not selected
    ctx["category_chips_json"] = json.dumps({
        "chips": [{"id": c["id"], "default": c["is_default"]} for c in chips],
        "selected": sorted(selected),
        "explicit": cats_param is not None,
    })
    return ctx


@app.get("/insights/price-creep")
def insights_price_creep_v2(request: Request):
    ctx = _insights_category_ctx(request)
    ctx.update(_cover_price_creep(ctx))
    return templates.TemplateResponse("v2/insights_pricecreep.html", ctx)


@app.get("/insights/top-titles")
def insights_top_titles_v2(request: Request):
    ctx = _insights_category_ctx(request)
    return templates.TemplateResponse("v2/insights_toptitles.html", ctx)


@app.get("/classic/search")
def search_items(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    has_tracking: str | None = None,
    page: int = 1,
):
    ctx = _build_search_context(request, q, source, status, start_date, end_date, sort, min_price, max_price, has_tracking, page)
    return templates.TemplateResponse("search.html", ctx)


def _build_search_context(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    has_tracking: str | None = None,
    page: int = 1,
):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_filter_tab_sources(cur)

    active_status = status or "all"
    active_sort = sort if sort in SEARCH_SORT_OPTIONS else "date_desc"

    min_price_val = None
    if min_price and min_price.strip():
        try:
            min_price_val = float(min_price)
        except ValueError:
            min_price_val = None
    max_price_val = None
    if max_price and max_price.strip():
        try:
            max_price_val = float(max_price)
        except ValueError:
            max_price_val = None

    active_date_preset = None
    active_date_preset_label = None
    if start_date and end_date:
        today = date.today()
        month_start, month_end = month_bounds(today)
        year_start, year_end = date(today.year, 1, 1), date(today.year, 12, 31)
        if start_date == month_start.isoformat() and end_date == month_end.isoformat():
            active_date_preset = "month"
            active_date_preset_label = today.strftime("%B %Y")
        elif start_date == year_start.isoformat() and end_date == year_end.isoformat():
            active_date_preset = "year"
            active_date_preset_label = str(today.year)

    has_filter = bool(
        (q and q.strip()) or source or (status and status != "all") or start_date or end_date
        or min_price_val is not None or max_price_val is not None or has_tracking
    )

    results = []
    spent = remaining = cancelled_total = 0.0
    cancelled_count = 0
    match_count = 0
    total_pages = 1
    current_page = 1

    if has_filter:
        where_clause, params = build_search_query(
            q, source, active_status, start_date, end_date, min_price_val, max_price_val, has_tracking
        )
        order_sql = SEARCH_SORT_OPTIONS[active_sort][0]

        cur.execute(f"SELECT * FROM items WHERE {where_clause} ORDER BY {order_sql}", params)
        all_matches = [dict(r) for r in cur.fetchall()]

        match_count = len(all_matches)
        spent = round(sum(r["price"] for r in all_matches if r["charge_status"] == "charged" and r["status"] != "cancelled"), 2)
        remaining = round(sum(r["price"] for r in all_matches if r["charge_status"] != "charged" and r["status"] != "cancelled"), 2)
        cancelled_total = round(sum(r["price"] for r in all_matches if r["status"] == "cancelled"), 2)
        cancelled_count = sum(1 for r in all_matches if r["status"] == "cancelled")

        total_pages = max(1, math.ceil(match_count / SEARCH_PER_PAGE))
        current_page = max(1, min(page or 1, total_pages))
        start_idx = (current_page - 1) * SEARCH_PER_PAGE
        results = all_matches[start_idx:start_idx + SEARCH_PER_PAGE]
        for r in results:
            r["release_date_label"] = (
                date.fromisoformat(r["release_date"]).strftime("%d %b %Y") if r["release_date"] else "no date set"
            )
            r["source_color"] = source_color(r["source"])

    conn.close()
    return {
        "request": request,
        "q": q or "",
        "results": results,
        "has_filter": has_filter,
        "all_sources": all_sources,
        "active_source": source or "",
        "active_status": active_status,
        "active_has_tracking": has_tracking or "",
        "start_date": start_date or "",
        "end_date": end_date or "",
        "min_price": min_price or "",
        "max_price": max_price or "",
        "active_sort": active_sort,
        "active_date_preset": active_date_preset,
        "active_date_preset_label": active_date_preset_label,
        "sort_options": SEARCH_SORT_OPTIONS,
        "match_count": match_count,
        "spent": spent,
        "remaining": remaining,
        "cancelled_total": cancelled_total,
        "cancelled_count": cancelled_count,
        "total_pages": total_pages,
        "current_page": current_page,
        "per_page": SEARCH_PER_PAGE,
    }


@app.get("/search")
def search_v2(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    has_tracking: str | None = None,
    page: int = 1,
):
    ctx = _build_search_context(request, q, source, status, start_date, end_date, sort, min_price, max_price, has_tracking, page)
    return templates.TemplateResponse("v2/search.html", ctx)


@app.get("/search/export.csv")
def export_search_csv(
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
    min_price: str | None = None,
    max_price: str | None = None,
    has_tracking: str | None = None,
):
    """Downloads whatever's currently filtered on the search page as a CSV
    file - reuses the exact same query-building logic, so the export always
    matches what's on screen."""
    active_status = status or "all"
    active_sort = sort if sort in SEARCH_SORT_OPTIONS else "date_desc"

    min_price_val = None
    if min_price and min_price.strip():
        try:
            min_price_val = float(min_price)
        except ValueError:
            min_price_val = None
    max_price_val = None
    if max_price and max_price.strip():
        try:
            max_price_val = float(max_price)
        except ValueError:
            max_price_val = None

    conn = db.get_db()
    cur = conn.cursor()
    where_clause, params = build_search_query(
        q, source, active_status, start_date, end_date, min_price_val, max_price_val, has_tracking
    )
    order_sql = SEARCH_SORT_OPTIONS[active_sort][0]
    cur.execute(f"SELECT * FROM items WHERE {where_clause} ORDER BY {order_sql}", params)
    rows = [dict(r) for r in cur.fetchall()]
    category_names = _sync_category_names(cur)
    conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Price", "Release Date", "Shop", "Status", "Paid", "Order Number", "Category"])
    for r in rows:
        writer.writerow([
            _csv_safe(r["name"]),
            f"{r['price']:.2f}",
            r["release_date"] or "",
            _csv_safe(r["source"]),
            r["status"],
            "Yes" if r["charge_status"] == "charged" else "No",
            _csv_safe(r["order_number"]) if r["order_number"] else "",
            _csv_safe(category_names.get(r.get("category_id")) or category_names.get(None) or ""),
        ])

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=kaching-export-{date.today().isoformat()}.csv"},
    )


# --- Calendar -----------------------------------------------------------------

@app.get("/classic/calendar")
def calendar_view(request: Request, month: str | None = None, source: str | None = None):
    ctx = _build_calendar_context(request, month, source)
    return templates.TemplateResponse("calendar.html", ctx)


def _build_calendar_context(request: Request, month: str | None = None, source: str | None = None):
    today = date.today()
    if month:
        try:
            viewed_month = datetime.strptime(month, "%Y-%m").date().replace(day=1)
        except ValueError:
            viewed_month = today.replace(day=1)
    else:
        viewed_month = today.replace(day=1)

    v_start, v_end = month_bounds(viewed_month)
    conn = db.get_db()
    cur = conn.cursor()
    items = fetch_items_between(cur, v_start, v_end, source)
    filter_tab_sources = get_filter_tab_sources(cur)

    by_date = {}
    for it in items:
        by_date.setdefault(it["release_date"], []).append(it)

    # Real per-day spend (comics + shipping, matching every other total
    # figure in the app) - grouped the same way the agenda list below
    # already groups by date, so a day showing "one parcel of £54.88"
    # in the agenda and this same day's heatmap intensity are computed
    # from the exact same real shipping figures, not two different
    # notions of "total" drifting apart from each other.
    day_groups = group_by_date(items)
    day_totals = {}
    for group in day_groups:
        day_shipping, _, _, _, _, _, _, _, _ = compute_shipping_for_groups(cur, [group])
        day_totals[group["date"]] = round(group["subtotal"] + day_shipping, 2)
    max_day_total = max(day_totals.values(), default=0) or 1

    # Spend-by-shop legend (this viewed month) - same items already fetched
    # above, just totalled per source rather than per day.
    shop_month_totals = {}
    for it in items:
        shop_month_totals[it["source"]] = shop_month_totals.get(it["source"], 0) + it["price"]
    month_shop_total = sum(shop_month_totals.values()) or 1
    shop_legend = sorted(
        [
            {
                "source": src,
                "color": source_color(src),
                "total": round(amt, 2),
                "pct": round((amt / month_shop_total) * 100, 1),
            }
            for src, amt in shop_month_totals.items()
        ],
        key=lambda s: -s["total"],
    )[:5]

    # Year activity heatmap - real daily totals (comics only, no per-day
    # shipping attribution - matches "each square is a day, darker means
    # more tracked spend" at a glance, not a precise accounting figure)
    # for the past 365 days, so the front end can render either the
    # 1-month or 6-month zoom from one real dataset rather than two
    # separate queries.
    heatmap_start = today - timedelta(days=364)
    cur.execute(
        """
        SELECT date(COALESCE(release_date, placed_date)) AS d, SUM(price) AS total
        FROM items
        WHERE status != 'cancelled'
          AND COALESCE(release_date, placed_date) IS NOT NULL
          AND date(COALESCE(release_date, placed_date)) BETWEEN date(?) AND date(?)
        GROUP BY d
        """,
        (heatmap_start.isoformat(), today.isoformat()),
    )
    heatmap_daily = {r["d"]: round(r["total"], 2) for r in cur.fetchall()}
    heatmap_daily_json = json.dumps(heatmap_daily)

    conn.close()

    agenda_groups = day_groups
    today_iso = today.isoformat()
    default_open_date = None
    upcoming = [g["date"] for g in agenda_groups if g["date"] >= today_iso]
    if upcoming:
        default_open_date = min(upcoming)
    elif agenda_groups:
        default_open_date = max(g["date"] for g in agenda_groups)

    days_in_month = calendar.monthrange(viewed_month.year, viewed_month.month)[1]
    first_weekday = v_start.weekday()  # Monday = 0

    weeks = []
    week = [None] * first_weekday
    for day_num in range(1, days_in_month + 1):
        d = date(viewed_month.year, viewed_month.month, day_num)
        d_iso = d.isoformat()
        day_items = by_date.get(d_iso, [])
        day_total = day_totals.get(d_iso, 0.0)
        week.append({
            "day": day_num,
            "date_iso": d_iso,
            "is_today": d == today,
            "count": len(day_items),
            "total": day_total,
            "intensity": round(day_total / max_day_total, 2) if day_total else 0,
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        while len(week) < 7:
            week.append(None)
        weeks.append(week)

    prev_month_param = shift_month(viewed_month, -1).strftime("%Y-%m")
    next_month_param = shift_month(viewed_month, 1).strftime("%Y-%m")
    viewed_month_param = viewed_month.strftime("%Y-%m")
    is_current_month = (viewed_month.year == today.year and viewed_month.month == today.month)

    return {
        "request": request,
        "viewed_month_label": viewed_month.strftime("%B %Y"),
        "weeks": weeks,
        "agenda_groups": agenda_groups,
        "prev_month_param": prev_month_param,
        "next_month_param": next_month_param,
        "is_current_month": is_current_month,
        "filter_tab_sources": filter_tab_sources,
        "active_source": source or "",
        "viewed_month_param": viewed_month_param,
        "default_open_date": default_open_date,
        "shop_legend": shop_legend,
        "heatmap_daily_json": heatmap_daily_json,
        "heatmap_start": heatmap_start.isoformat(),
    }


@app.get("/calendar")
def calendar_v2(request: Request, month: str | None = None, source: str | None = None):
    ctx = _build_calendar_context(request, month, source)
    return templates.TemplateResponse("v2/calendar.html", ctx)


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """RFC5545 line folding: no content line may exceed 75 octets."""
    if len(line) <= 75:
        return line
    parts = []
    while len(line) > 75:
        parts.append(line[:75])
        line = " " + line[75:]
    parts.append(line)
    return "\r\n".join(parts)


@app.get("/calendar/export.ics")
def export_ics():
    """Downloadable calendar feed of upcoming (non-cancelled) release dates,
    one event per day grouped like the shipment view - so a subscribing
    calendar app isn't cluttered with a separate entry per variant cover."""
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date IS NOT NULL AND date(release_date) >= date(?)
        ORDER BY release_date
        """,
        (today.isoformat(),),
    )
    items = [dict(r) for r in cur.fetchall()]
    currency_symbol = get_currency_symbol(cur)
    conn.close()

    by_date = {}
    for it in items:
        by_date.setdefault(it["release_date"], []).append(it)

    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Ka-Ching!//Releases//EN", "CALSCALE:GREGORIAN"]

    for release_date_str, day_items in sorted(by_date.items()):
        d_compact = release_date_str.replace("-", "")
        d_next = (date.fromisoformat(release_date_str) + timedelta(days=1)).strftime("%Y%m%d")
        total = round(sum(i["price"] for i in day_items), 2)
        names = [f"{i['name']} ({i['source']})" for i in day_items]
        summary = f"{len(day_items)} item{'s' if len(day_items) != 1 else ''} out ({currency_symbol}{total:.2f})"
        description = "\\n".join(_ics_escape(n) for n in names)
        uid = f"kaching-{release_date_str}@kaching.local"

        lines.append("BEGIN:VEVENT")
        lines.append(_ics_fold(f"UID:{uid}"))
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART;VALUE=DATE:{d_compact}")
        lines.append(f"DTEND;VALUE=DATE:{d_next}")
        lines.append(_ics_fold(f"SUMMARY:{_ics_escape(summary)}"))
        lines.append(_ics_fold(f"DESCRIPTION:{description}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines) + "\r\n"

    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=kaching-releases.ics"},
    )


def check_preview_duplicates(cur, preview_items):
    """For each previewed item, flag whether something with the same name
    already exists under a DIFFERENT order - a likely accidental double
    order, worth a second look before confirming. The same name under the
    SAME order is just a normal re-import refresh and isn't flagged."""
    symbol = get_currency_symbol(cur)
    for it in preview_items:
        cur.execute(
            "SELECT order_number, price, release_date FROM items WHERE name = ? AND status != 'cancelled'",
            (it["name"],),
        )
        existing_rows = [dict(r) for r in cur.fetchall()]
        others = [r for r in existing_rows if str(r["order_number"]) != str(it["order_number"])]
        it["duplicate_flag"] = None
        if others:
            r = others[0]
            it["duplicate_flag"] = f"Already tracked - order #{r['order_number']}, {symbol}{r['price']:.2f}"
    return preview_items


# --- Import -------------------------------------------------------------------

@app.get("/import")
def import_form_redirect():
    # Import now lives on the same page as Add - keep this route working
    # in case of an old bookmark, but send people to the combined page.
    return RedirectResponse(url="/items/new", status_code=307)


@app.post("/import")
def import_preview(request: Request, order_text: str = Form(...), shop_hint: str = Form(""), v2: str = Form(""),
                   category_id: str = Form("")):
    """Parses the pasted text and shows an editable review screen - nothing
    is written to the database until the person confirms it looks right."""
    preview = parser.detect_import(order_text, shop_hint=shop_hint or None)

    conn = db.get_db()
    cur = conn.cursor()
    preview["rows"] = check_preview_duplicates(cur, preview["rows"])
    all_sources = get_all_sources(cur)
    import_category_id = _resolve_category(cur, category_id) or db.default_category_id(conn)
    conn.commit()
    conn.close()

    template_name = "v2/importpreview.html" if v2 else "import_preview.html"
    return templates.TemplateResponse(template_name, {
        "request": request,
        "preview": preview,
        "all_sources": all_sources,
        "import_category_id": import_category_id,
    })


@app.post("/import/confirm")
async def import_confirm(request: Request):
    form = await request.form()
    parser_type = form.get("parser_type", "generic")
    next_url = form.get("next") or "/"

    if parser_type == "release_date_email":
        updates = json.loads(form.get("release_updates_json", "[]"))
        result = parser.apply_release_date_updates(updates)
        logger.info("EMAIL DATE UPDATE: %s", result)
        if result.get("changes"):
            conn = db.get_db()
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('_flash_date_changes', ?)",
                (json.dumps([
                    {"name": c["name"], "old_date": c["old_date"], "new_date": c["new_date"]}
                    for c in result["changes"]
                ]),),
            )
            conn.commit()
            conn.close()
        return RedirectResponse(url=next_url, status_code=303)

    if parser_type == "order_detail_postage":
        samples = json.loads(form.get("postage_samples_json", "[]"))
        count = parser.store_shipment_postage(samples)
        logger.info("ORDER-DETAIL POSTAGE CAPTURED: %s samples", count)
        return RedirectResponse(url=next_url, status_code=303)

    row_count = int(form.get("row_count", "0") or 0)

    def _str_to_date(s):
        return date.fromisoformat(s) if s else None

    kept_items = []
    for i in range(row_count):
        if not form.get(f"keep_{i}"):
            continue
        name = (form.get(f"name_{i}") or "").strip()
        if not name:
            continue
        try:
            price = float(form.get(f"price_{i}") or "0")
        except ValueError:
            continue
        kept_items.append({
            "name": name,
            "price": price,
            "release_date_raw": form.get(f"release_date_{i}") or "",
            "order_number": form.get(f"order_number_{i}") or None,
            "placed_date_raw": form.get(f"placed_date_{i}") or "",
            "status": form.get(f"status_{i}") or "preorder",
            "charge_status": form.get(f"charge_status_{i}") or "not_charged",
            "note": form.get(f"note_{i}") or None,
            "source": (form.get(f"source_{i}") or "").strip(),
            "tracking_number": (form.get(f"tracking_number_{i}") or "").strip() or None,
            "category_id_raw": form.get(f"category_id_{i}"),
        })

    if not kept_items:
        return RedirectResponse(url=(form.get("next") or "/items/new"), status_code=303)

    # Resolve each row's chosen category once, up front, for both paths below.
    _cat_conn = db.get_db()
    _cat_cur = _cat_conn.cursor()
    _fallback_category = db.default_category_id(_cat_conn)
    for it in kept_items:
        it["category_id"] = _resolve_category(_cat_cur, it.pop("category_id_raw")) or _fallback_category
    _cat_conn.commit()
    _cat_conn.close()

    if parser_type == "forbidden_planet":
        order_totals_raw = form.get("order_totals_json", "{}")
        try:
            order_totals = {k: float(v) for k, v in json.loads(order_totals_raw).items()}
        except (ValueError, TypeError):
            order_totals = {}
        store_items = [
            {
                "name": it["name"],
                "order_number": it["order_number"],
                "placed_date": _str_to_date(it["placed_date_raw"]),
                "status": it["status"],
                "release_date": _str_to_date(it["release_date_raw"]),
                "charge_status": it["charge_status"] or None,
                "price": it["price"],
                "note": it["note"],
                "category_id": it["category_id"],
            }
            for it in kept_items
        ]
        result = parser.store_parsed_items(store_items, order_totals)
        logger.info("IMPORT CONFIRM (Forbidden Planet): %s", result)

        postage_samples = json.loads(form.get("postage_samples_json", "[]"))
        if postage_samples:
            saved = parser.store_shipment_postage(postage_samples)
            logger.info("ORDER-DETAIL POSTAGE CAPTURED: %s samples", saved)

        if result.get("date_slippage"):
            conn2 = db.get_db()
            conn2.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('_flash_date_changes', ?)",
                (json.dumps(result["date_slippage"]),),
            )
            conn2.commit()
            conn2.close()
    else:
        order_shipping_raw = form.get("order_shipping_json", "{}")
        try:
            order_shipping_map = {k: float(v) for k, v in json.loads(order_shipping_raw).items()}
        except (ValueError, TypeError):
            order_shipping_map = {}

        today = date.today()
        now = datetime.now(timezone.utc).isoformat()

        conn = db.get_db()
        cur = conn.cursor()
        created = []
        order_sources = {}
        for it in kept_items:
            item_source = it["source"] or "Unknown shop"
            release_iso = it["release_date_raw"] or None
            cur.execute(
                """
                INSERT INTO items
                    (name, order_number, placed_date, status, release_date, charge_status,
                     price, note, imported_at, manual_override, source, tracking_number, uuid, updated_at,
                     category_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    it["name"], it["order_number"], today.isoformat(), it["status"] or "preorder",
                    release_iso, it["charge_status"] or "not_charged", it["price"], it["note"], now, item_source,
                    it["tracking_number"], db.new_uuid(), now, it["category_id"],
                ),
            )
            created.append((cur.lastrowid, it["name"], it["price"], item_source))
            if it["order_number"] and it["order_number"] not in order_sources:
                order_sources[it["order_number"]] = item_source

        # One shipment_postage sample per distinct order among the kept
        # rows, tagged to that order's own shop - covers both a single
        # order and a bulk paste spanning several different orders/sellers.
        for order_number, shipping_val in order_shipping_map.items():
            if order_number not in order_sources:
                continue
            cur.execute(
                """
                INSERT INTO shipment_postage (order_number, shipment_index, amount, captured_at, source)
                VALUES (?, 0, ?, ?, ?)
                ON CONFLICT(order_number, shipment_index) DO UPDATE SET
                    amount = excluded.amount, captured_at = excluded.captured_at, source = excluded.source
                """,
                (order_number, shipping_val, now, order_sources[order_number]),
            )

        conn.commit()
        conn.close()
        logger.info("IMPORT CONFIRM (generic): created=%s shipping=%s", created, order_shipping_map)

    return RedirectResponse(url=next_url, status_code=303)


# --- Settings / notifications -------------------------------------------------

@app.get("/classic/settings")
def settings_form(
    request: Request,
    test_result: str | None = None,
    test_error: str | None = None,
    restore_result: str | None = None,
    restore_count: int | None = None,
    reset_result: str | None = None,
    notif_import_result: str | None = None,
):
    ctx = _build_settings_context(request, test_result, test_error, restore_result, restore_count, reset_result, notif_import_result)
    return templates.TemplateResponse("settings.html", ctx)


def _build_settings_context(
    request: Request,
    test_result: str | None = None,
    test_error: str | None = None,
    restore_result: str | None = None,
    restore_count: int | None = None,
    reset_result: str | None = None,
    notif_import_result: str | None = None,
):
    conn = db.get_db()
    cur = conn.cursor()
    values = notifications.get_all_settings(cur)
    cur.execute("SELECT COUNT(*) AS n FROM items")
    item_count = cur.fetchone()["n"]
    sync_api_key = notifications.get_setting(cur, "sync_api_key", None)
    cur.execute("SELECT client_id, client_label, last_synced_at FROM sync_state ORDER BY last_synced_at DESC")
    synced_devices = cur.fetchall()
    cur.execute("SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY source")
    all_shops = cur.fetchall()
    cur.execute("SELECT * FROM notification_log ORDER BY id DESC LIMIT 20")
    notification_log = cur.fetchall()
    conn.close()

    try:
        db_size_bytes = os.path.getsize(db.DB_PATH)
        db_size_label = f"{db_size_bytes / 1024 / 1024:.2f} MB" if db_size_bytes >= 1024 * 1024 else f"{db_size_bytes / 1024:.1f} KB"
    except OSError:
        db_size_label = "unknown"

    past_backups = []
    backup_dir = os.path.join(os.path.dirname(db.DB_PATH), "backups")
    if os.path.isdir(backup_dir):
        for fname in sorted(os.listdir(backup_dir), reverse=True):
            if not _AUTO_BACKUP_NAME_RE.match(fname):
                continue
            full_path = os.path.join(backup_dir, fname)
            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                continue
            size_label = f"{size_bytes / 1024 / 1024:.2f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes / 1024:.1f} KB"
            # Filename is kaching-auto-YYYYMMDD-HHMMSS.db
            stamp = fname.replace("kaching-auto-", "").replace(".db", "")
            try:
                taken_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%d %b %Y, %H:%M")
            except ValueError:
                taken_at = fname
            past_backups.append({"filename": fname, "size_label": size_label, "taken_at": taken_at})

    return {
        "request": request,
        "values": values,
        "test_result": test_result,
        "test_error": test_error,
        "restore_result": restore_result,
        "restore_count": restore_count,
        "reset_result": reset_result,
        "notif_import_result": notif_import_result,
        "item_count": item_count,
        "db_size_label": db_size_label,
        "sync_api_key": sync_api_key,
        "synced_devices": synced_devices,
        "all_shops": all_shops,
        "notification_log": notification_log,
        "past_backups": past_backups,
    }


@app.get("/settings")
def settings_v2(
    request: Request,
    test_result: str | None = None,
    test_error: str | None = None,
    restore_result: str | None = None,
    restore_count: int | None = None,
    reset_result: str | None = None,
    notif_import_result: str | None = None,
):
    ctx = _build_settings_context(request, test_result, test_error, restore_result, restore_count, reset_result, notif_import_result)
    conn = db.get_db()
    ctx["categories_with_counts"] = get_categories(conn.cursor(), with_counts=True)
    ctx["default_cat_id"] = db.default_category_id(conn)
    conn.close()
    return templates.TemplateResponse("v2/settings.html", ctx)


@app.post("/settings/sync/generate-key")
def generate_sync_key(next: str | None = Form(None)):
    # Regenerating invalidates whatever key any already-connected device is
    # using - a deliberate choice (lost/compromised key should actually stop
    # working), just means the person needs to re-paste the new one into any
    # device they still want syncing.
    conn = db.get_db()
    cur = conn.cursor()
    notifications.set_setting(cur, "sync_api_key", secrets.token_urlsafe(24))
    conn.commit()
    conn.close()
    # v2 sends next=/v2/settings; the old UI sends nothing. Local paths only.
    back = next if (next and next.startswith("/") and not next.startswith("//")) else "/settings"
    return RedirectResponse(url=back, status_code=303)


@app.post("/settings/sync/enabled")
def set_sync_enabled(enabled: str = Form(...), next: str = Form("/settings")):
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "sync_enabled", "1" if enabled == "1" else "0")
    conn.commit()
    conn.close()
    back = next if (next.startswith("/") and not next.startswith("//")) else "/settings"
    return RedirectResponse(url=back, status_code=303)


@app.post("/settings/categories/add")
def add_category(name: str = Form(...), next: str = Form("/settings")):
    conn = db.get_db()
    _create_category(conn.cursor(), name)
    conn.commit()
    conn.close()
    return RedirectResponse(url=next, status_code=303)


@app.post("/settings/categories/{category_id}/series")
def toggle_category_series(category_id: int, next: str = Form("/settings")):
    conn = db.get_db()
    conn.execute("UPDATE categories SET has_series = 1 - has_series WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=next, status_code=303)


@app.post("/settings/categories/{category_id}/remove")
def remove_category(category_id: int, next: str = Form("/settings")):
    """Only removes a category nothing uses - items are never left without
    one. The default category can't be removed either, since new items
    from sync and older code paths fall back to it."""
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM items WHERE category_id = ? AND deleted_at IS NULL", (category_id,))
    in_use = cur.fetchone()[0]
    default_id = db.default_category_id(conn)
    if not in_use and category_id != default_id:
        # Deleted items aren't shown anywhere, but still point at this
        # category - move them to the default so nothing is left pointing
        # at a category that no longer exists.
        cur.execute("UPDATE items SET category_id = ? WHERE category_id = ?", (default_id, category_id))
        cur.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        logger.info("CATEGORY REMOVED: id=%s", category_id)
    conn.close()
    return RedirectResponse(url=next, status_code=303)


@app.post("/settings/shops/rename")
def rename_shop(old_name: str = Form(...), new_name: str = Form(...), next: str = Form("/settings")):
    """Renames a shop across every item, or merges it into an existing
    shop of that name if one already exists - same operation either way,
    just a plain rename of the source column. Also renames it in
    shipment_postage so real shipping estimates already captured for
    this shop don't silently stop matching after the rename."""
    old_clean = old_name.strip()
    new_clean = new_name.strip()
    if not old_clean or not new_clean or old_clean == new_clean:
        return RedirectResponse(url=next, status_code=303)

    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("UPDATE items SET source = ?, updated_at = ? WHERE source = ?", (new_clean, db.utc_now(), old_clean))
    cur.execute("UPDATE shipment_postage SET source = ? WHERE source = ?", (new_clean, old_clean))
    conn.commit()
    conn.close()
    logger.info("SHOP RENAME: %r -> %r", old_clean, new_clean)
    return RedirectResponse(url=next, status_code=303)


SIDEBAR_BUDGET_DISPLAYS = ("spent_of", "left", "percent", "daily", "due", "hidden")


@app.post("/settings")
def save_settings(
    notify_provider: str = Form("none"),
    notify_hour: str = Form("8"),
    ntfy_url: str = Form(""),
    ntfy_topic: str = Form(""),
    gotify_url: str = Form(""),
    gotify_token: str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    webhook_url: str = Form(""),
    webhook_json_template: str = Form(""),
    monthly_budget: str = Form(""),
    notify_on_quiet_days: str = Form("no"),
    weekly_digest_enabled: str = Form("no"),
    weekly_digest_day: str = Form("0"),
    budget_cycle: str = Form("monthly"),
    budget_rollover: str = Form("no"),
    budget_alert_enabled: str = Form("no"),
    currency_symbol: str = Form("gbp"),
    default_landing_page: str = Form("dashboard"),
    auto_backup: str = Form("no"),
    sidebar_budget_display: str | None = Form(None),
    next: str = Form("/settings"),
):
    # Only the v2 Budget card sends this; every other settings form (and
    # the old UI) leaves it alone rather than resetting it.
    if sidebar_budget_display in SIDEBAR_BUDGET_DISPLAYS:
        _c = db.get_db()
        notifications.set_setting(_c.cursor(), "sidebar_budget_display", sidebar_budget_display)
        _c.commit()
        _c.close()
    notifications.save_settings({
        "notify_provider": notify_provider,
        "notify_hour": notify_hour,
        "ntfy_url": ntfy_url.strip(),
        "ntfy_topic": ntfy_topic.strip(),
        "gotify_url": gotify_url.strip(),
        "gotify_token": gotify_token.strip(),
        "telegram_bot_token": telegram_bot_token.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "webhook_url": webhook_url.strip(),
        "webhook_json_template": webhook_json_template.strip() or '{"title": "{title}", "message": "{message}"}',
        "monthly_budget": monthly_budget.strip(),
        "notify_on_quiet_days": notify_on_quiet_days,
        "weekly_digest_enabled": weekly_digest_enabled,
        "weekly_digest_day": weekly_digest_day,
        "budget_cycle": budget_cycle,
        "budget_rollover": budget_rollover,
        "budget_alert_enabled": budget_alert_enabled,
        "currency_symbol": currency_symbol,
        "default_landing_page": default_landing_page,
        "auto_backup": auto_backup,
    })
    logger.info("SETTINGS SAVED: provider=%s notify_hour=%s monthly_budget=%s", notify_provider, notify_hour, monthly_budget)
    return RedirectResponse(url=next, status_code=303)


@app.post("/settings/test")
def test_notification(next: str = Form("/settings")):
    conn = db.get_db()
    cur = conn.cursor()
    ok, err = notifications.send_via_configured_provider(
        cur, "Ka-Ching! test", "If you're seeing this, notifications are working."
    )
    conn.close()
    if ok:
        return RedirectResponse(url=f"{next}?test_result=sent", status_code=303)
    return RedirectResponse(url=f"{next}?test_error={quote(err or 'Unknown error')}", status_code=303)


@app.post("/settings/test-digest")
def test_digest(next: str = Form("/settings")):
    result = notifications.check_and_notify_tomorrow(force=True)
    if result is None:
        return RedirectResponse(url=f"{next}?test_error=No+provider+configured", status_code=303)
    ok, err = result
    if ok:
        return RedirectResponse(url=f"{next}?test_result=sent", status_code=303)
    return RedirectResponse(url=f"{next}?test_error={quote(err or 'Unknown error')}", status_code=303)


@app.post("/settings/test-weekly-digest")
def test_weekly_digest(next: str = Form("/settings")):
    result = notifications.check_and_notify_week(force=True)
    if result is None:
        return RedirectResponse(url=f"{next}?test_error=No+provider+configured", status_code=303)
    ok, err = result
    if ok:
        return RedirectResponse(url=f"{next}?test_result=sent", status_code=303)
    return RedirectResponse(url=f"{next}?test_error={quote(err or 'Unknown error')}", status_code=303)


@app.post("/settings/test-budget-alert")
def test_budget_alert(next: str = Form("/settings")):
    result = check_budget_threshold(force=True)
    if result is None:
        return RedirectResponse(url=f"{next}?test_error=No+provider+configured", status_code=303)
    ok, err = result
    if ok:
        return RedirectResponse(url=f"{next}?test_result=sent", status_code=303)
    return RedirectResponse(url=f"{next}?test_error={quote(err or 'Unknown error')}", status_code=303)


@app.post("/settings/factory-reset")
def factory_reset(confirm: str = Form(...)):
    if confirm != "RESET":
        return RedirectResponse(url="/settings?reset_result=cancelled", status_code=303)
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM items")
    cur.execute("DELETE FROM orders")
    cur.execute("DELETE FROM dismissed_duplicates")
    cur.execute("DELETE FROM shipment_postage")
    conn.commit()
    conn.close()
    logger.warning("FACTORY RESET: all tracked items and order data wiped, settings kept")
    return RedirectResponse(url="/settings?reset_result=done", status_code=303)


NOTIFICATION_SETTINGS_KEYS = [
    "notify_provider", "notify_hour", "ntfy_url", "ntfy_topic",
    "gotify_url", "gotify_token", "telegram_bot_token", "telegram_chat_id",
    "webhook_url", "webhook_json_template", "notify_on_quiet_days",
    "weekly_digest_enabled", "weekly_digest_day",
]


@app.get("/settings/export-notifications.json")
def export_notification_config():
    """Downloads just the notification setup - handy when moving to a new
    server, without needing a full database backup for just this."""
    conn = db.get_db()
    cur = conn.cursor()
    config = {key: notifications.get_setting(cur, key, "") for key in NOTIFICATION_SETTINGS_KEYS}
    conn.close()
    payload = json.dumps(config, indent=2).encode("utf-8")
    filename = f"kaching-notification-config-{date.today().isoformat()}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/settings/import-notifications")
async def import_notification_config(notification_config_file: UploadFile = File(...), next: str = Form("/settings")):
    contents = await notification_config_file.read()
    try:
        config = json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("NOTIFICATION CONFIG IMPORT rejected: not valid JSON (filename=%s)", notification_config_file.filename)
        return RedirectResponse(url=f"{next}?notif_import_result=bad_file", status_code=303)

    if not isinstance(config, dict):
        return RedirectResponse(url=f"{next}?notif_import_result=bad_file", status_code=303)

    to_apply = {key: str(config[key]) for key in NOTIFICATION_SETTINGS_KEYS if key in config}
    notifications.save_settings(to_apply)
    logger.info("NOTIFICATION CONFIG IMPORTED: %s keys applied", len(to_apply))
    return RedirectResponse(url=f"{next}?notif_import_result=ok", status_code=303)


@app.get("/settings/export-all.csv")
def export_all_csv():
    """Downloads every tracked item, unfiltered - the full spend history,
    not just whatever's currently filtered on the Search page."""
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items ORDER BY release_date DESC, name")
    rows = [dict(r) for r in cur.fetchall()]
    category_names = _sync_category_names(cur)
    conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Price", "Release Date", "Shop", "Status", "Paid", "Order Number", "Category"])
    for r in rows:
        writer.writerow([
            _csv_safe(r["name"]),
            f"{r['price']:.2f}",
            r["release_date"] or "",
            _csv_safe(r["source"]),
            r["status"],
            "Yes" if r["charge_status"] == "charged" else "No",
            _csv_safe(r["order_number"]) if r["order_number"] else "",
            _csv_safe(category_names.get(r.get("category_id")) or category_names.get(None) or ""),
        ])
    csv_bytes = buffer.getvalue().encode("utf-8")
    filename = f"kaching-full-export-{date.today().isoformat()}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/settings/backup")
def download_backup():
    backup_name = f"kaching-backup-{date.today().isoformat()}.db"
    # Snapshot first (see _snapshot_db) so the download includes changes
    # still in the WAL file, then clean the temp copy up once it's sent.
    tmp_path = f"{db.DB_PATH}.download-{secrets.token_hex(6)}"
    _snapshot_db(tmp_path)
    return FileResponse(
        tmp_path, filename=backup_name, media_type="application/octet-stream",
        background=BackgroundTask(os.remove, tmp_path),
    )


_AUTO_BACKUP_NAME_RE = re.compile(r"^kaching-auto-\d{8}-\d{6}\.db$")


@app.get("/settings/backups/{filename}")
def download_auto_backup(filename: str):
    """Downloads one specific auto-backup by name. The filename pattern is
    checked strictly (fixed prefix, all-digit timestamp, .db suffix) before
    it's ever joined onto a path, so nothing after the URL's own routing
    can point outside the backups folder."""
    if not _AUTO_BACKUP_NAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Not found")
    backup_dir = os.path.join(os.path.dirname(db.DB_PATH), "backups")
    full_path = os.path.join(backup_dir, filename)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full_path, filename=filename, media_type="application/octet-stream")


@app.post("/settings/restore")
async def restore_backup(backup_file: UploadFile = File(...), next: str | None = Form(None)):
    # The v2 Settings page sends next=/v2/settings; the old UI sends
    # nothing and keeps landing back on /settings. Local paths only.
    back = next if (next and next.startswith("/") and not next.startswith("//")) else "/settings"
    contents = await backup_file.read()

    # A real SQLite database file always starts with this exact 16-byte header
    if not contents.startswith(b"SQLite format 3\x00"):
        logger.warning("BACKUP RESTORE rejected: uploaded file isn't a SQLite database (filename=%s)", backup_file.filename)
        return RedirectResponse(
            url=f"{back}?test_error={quote('That file is not a valid database (wrong file type?)')}",
            status_code=303,
        )

    tmp_path = f"{db.DB_PATH}.restore-tmp"
    with open(tmp_path, "wb") as f:
        f.write(contents)

    # Confirm it's actually a Ka-Ching database (has the items table) before
    # touching anything live - a valid SQLite file that isn't ours would
    # otherwise wipe out the real data with something unrelated.
    try:
        test_conn = sqlite3.connect(tmp_path)
        test_cur = test_conn.cursor()
        test_cur.execute("SELECT COUNT(*) FROM items")
        item_count = test_cur.fetchone()[0]
        test_conn.close()
    except sqlite3.Error as exc:
        os.remove(tmp_path)
        logger.warning("BACKUP RESTORE rejected: not a Ka-Ching database (filename=%s, error=%s)", backup_file.filename, exc)
        return RedirectResponse(
            url=f"{back}?test_error={quote('That file is a database, but not a Ka-Ching one')}",
            status_code=303,
        )

    # Safety copy of whatever's currently live, in case this restore turns
    # out to be the wrong file - not exposed in the UI, but sits in /data
    # for manual recovery via docker exec if ever needed.
    if os.path.exists(db.DB_PATH):
        safety_path = f"{db.DB_PATH}.before-restore-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        _snapshot_db(safety_path)
        logger.info("BACKUP RESTORE: saved safety copy of current database to %s", safety_path)

    # Copy the backup in through SQLite rather than swapping the file on
    # disk. Swapping the file leaves the live database's WAL/shm files
    # behind, and SQLite can replay those stale pages over the restored
    # data. The backup API takes the proper locks and replaces every page.
    try:
        restore_src = sqlite3.connect(tmp_path)
        live = db.get_db()
        try:
            restore_src.backup(live)
        finally:
            live.close()
            restore_src.close()
        os.remove(tmp_path)
    except sqlite3.Error as exc:
        # Fallback (e.g. a backup with a different page size, which SQLite
        # won't copy into a WAL database): empty the WAL into the main
        # file first, swap the file, and remove the leftover WAL/shm.
        logger.warning("BACKUP RESTORE: in-place copy failed (%s), falling back to file swap", exc)
        chk = db.get_db()
        chk.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        chk.close()
        os.replace(tmp_path, db.DB_PATH)
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(db.DB_PATH + suffix)
            except FileNotFoundError:
                pass
    logger.info("BACKUP RESTORE: replaced live database, filename=%s, items=%d", backup_file.filename, item_count)

    # Bring the restored file up to the current schema straight away, the
    # same upgrade startup runs. A backup from before categories existed
    # gets the categories table, the starter list, and every item set to
    # the default (Comics) - without this the v2 pages would error until
    # the container was restarted.
    db.init_db()
    logger.info("BACKUP RESTORE: schema upgrade applied to restored database")

    return RedirectResponse(url=f"{back}?restore_result=ok&restore_count={item_count}", status_code=303)


# --- API ------------------------------------------------------------------

@app.get("/api/summary")
def api_summary():
    """Small JSON endpoint intended for cron/ntfy digests - see README."""
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    week_end = today + timedelta(days=6)
    week_items = fetch_items_between(cur, today, week_end)

    m_start, m_end = month_bounds(today)
    month_items = fetch_items_between(cur, m_start, m_end)
    month_groups = group_by_date(month_items)
    month_comics_total = sum(i["price"] for i in month_items)
    month_shipping_total, month_spent_shipping, month_remaining_shipping, _, _, _, _, _, _ = compute_shipping_for_groups(cur, month_groups)
    month_total = round(month_comics_total + month_shipping_total, 2)

    month_spent_comics, month_remaining_comics, _, _ = split_spent_remaining(month_items)

    conn.close()
    return {
        "week_total": round(sum(i["price"] for i in week_items), 2),
        "week_item_count": len(week_items),
        "month_total_estimate": month_total,
        "month_item_count": len(month_items),
        "month_spent": round(month_spent_comics + month_spent_shipping, 2),
        "month_remaining": round(month_remaining_comics + month_remaining_shipping, 2),
        "month": today.strftime("%B %Y"),
    }


# --- App sync ---------------------------------------------------------------
#
# Two-way sync between this server and the Android app. Direct connection
# only - the app talks straight to this server with a token it was given
# once, nothing else is in the loop. No accounts, no telemetry, no data
# about this ever leaves this server other than to the device the person
# themselves connected.
#
# Known gap: `shipping` (real postage captured against `shipment_postage`
# on this side, a plain per-item column on the app's side) isn't part of
# the sync payload yet - the two sides model it too differently to fold in
# here safely. Doesn't cause data loss, it just doesn't round-trip yet.

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


def _item_row_to_sync_dict(row: sqlite3.Row, category_names: dict | None = None) -> dict:
    out = {field: row[field] for field in SYNC_ITEM_FIELDS}
    out["manual_override"] = bool(row["manual_override"])
    out["updated_at"] = row["updated_at"]
    out["deleted"] = bool(row["deleted_at"])
    if category_names is not None:
        out["category"] = category_names.get(row["category_id"]) or category_names.get(None)
    return out


def _sync_category_names(cur) -> dict:
    """category id -> name for the sync payload. The None key holds the
    default category's name, used for any item whose category is missing
    or points at one that no longer exists."""
    cur.execute("SELECT id, name FROM categories")
    names = {r["id"]: r["name"] for r in cur.fetchall()}
    default_id = db.default_category_id(cur.connection)
    names[None] = names.get(default_id)
    return names


def _sync_category_id(cur, name):
    """A pushed category name -> id (matched ignoring case, created if it's
    new, same as typing a new one on the logging forms). None when the app
    didn't send one, which callers treat as "leave the category alone"."""
    if name is None or not str(name).strip():
        return None
    return _create_category(cur, str(name))


def _require_sync_key(request: Request):
    # Checked before anything else, so while sync is switched off nothing
    # a phone sends is read or written.
    if not _sync_enabled():
        raise HTTPException(
            status_code=503,
            detail="Sync is switched off on this server - turn it on in Settings -> Sync.",
        )
    conn = db.get_db()
    stored_key = notifications.get_setting(conn.cursor(), "sync_api_key", None)
    conn.close()
    if not stored_key:
        raise HTTPException(
            status_code=503,
            detail="Sync isn't set up on this server yet - generate a sync key in Settings first.",
        )
    provided = request.headers.get("X-Sync-Key")
    if not provided or not secrets.compare_digest(provided, stored_key):
        raise HTTPException(status_code=401, detail="Invalid or missing sync key.")


@app.post("/api/sync")
async def api_sync(request: Request, payload: SyncRequest):
    _require_sync_key(request)

    now = db.utc_now()
    conn = db.get_db()
    cur = conn.cursor()
    applied = []
    conflicts = []
    skipped_duplicates = []
    reconciled = []
    default_category = db.default_category_id(conn)

    for item in payload.push:
        cur.execute("SELECT * FROM items WHERE uuid = ?", (item.uuid,))
        existing = cur.fetchone()

        # Something changed here for this item after the client's own last
        # successful checkpoint, and the client also wants to change it now
        # - don't silently pick a winner, surface both versions and let the
        # person decide (per the "flag it and let me pick" conflict policy).
        # Note: on a client's very first sync (since=None) there's nothing
        # to compare against, so a same-uuid row here just gets overwritten
        # - fine for a brand new item, but if this is really the same phone
        # re-syncing from scratch that's a rough edge reconciliation still
        # doesn't close, since it only matches on (order_number, name, price).
        if existing is not None and payload.since and existing["updated_at"] > payload.since:
            conflicts.append({
                "uuid": item.uuid,
                "mine": item.model_dump(),
                "theirs": _item_row_to_sync_dict(existing, _sync_category_names(cur)),
            })
            continue

        if item.deleted:
            if existing is not None:
                cur.execute(
                    "UPDATE items SET deleted_at = ?, updated_at = ? WHERE uuid = ?",
                    (item.updated_at, item.updated_at, item.uuid),
                )
                applied.append(item.uuid)
            # If it never existed here under this uuid, there's nothing to
            # delete - a delete colliding with an un-reconciled row under a
            # different uuid is a rarer edge case reconciliation doesn't
            # cover yet, just a silent no-op rather than a crash.
            continue

        # Resolved only once the item is actually being written, so a
        # conflicting push can't create a category as a side effect.
        pushed_category_id = _sync_category_id(cur, item.category)

        if existing is not None:
            cur.execute(
                """
                UPDATE items
                SET name = ?, order_number = ?, placed_date = ?, status = ?, release_date = ?,
                    charge_status = ?, price = ?, note = COALESCE(?, note), source = ?, tracking_number = ?,
                    manual_override = ?, updated_at = ?, deleted_at = NULL,
                    category_id = COALESCE(?, category_id)
                WHERE uuid = ?
                """,
                (item.name, item.order_number, item.placed_date, item.status, item.release_date,
                 item.charge_status, item.price, item.note, item.source, item.tracking_number,
                 int(item.manual_override), item.updated_at, pushed_category_id, item.uuid),
            )
            applied.append(item.uuid)
            continue

        try:
            cur.execute(
                """
                INSERT INTO items
                    (name, order_number, placed_date, status, release_date, charge_status,
                     price, note, imported_at, manual_override, source, tracking_number, uuid, updated_at,
                     category_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.name, item.order_number, item.placed_date, item.status, item.release_date,
                 item.charge_status, item.price, item.note, now, int(item.manual_override),
                 item.source, item.tracking_number, item.uuid, item.updated_at,
                 pushed_category_id or default_category),
            )
            applied.append(item.uuid)
        except sqlite3.IntegrityError:
            # This exact (order_number, name, price) already exists here
            # under a DIFFERENT uuid - almost always real order history
            # that existed on both sides independently before sync ever
            # existed, not a genuine new duplicate. First-time
            # reconciliation: find that row, let whichever side's data is
            # actually newer win, and tell the client to adopt the
            # server's uuid for it going forward instead of leaving the
            # two as permanently separate records.
            cur.execute(
                "SELECT * FROM items WHERE order_number IS ? AND name = ? AND price = ?",
                (item.order_number, item.name, item.price),
            )
            match = cur.fetchone()
            if match is None:
                # Constraint fired but a plain re-query found nothing -
                # shouldn't happen, but don't crash the batch over it.
                logger.warning(
                    "SYNC: collision on uuid=%s (%r) but no matching row found on re-query - skipping",
                    item.uuid, item.name,
                )
                skipped_duplicates.append(item.uuid)
                continue

            if item.updated_at > match["updated_at"]:
                cur.execute(
                    """
                    UPDATE items
                    SET name = ?, order_number = ?, placed_date = ?, status = ?, release_date = ?,
                        charge_status = ?, price = ?, note = COALESCE(?, note), source = ?, tracking_number = ?,
                        manual_override = ?, updated_at = ?, category_id = COALESCE(?, category_id)
                    WHERE uuid = ?
                    """,
                    (item.name, item.order_number, item.placed_date, item.status, item.release_date,
                     item.charge_status, item.price, item.note, item.source, item.tracking_number,
                     int(item.manual_override), item.updated_at, pushed_category_id, match["uuid"]),
                )
            logger.info(
                "SYNC: reconciled uuid=%s (%r) with existing server row uuid=%s",
                item.uuid, item.name, match["uuid"],
            )
            reconciled.append({"local_uuid": item.uuid, "server_uuid": match["uuid"]})

    # Shipping is simpler than items: no uuid/conflict machinery needed,
    # just "the real figure for this order/shipment", so whichever side's
    # capture is more recent wins outright rather than surfacing a
    # conflict to resolve.
    shipping_applied = []
    for rec in payload.push_shipping:
        cur.execute(
            """
            INSERT INTO shipment_postage (order_number, shipment_index, amount, captured_at, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(order_number, shipment_index) DO UPDATE SET
                amount = excluded.amount, captured_at = excluded.captured_at, source = excluded.source
            WHERE excluded.captured_at > shipment_postage.captured_at
            """,
            (rec.order_number, rec.shipment_index, rec.amount, rec.captured_at, rec.source),
        )
        shipping_applied.append(f"{rec.order_number}:{rec.shipment_index}")

    orders_applied = []
    for rec in payload.push_orders:
        cur.execute(
            """
            INSERT INTO orders (order_number, declared_total, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_number) DO UPDATE SET
                declared_total = excluded.declared_total, last_seen_at = excluded.last_seen_at
            WHERE excluded.last_seen_at > orders.last_seen_at
            """,
            (rec.order_number, rec.declared_total, rec.last_seen_at),
        )
        orders_applied.append(rec.order_number)

    # Pull: anything that changed here - via this push just above, the
    # webui, or another device - since this client's last checkpoint.
    if payload.since:
        cur.execute("SELECT * FROM items WHERE updated_at > ?", (payload.since,))
    else:
        cur.execute("SELECT * FROM items")
    change_rows = cur.fetchall()
    category_names = _sync_category_names(cur)
    changes = [_item_row_to_sync_dict(row, category_names) for row in change_rows]

    if payload.since:
        cur.execute("SELECT * FROM shipment_postage WHERE captured_at > ?", (payload.since,))
    else:
        cur.execute("SELECT * FROM shipment_postage")
    shipping_changes = [dict(row) for row in cur.fetchall()]

    if payload.since:
        cur.execute("SELECT * FROM orders WHERE declared_total IS NOT NULL AND last_seen_at > ?", (payload.since,))
    else:
        cur.execute("SELECT * FROM orders WHERE declared_total IS NOT NULL")
    order_changes = [dict(row) for row in cur.fetchall()]

    # A shop-name -> colour mapping and the server's own configured
    # shipping default, computed once here and handed to the app rather
    # than the app trying to replicate this server's colour-hashing
    # algorithm independently - a full DISTINCT query, not scoped to
    # this delta, so every shop the app might display gets a colour,
    # not just ones that happen to have changed recently.
    cur.execute("SELECT DISTINCT source FROM items WHERE source IS NOT NULL")
    shop_colors = {row["source"]: source_color(row["source"]) for row in cur.fetchall()}
    sync_categories = [
        {"name": c["name"], "color": c["color"], "has_series": bool(c["has_series"])}
        for c in get_categories(cur)
    ]

    cur.execute(
        """
        INSERT INTO sync_state (client_id, client_label, last_synced_at, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            client_label = excluded.client_label, last_synced_at = excluded.last_synced_at
        """,
        (payload.client_id, payload.client_label, now, now),
    )

    conn.commit()
    conn.close()

    logger.info(
        "SYNC: client=%s label=%r pushed=%d applied=%d conflicts=%d reconciled=%d skipped_duplicates=%d pulled=%d shipping_pushed=%d shipping_pulled=%d orders_pushed=%d orders_pulled=%d",
        payload.client_id, payload.client_label, len(payload.push), len(applied), len(conflicts),
        len(reconciled), len(skipped_duplicates), len(changes),
        len(shipping_applied), len(shipping_changes), len(orders_applied), len(order_changes),
    )

    return {
        "server_time": now,
        "applied": applied,
        "conflicts": conflicts,
        "reconciled": reconciled,
        "skipped_duplicates": skipped_duplicates,
        "changes": changes,
        "shipping_applied": shipping_applied,
        "shipping_changes": shipping_changes,
        "orders_applied": orders_applied,
        "order_changes": order_changes,
        "shop_colors": shop_colors,
        "default_shipping_estimate": DEFAULT_SHIPPING_ESTIMATE,
        # Full category list every sync (like shop_colors), so a renamed
        # or recoloured category reaches the app even when none of its
        # items changed.
        "categories": sync_categories,
    }
