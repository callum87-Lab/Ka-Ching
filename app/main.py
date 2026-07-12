import asyncio
import calendar
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, notifications, parser

APP_DIR = os.path.dirname(__file__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kaching")

app = FastAPI(title="Ka-Ching!")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))

APP_VERSION = "2026.07.12.3"
templates.env.globals["app_version"] = APP_VERSION
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

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
        except Exception:
            logger.exception("NOTIFY SCHEDULER: daily check failed, will retry tomorrow")
            await asyncio.sleep(3600)


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
            dest = os.path.join(backup_dir, f"pullcost-auto-{stamp}.db")
            shutil.copyfile(db.DB_PATH, dest)
            logger.info("AUTO BACKUP: saved %s", dest)

            existing = sorted(
                (f for f in os.listdir(backup_dir) if f.startswith("pullcost-auto-")),
            )
            for old in existing[:-7]:
                os.remove(os.path.join(backup_dir, old))
        except Exception:
            logger.exception("AUTO BACKUP: failed, will retry tomorrow")
            await asyncio.sleep(3600)


@app.on_event("startup")
async def start_notification_scheduler():
    asyncio.create_task(_daily_notification_loop())
    asyncio.create_task(_daily_backup_loop())


# --- Shared helpers ----------------------------------------------------------

def source_color(name: str) -> str:
    if name == DEFAULT_SOURCE:
        return "#2fd8ff"
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
    query = """
        SELECT * FROM items
        WHERE status != 'cancelled'
          AND release_date IS NOT NULL
          AND date(release_date) BETWEEN date(?) AND date(?)
    """
    params = [start.isoformat(), end.isoformat()]
    if source:
        clause, extra_params = source_filter_sql(source)
        query += f" AND {clause}"
        params.extend(extra_params)
    query += " ORDER BY release_date, name"
    cur.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def group_by_date(items):
    """Groups items by release date, then sub-groups each date by source/
    retailer (Forbidden Planet, or wherever else), so a day with releases
    from more than one shop shows each shop's items separately rather than
    one undifferentiated pile."""
    groups = {}
    for it in items:
        key = it["release_date"]
        groups.setdefault(key, []).append(it)
    result = []
    for key in sorted(groups.keys()):
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
            "date_label": date.fromisoformat(key).strftime("%a %d %b"),
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
    cur.execute("SELECT order_number, amount FROM shipment_postage")
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


def annotate_group_shipping(cur, groups):
    """Attaches the correct shipping amount to each source-group within
    date-groups, for direct display - exact per-order postage when known,
    an estimate only when it genuinely isn't. Mirrors the same logic
    compute_shipping_for_groups uses for the totals, so what's shown next
    to each shipment always matches what's counted in the numbers above it."""
    cur.execute("SELECT order_number, amount FROM shipment_postage")
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


