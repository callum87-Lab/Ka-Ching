import calendar
import hashlib
import logging
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, parser

APP_DIR = os.path.dirname(__file__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kaching")

app = FastAPI(title="Ka-Ching!")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))
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


# --- Shared helpers ----------------------------------------------------------

def source_color(name: str) -> str:
    if name == DEFAULT_SOURCE:
        return "#2fd8ff"
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16)
    return SOURCE_PALETTE[h % len(SOURCE_PALETTE)]


def get_shipping_estimate(cur):
    """Work out shipping per parcel from the person's own order history where
    possible, rather than relying on a guessed constant.

    Preference order:
    1. Exact per-shipment postage figures, captured directly from pasted
       order-detail pages (Order Summary -> Postage breakdown) - real data,
       no approximation involved.
    2. An approximation from order-history pages: the declared order total
       minus the items' cost, split evenly across however many distinct
       release dates the order covers (Forbidden Planet splits shipping by
       release date per their own stated policy).
    3. DEFAULT_SHIPPING_ESTIMATE, until there's enough real data to trust
       either of the above.
    """
    cur.execute("SELECT order_number, amount FROM shipment_postage")
    postage_rows = cur.fetchall()
    exact_samples = [r["amount"] for r in postage_rows if r["amount"] and r["amount"] > 0]
    if len(exact_samples) >= MIN_SHIPPING_SAMPLES:
        return round(sum(exact_samples) / len(exact_samples), 2), "exact", len(exact_samples), len(exact_samples)

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
        query += " AND source = ?"
        params.append(source)
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


def split_shipping(groups, shipping_estimate):
    spent_shipping = sum(shipping_estimate for g in groups if g["all_paid"])
    remaining_shipping = sum(shipping_estimate for g in groups if not g["all_paid"])
    return round(spent_shipping, 2), round(remaining_shipping, 2)


