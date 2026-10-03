"""Item routes: logging orders (manual and paste import), editing, marking paid, delays, bulk actions."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers


def _valid_category_id(cur, raw):
    """A submitted category id if it's real, otherwise None."""
    try:
        cat_id = int(raw)
    except (TypeError, ValueError):
        return None
    cur.execute("SELECT 1 FROM categories WHERE id = ?", (cat_id,))
    return cat_id if cur.fetchone() else None


def _resolve_category(cur, raw):
    """What a category box submitted -> a category id. The forms let you
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


def _parse_item_form_date(raw: str, fallback: date) -> str:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()
    except (ValueError, AttributeError):
        return fallback.isoformat()


MAX_PRICE = 100_000


def _price_ok(value):
    """A sensible price: a real number from 0 up to 100,000."""
    return isinstance(value, (int, float)) and value == value and 0 <= value <= MAX_PRICE


def _valid_date(raw):
    try:
        datetime.strptime((raw or "").strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _clean_text(value):
    """A form value, or None if it's empty or the literal text "None"
    (which older review screens sent back for a missing value)."""
    value = (value or "").strip()
    return None if value in ("", "None") else value


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
        it["already_tracked"] = any(
            str(r["order_number"]) == str(it["order_number"]) and abs((r["price"] or 0) - (it["price"] or 0)) < 0.005
            for r in existing_rows
        )
        it["duplicate_flag"] = None
        if others:
            r = others[0]
            it["duplicate_flag"] = f"Already tracked - order #{r['order_number']}, {symbol}{r['price']:,.2f}"
    return preview_items


@app.post("/items/{item_id}/mark")
def mark_item(item_id: int, action: str = Form(...), next: str | None = Form(None)):
    next = safe_redirect(next, None)
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


@app.post("/items/bulk-action")
def bulk_item_action(
    item_ids: list[str] = Form(...),
    bulk_action: str = Form(...),
    next: str | None = Form(None),
):
    next = safe_redirect(next, None)
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


@app.get("/items/new")
def new_items_form_v2(request: Request):
    conn = db.get_db()
    cur = conn.cursor()
    all_sources = get_all_sources(cur)
    cur.execute("SELECT source FROM items ORDER BY imported_at DESC LIMIT 1")
    last_row = cur.fetchone()
    last_used_source = last_row["source"] if last_row else DEFAULT_SOURCE
    conn.close()
    return render("logorders.html", {
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
    next_url = safe_redirect(form.get("next"), "/")

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
        if not _price_ok(price_val):
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
    return render("edititem.html", {
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
    next = safe_redirect(next, '/')
    if not _price_ok(price):
        conn = db.get_db()
        set_flash(conn.cursor(), f"Price must be between 0 and {MAX_PRICE:,} - nothing was changed.")
        conn.commit()
        conn.close()
        return RedirectResponse(url=safe_redirect(f"/items/{item_id}/edit", "/"), status_code=303)
    today = date.today()
    release_iso = _parse_item_form_date(release_date, today)
    bad_date = bool(release_date.strip()) and not _valid_date(release_date)
    charge_status = "charged" if already_paid else "not_charged"
    source_clean = source.strip() or DEFAULT_SOURCE
    tracking_clean = tracking_number.strip() or None
    note_clean = note.strip() or None

    conn = db.get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    existing = cur.fetchone()
    now = db.utc_now()
    if bad_date and existing is not None:
        # an unreadable date never silently becomes today's
        release_iso = existing["release_date"]
        set_flash(cur, "That release date wasn't a valid date, so it was left as it was.")

    new_values = {
        "name": name.strip(), "price": price, "release_date": release_iso,
        "source": source_clean, "charge_status": charge_status,
        "tracking_number": tracking_clean, "note": note_clean,
    }
    # Category is only changed when the form actually sent one (the edit
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
    next = safe_redirect(next, None)
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
    return RedirectResponse(url=next or safe_redirect(f"/items/{item_id}/edit", "/"), status_code=303)


@app.get("/import")
def import_form_redirect():
    # Import now lives on the same page as Add - keep this route working
    # in case of an old bookmark, but send people to the combined page.
    return RedirectResponse(url="/items/new", status_code=307)


@app.post("/import")
def import_preview(request: Request, order_text: str = Form(...), shop_hint: str = Form(""),
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

    template_name = "importpreview.html"
    return render(template_name, {
        "request": request,
        "preview": preview,
        "all_sources": all_sources,
        "import_category_id": import_category_id,
    })


@app.post("/import/confirm")
async def import_confirm(request: Request):
    form = await request.form()
    parser_type = form.get("parser_type", "generic")
    next_url = safe_redirect(form.get("next"), "/")

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
        if not _price_ok(price):
            continue
        kept_items.append({
            "name": name,
            "price": price,
            "release_date_raw": form.get(f"release_date_{i}") or "",
            "order_number": form.get(f"order_number_{i}") or None,
            "placed_date_raw": form.get(f"placed_date_{i}") or "",
            "status": form.get(f"status_{i}") or "preorder",
            "charge_status": form.get(f"charge_status_{i}") or "not_charged",
            "note": _clean_text(form.get(f"note_{i}")),
            "source": (form.get(f"source_{i}") or "").strip(),
            "tracking_number": (form.get(f"tracking_number_{i}") or "").strip() or None,
            "category_id_raw": form.get(f"category_id_{i}"),
        })

    if not kept_items:
        return RedirectResponse(url=safe_redirect(form.get("next"), "/items/new"), status_code=303)

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
        skipped = 0
        order_sources = {}
        for it in kept_items:
            item_source = it["source"] or "Unknown shop"
            release_iso = it["release_date_raw"] or None
            # The same item in the same order is already tracked (e.g. the
            # page was pasted twice): leave the existing one alone.
            cur.execute(
                "SELECT 1 FROM items WHERE order_number IS ? AND name = ? AND abs(price - ?) < 0.005",
                (it["order_number"], it["name"], it["price"]),
            )
            if cur.fetchone():
                skipped += 1
                continue
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

        if skipped:
            set_flash(cur, f"{skipped} item{'s' if skipped != 1 else ''} already tracked, so skipped"
                           + (f"; {len(created)} added." if created else "."))
        conn.commit()
        conn.close()
        logger.info("IMPORT CONFIRM (generic): created=%s skipped=%s shipping=%s", created, skipped, order_shipping_map)

    return RedirectResponse(url=next_url, status_code=303)