def render_trend_svg(chart_data, range_key):
    """Self-contained SVG spend-trend chart - no external chart library, so
    the dashboard never needs to reach the internet to render it. A smooth
    gradient area for total spend (comics + shipping, matching the hero's
    own figures), with a thin bar strip beneath for item count per period -
    a second real metric, not decoration. Hover reveals the exact
    breakdown; handled by a small shared script in base.html."""
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

    def y_at(total):
        return round(pad_top + area_h - (total / max_total) * area_h, 1)

    points = [(x_at(i), y_at(c["total"])) for i, c in enumerate(chart_data)]
    line_path = _smooth_svg_path(points)
    baseline_y = pad_top + area_h
    area_path = f"{line_path} L{points[-1][0]},{baseline_y} L{points[0][0]},{baseline_y} Z"

    grad_id = f"trendgrad-{range_key}"
    bars_top = pad_top + area_h + gap
    label_y = bars_top + bars_h + 16
    bar_w = min(18.0, step * 0.5)

    parts = [
        f'<svg viewBox="0 0 {W} {H}" class="trend-svg" preserveAspectRatio="none" '
        f'role="img" aria-label="Spend trend over time, with item counts below">',
        f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="var(--neon-blue)" stop-opacity="0.45"/>'
        f'<stop offset="100%" stop-color="var(--neon-blue)" stop-opacity="0.02"/>'
        f'</linearGradient></defs>',
    ]

    for frac in (0, 0.5, 1):
        gy = round(pad_top + area_h * (1 - frac), 1)
        parts.append(f'<line x1="{pad_l}" y1="{gy}" x2="{W - pad_r}" y2="{gy}" '
                      f'stroke="var(--border)" stroke-width="1"/>')

    parts.append(f'<path d="{area_path}" fill="url(#{grad_id})" stroke="none"/>')
    parts.append(f'<path d="{line_path}" fill="none" stroke="var(--neon-blue)" '
                  f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')

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
        parts.append(f'<text class="trend-axis-label{alt_class}" x="{x_at(i)}" y="{label_y}" text-anchor="middle" font-size="12" '
                      f'font-weight="{weight}" fill="{color}">{c["label"]}</text>')

    hit_w = round(step, 1)
    for i, c in enumerate(chart_data):
        cx, cy = points[i]
        zx = round(cx - hit_w / 2, 1)
        parts.append(
            f'<rect class="trend-hit" x="{zx}" y="0" width="{hit_w}" height="{bars_top + bars_h}" '
            f'fill="transparent" data-label="{c["label"]}" data-total="{c["total"]:.2f}" '
            f'data-comics="{c["comics_total"]:.2f}" data-shipping="{c["shipping_total"]:.2f}" '
            f'data-count="{c["count"]}" data-cx="{cx}" data-cy="{cy}"/>'
        )

    parts.append('<line class="trend-guide" y1="14" x2="0" stroke="var(--neon-blue)" '
                 f'stroke-width="1" stroke-dasharray="3,3" opacity="0.4" style="display:none;" y2="{bars_top + bars_h}"/>')
    parts.append('<circle class="trend-dot" r="4.5" fill="var(--neon-blue)" '
                 'stroke="var(--bg-1)" stroke-width="2" style="display:none;"/>')
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

@app.get("/")
def dashboard(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None):
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    # Only redirect on a clean, unparameterised visit to "/" - once someone's
    # actively navigating the dashboard (a month, chart range, or shop filter
    # in the URL), respect that rather than bouncing them away mid-browse.
    if not month and not chart_range and not source:
        landing = notifications.get_setting(cur, "default_landing_page", "dashboard")
        landing_paths = {"calendar": "/calendar", "search": "/search", "add": "/items/new"}
        if landing in landing_paths:
            conn.close()
            return RedirectResponse(url=landing_paths[landing], status_code=303)

    active_source = source if source else None

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

    cur.execute(
        "SELECT * FROM items WHERE status = 'cancelled' AND manual_override = 1 ORDER BY id DESC LIMIT 15"
    )
    recently_cancelled = [dict(r) for r in cur.fetchall()]

    conn.close()

    return templates.TemplateResponse("dashboard.html", {
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
        "monthly_budget": monthly_budget,
        "budget_pct": budget_pct,
        "budget_bar_pct": budget_bar_pct,
        "budget_cycle_label": budget_cycle_label,
        "cycle_spend": cycle_spend,
        "date_changes_flash": date_changes_flash,
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
    })


@app.post("/items/{item_id}/mark")
def mark_item(item_id: int, action: str = Form(...), next: str | None = Form(None)):
    conn = db.get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, name, order_number, release_date, status, manual_override FROM items WHERE id = ?",
        (item_id,),
    )
    before = cur.fetchone()
    before_dict = dict(before) if before else None
    logger.info("MARK request: item_id=%s action=%s before=%s", item_id, action, before_dict)

    if action == "paid":
        cur.execute(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                charge_status = 'charged',
                manual_override = 1
            WHERE id = ?
            """,
            (item_id,),
        )
    elif action == "cancel":
        cur.execute(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                status = 'cancelled',
                manual_override = 1
            WHERE id = ?
            """,
            (item_id,),
        )
    elif action == "undo":
        cur.execute(
            """
            UPDATE items
            SET status = COALESCE(prev_status, status),
                charge_status = COALESCE(prev_charge_status, charge_status),
                manual_override = 0,
                prev_status = NULL,
                prev_charge_status = NULL
            WHERE id = ?
            """,
            (item_id,),
        )
    elif action == "remove":
        # Permanent delete - for bad data (duplicate line items, parsing
        # artifacts) rather than a real-world cancellation. No Undo.
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
                manual_override = 1
            WHERE id = ?
            """,
            [(i,) for i in now_ids],
        )
        logger.info("BULK ACTION: cancelled %d items: %s", len(now_ids), now_ids)
    elif bulk_action == "paid":
        cur.executemany(
            """
            UPDATE items
            SET prev_status = CASE WHEN manual_override = 0 THEN status ELSE prev_status END,
                prev_charge_status = CASE WHEN manual_override = 0 THEN charge_status ELSE prev_charge_status END,
                charge_status = 'charged',
                manual_override = 1
            WHERE id = ?
            """,
            [(i,) for i in now_ids],
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


@app.get("/items/new")
def new_items_form(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_all_sources(cur)
    conn.close()
    return templates.TemplateResponse("add_items.html", {
        "request": request,
        "all_sources": all_sources,
        "result": None,
    })


@app.post("/items/new")
async def create_items(request: Request):
    form = await request.form()
    names = form.getlist("name")
    prices = form.getlist("price")
    release_date = form.get("release_date", "")
    source = (form.get("source") or "").strip() or DEFAULT_SOURCE
    already_paid = form.get("already_paid")
    order_number = (form.get("order_number") or "").strip() or None
    shipping_cost_raw = (form.get("shipping_cost") or "").strip()
    tracking_number = (form.get("tracking_number") or "").strip() or None

    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    charge_status = "charged" if already_paid else "not_charged"
    now = datetime.now(timezone.utc).isoformat()

    conn = db.get_db()
    cur = conn.cursor()
    created = []
    for raw_name, raw_price in zip(names, prices):
        clean_name = raw_name.strip()
        if not clean_name:
            continue
        try:
            price_val = float(raw_price)
        except (TypeError, ValueError):
            continue
        cur.execute(
            """
            INSERT INTO items
                (name, order_number, placed_date, status, release_date, charge_status,
                 price, note, imported_at, manual_override, source, tracking_number)
            VALUES (?, ?, ?, 'preorder', ?, ?, ?, NULL, ?, 1, ?, ?)
            """,
            (clean_name, order_number, today.isoformat(), release_iso, charge_status, price_val, now, source, tracking_number),
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
    return RedirectResponse(url="/", status_code=303)


@app.get("/items/{item_id}/edit")
def edit_item_form(request: Request, item_id: int):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()
    all_sources = get_all_sources(cur)
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
):
    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    charge_status = "charged" if already_paid else "not_charged"
    source_clean = source.strip() or DEFAULT_SOURCE
    tracking_clean = tracking_number.strip() or None

    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE items
        SET name = ?, price = ?, release_date = ?, source = ?, charge_status = ?, manual_override = 1, tracking_number = ?
        WHERE id = ?
        """,
        (name.strip(), price, release_iso, source_clean, charge_status, tracking_clean, item_id),
    )
    conn.commit()
    conn.close()
    logger.info(
        "MANUAL EDIT: id=%s name=%r price=%s release_date=%s source=%r",
        item_id, name, price, release_iso, source_clean,
    )
    return RedirectResponse(url="/", status_code=303)