def build_chart_data(cur, today: date, range_key: str = DEFAULT_CHART_RANGE):
    cfg = RANGE_CONFIGS.get(range_key, RANGE_CONFIGS[DEFAULT_CHART_RANGE])
    unit = cfg["unit"]
    chart = []

    def sum_between(start: date, end: date):
        cur.execute(
            """
            SELECT COALESCE(SUM(price), 0) AS total, COUNT(*) AS n
            FROM items
            WHERE status != 'cancelled'
              AND date(COALESCE(release_date, placed_date)) BETWEEN date(?) AND date(?)
            """,
            (start.isoformat(), end.isoformat()),
        )
        return cur.fetchone()

    if unit == "week":
        for delta in range(-cfg["back"], cfg["forward"] + 1):
            w_start = today + timedelta(days=delta * 7)
            w_end = w_start + timedelta(days=6)
            row = sum_between(w_start, w_end)
            chart.append({
                "label": w_start.strftime("%d %b"),
                "total": round(row["total"], 2),
                "count": row["n"],
                "is_current": delta == 0,
                "is_future": delta > 0,
            })
    else:  # month
        for delta in range(-cfg["back"], cfg["forward"] + 1):
            m_start = shift_month(today, delta)
            m_end = m_start.replace(day=calendar.monthrange(m_start.year, m_start.month)[1])
            row = sum_between(m_start, m_end)
            chart.append({
                "label": m_start.strftime("%b"),
                "total": round(row["total"], 2),
                "count": row["n"],
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


def get_all_sources(cur):
    cur.execute("SELECT DISTINCT source FROM items WHERE status != 'cancelled' ORDER BY source")
    return [r["source"] for r in cur.fetchall()]


# --- Dashboard ---------------------------------------------------------------

@app.get("/")
def dashboard(request: Request, month: str | None = None, chart_range: str | None = None, source: str | None = None):
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    shipping_estimate, shipping_source, shipping_samples, shipping_orders_checked = get_shipping_estimate(cur)

    active_source = source if source else None

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
    hero_shipments = len(hero_groups)
    hero_shipping_total = round(hero_shipments * shipping_estimate, 2)
    hero_grand_total = round(hero_comics_total + hero_shipping_total, 2)

    hero_spent_comics, hero_remaining_comics, hero_spent_count, hero_remaining_count = split_spent_remaining(hero_items)
    hero_spent_shipping, hero_remaining_shipping = split_shipping(hero_groups, shipping_estimate)
    hero_spent_total = round(hero_spent_comics + hero_spent_shipping, 2)
    hero_remaining_total = round(hero_remaining_comics + hero_remaining_shipping, 2)

    nm_start = shift_month(today, 1)
    nm_end = nm_start.replace(day=calendar.monthrange(nm_start.year, nm_start.month)[1])
    next_month_items = fetch_items_between(cur, nm_start, nm_end)
    next_month_groups = group_by_date(next_month_items)
    next_month_comics_total = round(sum(i["price"] for i in next_month_items), 2)
    next_month_total = round(next_month_comics_total + len(next_month_groups) * shipping_estimate, 2)

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
    viewed_comics_total = round(sum(i["price"] for i in viewed_items), 2)
    viewed_shipments = len(viewed_groups)
    viewed_shipping_total = round(viewed_shipments * shipping_estimate, 2)
    viewed_grand_total = round(viewed_comics_total + viewed_shipping_total, 2)

    v_spent_comics, v_remaining_comics, viewed_spent_count, viewed_remaining_count = split_spent_remaining(viewed_items)
    v_spent_shipping, v_remaining_shipping = split_shipping(viewed_groups, shipping_estimate)
    viewed_spent_total = round(v_spent_comics + v_spent_shipping, 2)
    viewed_remaining_total = round(v_remaining_comics + v_remaining_shipping, 2)

    prev_month_param = shift_month(viewed_month, -1).strftime("%Y-%m")
    next_month_param = shift_month(viewed_month, 1).strftime("%Y-%m")
    viewed_month_param = viewed_month.strftime("%Y-%m")
    is_current_month = (viewed_month.year == today.year and viewed_month.month == today.month)

    active_chart_range = chart_range if chart_range in RANGE_CONFIGS else DEFAULT_CHART_RANGE
    chart_data = build_chart_data(cur, today, active_chart_range)

    year_stats = get_year_to_date(cur, today)

    duplicate_groups = find_duplicate_groups(cur)
    ghost_items = find_ghost_items(cur)
    all_sources = get_all_sources(cur)
    source_colors = {s: source_color(s) for s in all_sources}

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
        "chart_data": chart_data,
        "range_tabs": RANGE_TABS,
        "active_chart_range": active_chart_range,
        "shipping_estimate": shipping_estimate,
        "shipping_source": shipping_source,
        "shipping_samples": shipping_samples,
        "shipping_orders_checked": shipping_orders_checked,
        "year_stats": year_stats,
        "duplicate_groups": duplicate_groups,
        "ghost_items": ghost_items,
        "all_sources": all_sources,
        "source_colors": source_colors,
        "active_source": active_source,
        "total_items_tracked": total_items_tracked,
        "has_any_data": total_items_tracked > 0,
        "recently_cancelled": recently_cancelled,
    })


@app.post("/items/{item_id}/mark")
def mark_item(item_id: int, action: str = Form(...)):
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
    return RedirectResponse(url="/", status_code=303)


