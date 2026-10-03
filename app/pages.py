"""Page routes: Dashboard, Orders, Calendar, Search, Insights, Alerts, and redirects for old addresses."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers


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


def source_tab_group(source: str) -> str:
    """Groups per-seller eBay sources ('eBay - sad_lemon_comics', 'eBay -
    bearsgames', ...) into one 'eBay' entry for filter tabs - a separate
    tab per eBay seller gets unwieldy fast, and the specific seller still
    shows in the grouped item lists themselves, just not as its own tab."""
    if source == "eBay" or source.startswith("eBay -"):
        return "eBay"
    return source


def annotate_group_shipping(cur, groups):
    """Attaches the correct shipping amount to each source-group within
    date-groups, for direct display - exact per-order postage when known,
    an estimate only when it genuinely isn't. Mirrors the same logic
    compute_shipping_for_groups uses for the totals, so what's shown next
    to each shipment always matches what's counted in the numbers above it."""
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


def _insights_category_chips(cats_param):
    """Category filter chips for the Price creep / Top titles pages.

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
        series_groups.setdefault(series_key, []).append({"sort_key": sort_key, "price": it["price"], "name": it["name"], "release_date": it["release_date"], "category_id": it.get("category_id")})

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

    # Semi-circle gauge split point, for the pre-order/released gauge shape
    # matching the approved design (a half-circle).
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

    # This month's release days, for the Cumulative spend card below.
    today = date.today()
    month_items = [i for i in dated_items if i["release_date"][:7] == today.strftime("%Y-%m")]
    month_groups = group_by_date(month_items)
    cumulative_month_label = today.strftime("%B")

    # Overview "Cumulative spend" card (budget bar + release-day bars).
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
    _cm_cats = []
    if notifications.get_setting(cur, "budget_cycle", "monthly") == "monthly":
        _cm_cats = [{"name": r["name"], "color": r["color"], "spent": round(r["total"] - r["due"], 2), "due": r["due"]}
                    for r in _category_budget_rows(cur, today) if r["total"] > 0]
    cumulative_month_json = json.dumps({
        "ym": today.strftime("%Y-%m"),
        "month_name": today.strftime("%B"),
        "days_in_month": calendar.monthrange(today.year, today.month)[1],
        "today": today.day,
        "days": cm_days,
        "budget": cm_budget,
        "cats": _cm_cats,
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
    week_spent, week_remaining, _, _ = split_spent_remaining(week_items)

    # --- Hero: always the TRUE current month, all sources, regardless of what's being browsed below ---
    hero_start, hero_end = month_bounds(today)
    hero_items = fetch_items_between(cur, hero_start, hero_end)
    hero_groups = group_by_date(hero_items)
    hero_comics_total = round(sum(i["price"] for i in hero_items), 2)
    (hero_shipping_total, hero_spent_shipping, hero_remaining_shipping, hero_shipments,
     shipping_estimate, shipping_primary_source, shipping_source, shipping_samples, shipping_orders_checked
     ) = compute_shipping_for_groups(cur, hero_groups)
    hero_grand_total = round(hero_comics_total + hero_shipping_total, 2)

    budget_cycle = notifications.get_setting(cur, "budget_cycle", "monthly")
    monthly_budget = None
    budget_pct = None
    budget_bar_pct = None
    cycle_spend = None
    budget_cycle_label = {"monthly": "this month", "weekly": "this week", "28day": "this 28-day period"}.get(budget_cycle, "this month")

    # Same figures as the sidebar budget box, from the one shared calculation
    # (it follows the budget cycle and rollover, and ignores the shop filter -
    # a budget is for everything you buy).
    _bs = _topbar_budget_status()
    if _bs:
        monthly_budget = _bs["monthly_budget"]
        cycle_spend = _bs["cycle_spend"]
        budget_pct = _bs["budget_pct"]
        budget_bar_pct = _bs["budget_bar_pct"]

    hero_spent_comics, hero_remaining_comics, hero_spent_count, _ = split_spent_remaining(hero_items)
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

    v_spent_comics, v_remaining_comics, _, _ = split_spent_remaining(viewed_items)
    viewed_remaining_total = round(v_remaining_comics + v_remaining_shipping, 2)

    prev_month_param = shift_month(viewed_month, -1).strftime("%Y-%m")
    next_month_param = shift_month(viewed_month, 1).strftime("%Y-%m")
    viewed_month_param = viewed_month.strftime("%Y-%m")
    is_current_month = (viewed_month.year == today.year and viewed_month.month == today.month)

    active_chart_range = chart_range if chart_range in RANGE_CONFIGS else DEFAULT_CHART_RANGE
    chart_data_all = {key: build_chart_data(cur, today, key) for key in RANGE_CONFIGS}
    chart_svg_all = {key: render_trend_svg(data, key) for key, data in chart_data_all.items()}

    year_stats = get_year_to_date(cur, today)
    cur.execute(
        "SELECT substr(release_date, 1, 7) AS m, SUM(price) AS t FROM items "
        "WHERE status != 'cancelled' AND release_date LIKE ? AND release_date <= ? "
        "GROUP BY m ORDER BY t DESC LIMIT 1",
        (f"{today.year}-%", today.isoformat()),
    )
    _pm = cur.fetchone()
    year_top_month = datetime.strptime(_pm["m"], "%Y-%m").strftime("%B") if _pm and _pm["m"] else None
    all_time_stats = get_all_time_stats(cur)

    duplicate_groups = find_duplicate_groups(cur)
    ghost_items = find_ghost_items(cur)
    undated_items = find_undated_items(cur)
    auto_backup_on = notifications.get_setting(cur, "auto_backup", "no") == "yes"
    category_budget = _category_budget_rows(cur, today)
    category_budget_total = round(sum(r["total"] for r in category_budget), 2)
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
        "undated_items": undated_items,
        "auto_backup_on": auto_backup_on,
        "category_budget": category_budget,
        "category_budget_total": category_budget_total,
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
        "year_top_month": year_top_month,
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


def _cover_price_creep(ctx):
    """Price creep, based on COVER price: the cheapest copy bought of
    each issue number stands in for that issue's standard cover, and the
    trend runs first issue -> latest issue. Variants are counted
    separately (how much extra they cost on average) instead of making
    the trend spike. Returns overrides for the page's context; the old UI
    keeps its original first-copy/latest-copy figures untouched."""
    # Category colour for each series' dot (its most common category)
    _conn = db.get_db()
    _cats = {r["id"]: {"name": r["name"], "color": r["color"]} for r in _conn.execute("SELECT id, name, color FROM categories").fetchall()}
    _default = db.default_category_id(_conn)
    _conn.close()
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
        _counts = {}
        for e in st["entries"]:
            cid = e.get("category_id") if e.get("category_id") in _cats else _default
            _counts[cid] = _counts.get(cid, 0) + 1
        _cat = _cats.get(max(_counts, key=_counts.get)) if _counts else None
        series_rows.append({
            "category_name": _cat["name"] if _cat else "",
            "category_color": _cat["color"] if _cat else "#7c89ad",
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
        # Median, not mean: one odd series (e.g. only a pricey variant bought
        # for its first issue) can't drag the typical figure off course.
        "creep_avg_increase": round(statistics.median(r["change_pct"] for r in series_rows), 1) if tracked else 0,
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
        group["shipping"] = round(day_shipping, 2)
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
    undated_items = find_undated_items(cur)
    category_overages = find_category_overages(cur, today)
    currently_open = alert_counts(cur, today)["total"]
    if alert_events:
        last_event_date = date.fromisoformat(alert_events[0]["at"][:10])
        days_since_last_alert = (today - last_event_date).days
    else:
        days_since_last_alert = None

    conn.close()
    return render("alerts.html", {
        "request": request,
        "duplicate_groups": duplicate_groups,
        "ghost_items": ghost_items,
        "awaiting_charge": awaiting_charge,
        "notification_log": notification_log,
        "values": values,
        "alert_history": alert_history,
        "resolved_this_month": resolved_this_month,
        "category_overages": category_overages,
        "undated_items": undated_items,
        "currently_open": currently_open,
        "days_since_last_alert": days_since_last_alert,
        "test_result": test_result,
        "test_error": test_error,
        "notif_import_result": notif_import_result,
    })


@app.get("/classic")
@app.get("/classic/{rest:path}")
def classic_redirect(request: Request, rest: str = ""):
    """The old interface was kept at /classic/ for v3.0 only and is gone in
    v3.1; old links land on the same page in the current interface."""
    q = request.url.query
    return RedirectResponse(url="/" + rest + ("?" + q if q else ""), status_code=301)


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
    return render("dashboard.html", ctx)


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

    # Recently cancelled for the Orders card: same 30-day window and filter
    # as the shared dashboard context's recently_cancelled, but without its
    # LIMIT 15 - the card paginates, so it can show everything in the window.
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

    return render("orders.html", ctx)


@app.post("/duplicates/dismiss")
def dismiss_duplicate(name: str = Form(...), release_date: str = Form(...), next: str | None = Form(None)):
    next = safe_redirect(next, None)
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO dismissed_duplicates (name, release_date, dismissed_at) VALUES (?, ?, ?)",
        (name, release_date, datetime.now(timezone.utc).isoformat()),
    )
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


@app.get("/insights")
def insights_overview_v2(request: Request):
    ctx = _build_insights_context(request)
    return render("insights_overview.html", ctx)


@app.get("/insights/spend-by-shop")
def insights_shop_v2(request: Request):
    ctx = _build_insights_context(request)
    return render("insights_shop.html", ctx)


@app.get("/insights/price-creep")
def insights_price_creep_v2(request: Request):
    ctx = _insights_category_ctx(request)
    ctx.update(_cover_price_creep(ctx))
    return render("insights_pricecreep.html", ctx)


@app.get("/insights/top-titles")
def insights_top_titles_v2(request: Request):
    ctx = _insights_category_ctx(request)
    return render("insights_toptitles.html", ctx)


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
    return render("search.html", ctx)


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


@app.get("/calendar")
def calendar_v2(request: Request, month: str | None = None, source: str | None = None):
    ctx = _build_calendar_context(request, month, source)
    return render("calendar.html", ctx)