# --- Search --------------------------------------------------------------------

SEARCH_SORT_OPTIONS = {
    "date_desc": ("release_date DESC, name", "Release date (newest)"),
    "date_asc": ("release_date ASC, name", "Release date (oldest)"),
    "price_desc": ("price DESC, name", "Price (highest)"),
    "price_asc": ("price ASC, name", "Price (lowest)"),
    "name_asc": ("name ASC", "Name (A-Z)"),
}


def build_search_query(q, source, status, start_date, end_date):
    """Returns (where_clause, params) shared by the search page and CSV
    export, so both stay in sync with exactly the same filtering logic."""
    conditions = []
    params = []
    if q and q.strip():
        term = f"%{q.strip()}%"
        conditions.append("(name LIKE ? OR order_number LIKE ? OR source LIKE ?)")
        params.extend([term, term, term])
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
    if start_date:
        conditions.append("release_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("release_date <= ?")
        params.append(end_date)
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/insights")
def insights_page(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE status != 'cancelled'")
    all_items = [dict(r) for r in cur.fetchall()]
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
    top_titles = sorted(all_items, key=lambda i: -i["price"])[:3]
    for t in top_titles:
        t["release_date_label"] = (
            date.fromisoformat(t["release_date"]).strftime("%d %b %Y") if t["release_date"] else "no date set"
        )

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

    cur.execute("SELECT price FROM items WHERE status = 'cancelled'")
    cancelled_saved = round(sum(r["price"] for r in cur.fetchall()), 2)

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

    conn.close()
    return templates.TemplateResponse("insights.html", {
        "request": request,
        "top_month": top_month,
        "priciest_item": priciest_item,
        "shop_stats": shop_stats,
        "has_data": bool(all_items),
        "total_issues": total_issues,
        "twelve_month_svg": twelve_month_svg,
        "top_titles": top_titles,
        "avg_per_month": avg_per_month,
        "avg_per_issue": avg_per_issue,
        "preorder_count": preorder_count,
        "released_count": released_count,
        "preorder_pct": preorder_pct,
        "cancelled_saved": cancelled_saved,
        "shipping_ratio_pct": shipping_ratio_pct,
        "total_all_spend": total_all_spend,
    })


@app.get("/search")
def search_items(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_filter_tab_sources(cur)

    active_status = status or "all"
    active_sort = sort if sort in SEARCH_SORT_OPTIONS else "date_desc"

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
    )

    results = []
    spent = remaining = cancelled_total = 0.0
    cancelled_count = 0
    match_count = 0
    truncated = False

    if has_filter:
        where_clause, params = build_search_query(q, source, active_status, start_date, end_date)
        order_sql = SEARCH_SORT_OPTIONS[active_sort][0]

        cur.execute(f"SELECT * FROM items WHERE {where_clause} ORDER BY {order_sql}", params)
        all_matches = [dict(r) for r in cur.fetchall()]

        match_count = len(all_matches)
        spent = round(sum(r["price"] for r in all_matches if r["charge_status"] == "charged" and r["status"] != "cancelled"), 2)
        remaining = round(sum(r["price"] for r in all_matches if r["charge_status"] != "charged" and r["status"] != "cancelled"), 2)
        cancelled_total = round(sum(r["price"] for r in all_matches if r["status"] == "cancelled"), 2)
        cancelled_count = sum(1 for r in all_matches if r["status"] == "cancelled")

        truncated = len(all_matches) > 500
        results = all_matches[:500]
        for r in results:
            r["release_date_label"] = (
                date.fromisoformat(r["release_date"]).strftime("%d %b %Y") if r["release_date"] else "no date set"
            )
            r["source_color"] = source_color(r["source"])

    conn.close()
    return templates.TemplateResponse("search.html", {
        "request": request,
        "q": q or "",
        "results": results,
        "has_filter": has_filter,
        "all_sources": all_sources,
        "active_source": source or "",
        "active_status": active_status,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "active_sort": active_sort,
        "active_date_preset": active_date_preset,
        "active_date_preset_label": active_date_preset_label,
        "sort_options": SEARCH_SORT_OPTIONS,
        "match_count": match_count,
        "spent": spent,
        "remaining": remaining,
        "cancelled_total": cancelled_total,
        "cancelled_count": cancelled_count,
        "truncated": truncated,
    })


@app.get("/search/export.csv")
def export_search_csv(
    q: str | None = None,
    source: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
):
    """Downloads whatever's currently filtered on the search page as a CSV
    file - reuses the exact same query-building logic, so the export always
    matches what's on screen."""
    active_status = status or "all"
    active_sort = sort if sort in SEARCH_SORT_OPTIONS else "date_desc"

    conn = db.get_db()
    cur = conn.cursor()
    where_clause, params = build_search_query(q, source, active_status, start_date, end_date)
    order_sql = SEARCH_SORT_OPTIONS[active_sort][0]
    cur.execute(f"SELECT * FROM items WHERE {where_clause} ORDER BY {order_sql}", params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Name", "Price", "Release Date", "Shop", "Status", "Paid", "Order Number"])
    for r in rows:
        writer.writerow([
            r["name"],
            f"{r['price']:.2f}",
            r["release_date"] or "",
            r["source"],
            r["status"],
            "Yes" if r["charge_status"] == "charged" else "No",
            r["order_number"] or "",
        ])

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=kaching-export-{date.today().isoformat()}.csv"},
    )


# --- Calendar -----------------------------------------------------------------

@app.get("/calendar")
def calendar_view(request: Request, month: str | None = None, source: str | None = None):
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
    conn.close()

    by_date = {}
    for it in items:
        by_date.setdefault(it["release_date"], []).append(it)

    agenda_groups = group_by_date(items)
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
        week.append({
            "day": day_num,
            "date_iso": d_iso,
            "is_today": d == today,
            "count": len(day_items),
            "total": round(sum(i["price"] for i in day_items), 2),
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

    return templates.TemplateResponse("calendar.html", {
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
    })


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
    conn.close()

    by_date = {}
    for it in items:
        by_date.setdefault(it["release_date"], []).append(it)

    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Ka-Ching!//Comic Releases//EN", "CALSCALE:GREGORIAN"]

    for release_date_str, day_items in sorted(by_date.items()):
        d_compact = release_date_str.replace("-", "")
        d_next = (date.fromisoformat(release_date_str) + timedelta(days=1)).strftime("%Y%m%d")
        total = round(sum(i["price"] for i in day_items), 2)
        names = [f"{i['name']} ({i['source']})" for i in day_items]
        summary = f"{len(day_items)} comic{'s' if len(day_items) != 1 else ''} out (\u00a3{total:.2f})"
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
            it["duplicate_flag"] = f"Already tracked - order #{r['order_number']}, £{r['price']:.2f}"
    return preview_items


# --- Import -------------------------------------------------------------------

@app.get("/import")
def import_form_redirect():
    # Import now lives on the same page as Add - keep this route working
    # in case of an old bookmark, but send people to the combined page.
    return RedirectResponse(url="/items/new", status_code=307)


@app.post("/import")
def import_preview(request: Request, order_text: str = Form(...)):
    """Parses the pasted text and shows an editable review screen - nothing
    is written to the database until the person confirms it looks right."""
    preview = parser.detect_import(order_text)

    conn = db.get_db()
    cur = conn.cursor()
    preview["rows"] = check_preview_duplicates(cur, preview["rows"])
    all_sources = get_all_sources(cur)
    conn.close()

    return templates.TemplateResponse("import_preview.html", {
        "request": request,
        "preview": preview,
        "all_sources": all_sources,
    })


@app.post("/import/confirm")
async def import_confirm(request: Request):
    form = await request.form()
    parser_type = form.get("parser_type", "generic")

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
        return RedirectResponse(url="/", status_code=303)

    if parser_type == "order_detail_postage":
        samples = json.loads(form.get("postage_samples_json", "[]"))
        count = parser.store_shipment_postage(samples)
        logger.info("ORDER-DETAIL POSTAGE CAPTURED: %s samples", count)
        return RedirectResponse(url="/", status_code=303)

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
        })

    if not kept_items:
        return RedirectResponse(url="/items/new", status_code=303)

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
                     price, note, imported_at, manual_override, source, tracking_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    it["name"], it["order_number"], today.isoformat(), it["status"] or "preorder",
                    release_iso, it["charge_status"] or "not_charged", it["price"], it["note"], now, item_source,
                    it["tracking_number"],
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

    return RedirectResponse(url="/", status_code=303)


# --- Settings / notifications -------------------------------------------------

@app.get("/settings")
def settings_form(
    request: Request,
    test_result: str | None = None,
    test_error: str | None = None,
    restore_result: str | None = None,
    restore_count: int | None = None,
    reset_result: str | None = None,
):
    conn = db.get_db()
    cur = conn.cursor()
    values = notifications.get_all_settings(cur)
    cur.execute("SELECT COUNT(*) AS n FROM items")
    item_count = cur.fetchone()["n"]
    conn.close()

    try:
        db_size_bytes = os.path.getsize(db.DB_PATH)
        db_size_label = f"{db_size_bytes / 1024 / 1024:.2f} MB" if db_size_bytes >= 1024 * 1024 else f"{db_size_bytes / 1024:.1f} KB"
    except OSError:
        db_size_label = "unknown"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "values": values,
        "test_result": test_result,
        "test_error": test_error,
        "restore_result": restore_result,
        "restore_count": restore_count,
        "reset_result": reset_result,
        "item_count": item_count,
        "db_size_label": db_size_label,
    })


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
    monthly_budget: str = Form(""),
    notify_on_quiet_days: str = Form("no"),
    budget_cycle: str = Form("monthly"),
    budget_rollover: str = Form("no"),
    currency_symbol: str = Form("gbp"),
    default_landing_page: str = Form("dashboard"),
    auto_backup: str = Form("no"),
):
    notifications.save_settings({
        "notify_provider": notify_provider,
        "notify_hour": notify_hour,
        "ntfy_url": ntfy_url.strip(),
        "ntfy_topic": ntfy_topic.strip(),
        "gotify_url": gotify_url.strip(),
        "gotify_token": gotify_token.strip(),
        "telegram_bot_token": telegram_bot_token.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "monthly_budget": monthly_budget.strip(),
        "notify_on_quiet_days": notify_on_quiet_days,
        "budget_cycle": budget_cycle,
        "budget_rollover": budget_rollover,
        "currency_symbol": currency_symbol,
        "default_landing_page": default_landing_page,
        "auto_backup": auto_backup,
    })
    logger.info("SETTINGS SAVED: provider=%s notify_hour=%s monthly_budget=%s", notify_provider, notify_hour, monthly_budget)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/test")