@app.post("/duplicates/dismiss")
def dismiss_duplicate(name: str = Form(...), release_date: str = Form(...)):
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO dismissed_duplicates (name, release_date, dismissed_at) VALUES (?, ?, ?)",
        (name, release_date, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


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
def new_item_form(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_all_sources(cur)
    conn.close()
    return templates.TemplateResponse("item_form.html", {
        "request": request,
        "item": None,
        "form_action": "/items/new",
        "all_sources": all_sources,
        "heading": "Add an item",
        "submit_label": "Add item",
    })


@app.post("/items/new")
def create_item(
    name: str = Form(...),
    price: float = Form(...),
    release_date: str = Form(...),
    source: str = Form(...),
    already_paid: str | None = Form(None),
):
    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    now = datetime.now(timezone.utc).isoformat()
    charge_status = "charged" if already_paid else "not_charged"
    source_clean = source.strip() or DEFAULT_SOURCE

    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO items
            (name, order_number, placed_date, status, release_date, charge_status,
             price, note, imported_at, manual_override, source)
        VALUES (?, NULL, ?, 'preorder', ?, ?, ?, NULL, ?, 1, ?)
        """,
        (name.strip(), today.isoformat(), release_iso, charge_status, price, now, source_clean),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    logger.info(
        "MANUAL ADD: id=%s name=%r price=%s release_date=%s source=%r",
        new_id, name, price, release_iso, source_clean,
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
):
    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    charge_status = "charged" if already_paid else "not_charged"
    source_clean = source.strip() or DEFAULT_SOURCE

    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE items
        SET name = ?, price = ?, release_date = ?, source = ?, charge_status = ?, manual_override = 1
        WHERE id = ?
        """,
        (name.strip(), price, release_iso, source_clean, charge_status, item_id),
    )
    conn.commit()
    conn.close()
    logger.info(
        "MANUAL EDIT: id=%s name=%r price=%s release_date=%s source=%r",
        item_id, name, price, release_iso, source_clean,
    )
    return RedirectResponse(url="/", status_code=303)


# --- Calendar -----------------------------------------------------------------

@app.get("/calendar")
def calendar_view(request: Request, month: str | None = None):
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
    items = fetch_items_between(cur, v_start, v_end)
    conn.close()

    by_date = {}
    for it in items:
        by_date.setdefault(it["release_date"], []).append(it)

    agenda_groups = group_by_date(items)

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
    is_current_month = (viewed_month.year == today.year and viewed_month.month == today.month)

    return templates.TemplateResponse("calendar.html", {
        "request": request,
        "viewed_month_label": viewed_month.strftime("%B %Y"),
        "weeks": weeks,
        "agenda_groups": agenda_groups,
        "prev_month_param": prev_month_param,
        "next_month_param": next_month_param,
        "is_current_month": is_current_month,
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


# --- Import -------------------------------------------------------------------

@app.get("/import")
def import_form(request: Request):
    return templates.TemplateResponse("import.html", {"request": request, "result": None})


@app.post("/import")
def import_submit(request: Request, order_text: str = Form(...)):
    result = parser.import_text(order_text)
    for c in result.get("release_changes", []):
        c["old_date_label"] = date.fromisoformat(c["old_date"]).strftime("%d %b %Y") if c["old_date"] else "no date set"
        c["new_date_label"] = date.fromisoformat(c["new_date"]).strftime("%d %b %Y")
    return templates.TemplateResponse("import.html", {"request": request, "result": result})


# --- API ------------------------------------------------------------------

@app.get("/api/summary")
def api_summary():
    """Small JSON endpoint intended for cron/ntfy digests - see README."""
    today = date.today()
    conn = db.get_db()
    cur = conn.cursor()

    shipping_estimate, _, _, _ = get_shipping_estimate(cur)

    week_end = today + timedelta(days=6)
    week_items = fetch_items_between(cur, today, week_end)

    m_start, m_end = month_bounds(today)
    month_items = fetch_items_between(cur, m_start, m_end)
    month_groups = group_by_date(month_items)
    month_comics_total = sum(i["price"] for i in month_items)
    month_shipping_total = len(month_groups) * shipping_estimate
    month_total = round(month_comics_total + month_shipping_total, 2)

    month_spent_comics, month_remaining_comics, _, _ = split_spent_remaining(month_items)
    month_spent_shipping, month_remaining_shipping = split_shipping(month_groups, shipping_estimate)

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
