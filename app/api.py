"""Machine-facing routes: the phone sync API, summary JSON, calendar feed, service worker, debug view."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers


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


def _item_row_to_sync_dict(row: sqlite3.Row, category_names: dict | None = None) -> dict:
    out = {field: row[field] for field in SYNC_ITEM_FIELDS}
    out["manual_override"] = bool(row["manual_override"])
    out["updated_at"] = row["updated_at"]
    out["deleted"] = bool(row["deleted_at"])
    if category_names is not None:
        out["category"] = category_names.get(row["category_id"]) or category_names.get(None)
    return out


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


@app.get("/sw.js")
def service_worker():
    """The same no-caching worker as /static/sw.js, but served from the root
    so its scope covers every page - which phones need before they'll offer
    a proper 'install app'."""
    path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    with open(path, encoding="utf-8") as f:
        body = f.read()
    return Response(body, media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


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

    # SUMs every shipment_index for a given order - a split delivery (the
    # same order captured across more than one real postage figure) must
    # count all of them, not just whichever row a plain dict comprehension
    # happened to keep last. This was a real, confirmed bug: an order with
    # two real shipments was silently having one of them dropped here.
    exact_by_order = _postage_by_order(cur)

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
        summary = f"{len(day_items)} item{'s' if len(day_items) != 1 else ''} out ({currency_symbol}{total:,.2f})"
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