def test_notification():
    conn = db.get_db()
    cur = conn.cursor()
    ok, err = notifications.send_via_configured_provider(
        cur, "Ka-Ching! test", "If you're seeing this, notifications are working."
    )
    conn.close()
    if ok:
        return RedirectResponse(url="/settings?test_result=sent", status_code=303)
    return RedirectResponse(url=f"/settings?test_error={quote(err or 'Unknown error')}", status_code=303)


@app.post("/settings/test-digest")
def test_digest():
    result = notifications.check_and_notify_tomorrow(force=True)
    if result is None:
        return RedirectResponse(url="/settings?test_error=No+provider+configured", status_code=303)
    ok, err = result
    if ok:
        return RedirectResponse(url="/settings?test_result=sent", status_code=303)
    return RedirectResponse(url=f"/settings?test_error={quote(err or 'Unknown error')}", status_code=303)


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


@app.get("/settings/backup")
def download_backup():
    backup_name = f"kaching-backup-{date.today().isoformat()}.db"
    return FileResponse(db.DB_PATH, filename=backup_name, media_type="application/octet-stream")


@app.post("/settings/restore")
async def restore_backup(backup_file: UploadFile = File(...)):
    contents = await backup_file.read()

    # A real SQLite database file always starts with this exact 16-byte header
    if not contents.startswith(b"SQLite format 3\x00"):
        logger.warning("BACKUP RESTORE rejected: uploaded file isn't a SQLite database (filename=%s)", backup_file.filename)
        return RedirectResponse(
            url=f"/settings?test_error={quote('That file is not a valid database (wrong file type?)')}",
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
            url=f"/settings?test_error={quote('That file is a database, but not a Ka-Ching one')}",
            status_code=303,
        )

    # Safety copy of whatever's currently live, in case this restore turns
    # out to be the wrong file - not exposed in the UI, but sits in /data
    # for manual recovery via docker exec if ever needed.
    if os.path.exists(db.DB_PATH):
        safety_path = f"{db.DB_PATH}.before-restore-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(db.DB_PATH, safety_path)
        logger.info("BACKUP RESTORE: saved safety copy of current database to %s", safety_path)

    os.replace(tmp_path, db.DB_PATH)
    logger.info("BACKUP RESTORE: replaced live database, filename=%s, items=%d", backup_file.filename, item_count)

    return RedirectResponse(url=f"/settings?restore_result=ok&restore_count={item_count}", status_code=303)


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
