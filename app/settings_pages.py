"""Settings routes: the Settings page and everything it saves - budget, categories, layout, security, notifications, backups and restores."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers


def _category_limits_now():
    conn = db.get_db()
    try:
        return _category_limits(conn.cursor())
    finally:
        conn.close()


def _restore_copies():
    """The safety copies a restore leaves next to the database, newest first."""
    folder = os.path.dirname(db.DB_PATH) or "."
    base = os.path.basename(db.DB_PATH)
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for fname in names:
        if not fname.startswith(base + ".before-restore-"):
            continue
        stamp = fname.rsplit("-", 1)[-1]
        full = os.path.join(folder, fname)
        try:
            size_bytes = os.path.getsize(full)
            taken_at = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%d %b %Y, %H:%M")
        except (OSError, ValueError):
            continue
        size_label = f"{size_bytes / 1024 / 1024:.2f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes / 1024:.1f} KB"
        out.append({"filename": fname, "path": full, "stamp": stamp, "taken_at": taken_at, "size_label": size_label})
    out.sort(key=lambda c: c["stamp"], reverse=True)
    return out


def _prune_restore_copies():
    for old in _restore_copies()[RESTORE_COPIES_KEPT:]:
        try:
            os.remove(old["path"])
            logger.info("BACKUP RESTORE: removed old safety copy %s", old["filename"])
        except OSError:
            pass


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
        "restore_copies": _restore_copies(),
        "category_limits": _category_limits_now(),
    }


@app.get("/settings/restore-copies/{filename}")
def download_restore_copy(filename: str):
    for c in _restore_copies():
        if c["filename"] == filename:
            return FileResponse(c["path"], filename=f"kaching-before-restore-{c['stamp']}.db", media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="No such copy")


@app.post("/settings/auto-backup/on")
def turn_on_auto_backup(next: str = Form("/")):
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "auto_backup", "yes")
    conn.commit()
    conn.close()
    back = next if (next.startswith("/") and not next.startswith("//")) else "/"
    return RedirectResponse(url=back, status_code=303)


@app.post("/settings/card-order")
async def save_card_order(request: Request):
    """Save one page's card order. Body: {"path": "/", "order": [ids]}.
    An empty order puts that page back to its default order."""
    try:
        body = await request.json()
        path = body.get("path")
        order = [c for c in body.get("order", []) if isinstance(c, str) and c in CARD_IDS]
    except Exception:
        raise HTTPException(status_code=400, detail="Expected {\"path\": ..., \"order\": [...]}")
    if path not in {pg["path"] for pg in CARD_LAYOUT}:
        raise HTTPException(status_code=400, detail="Unknown page")
    current = _card_order()
    if order:
        current[path] = order
    else:
        current.pop(path, None)
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "card_order", json.dumps(current))
    conn.commit()
    conn.close()
    return {"ok": True, "order": current.get(path, [])}


@app.post("/settings/category-limits")
async def save_category_limits(request: Request):
    form = await request.form()
    limits = {}
    for k, v in form.items():
        if not k.startswith("limit_"):
            continue
        try:
            cid = int(k[6:])
            amount = float(str(v).strip()) if str(v).strip() else 0
        except ValueError:
            continue
        if amount > 0:
            limits[str(cid)] = round(amount, 2)
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "category_limits", json.dumps(limits))
    conn.commit()
    conn.close()
    back = form.get("next") or "/settings"
    back = back if (back.startswith("/") and not back.startswith("//")) else "/settings"
    return RedirectResponse(url=f"{back}?limits_saved=1#category-limits", status_code=303)


@app.post("/settings/security/password")
def set_login_password(request: Request, current: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    """Turn the login on, or change its password. Changing it signs every
    other device out; this browser stays signed in."""
    back = "/settings"
    stored, _, version = _login_settings()
    # Signed in with the docker-compose master password: that's the recovery
    # route, so the Settings password can be replaced without the old one.
    if stored and not _env_password() and not _check_password_hash(current, stored):
        return RedirectResponse(url=back + "?security=wrong-current", status_code=303)
    if len(password) < 6:
        return RedirectResponse(url=back + "?security=too-short", status_code=303)
    if password != confirm:
        return RedirectResponse(url=back + "?security=mismatch", status_code=303)
    conn = db.get_db()
    cur = conn.cursor()
    notifications.set_setting(cur, "login_password_hash", _hash_password(password))
    notifications.set_setting(cur, "login_version", str(int(version) + 1))
    conn.commit()
    conn.close()
    logger.info("LOGIN: password %s", "changed" if stored else "set, login turned on")
    response = RedirectResponse(url=back + ("?security=changed" if stored else "?security=on"), status_code=303)
    _set_session_cookie(response, True)
    return response


@app.post("/settings/security/off")
def turn_login_off(current: str = Form("")):
    back = "/settings"
    stored, _, version = _login_settings()
    if stored and not _env_password() and not _check_password_hash(current, stored):
        return RedirectResponse(url=back + "?security=wrong-current", status_code=303)
    conn = db.get_db()
    cur = conn.cursor()
    notifications.set_setting(cur, "login_password_hash", "")
    notifications.set_setting(cur, "login_version", str(int(version) + 1))
    conn.commit()
    conn.close()
    logger.info("LOGIN: turned off")
    response = RedirectResponse(url=back + "?security=off", status_code=303)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response


@app.post("/settings/wide-layout")
def save_wide_layout(layout: str = Form("standard"), next: str = Form("/settings#layout")):
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "wide_layout", layout if layout in WIDE_LAYOUTS else "standard")
    conn.commit()
    conn.close()
    back = next if (next.startswith("/") and not next.startswith("//")) else "/settings"
    return RedirectResponse(url=back, status_code=303)


@app.post("/settings/layout")
async def save_layout(request: Request):
    """Replace the set of hidden cards. Body: {"hidden": [card ids]}.
    Unknown ids are ignored, so an old page can never store junk."""
    try:
        body = await request.json()
        hidden = sorted({c for c in body.get("hidden", []) if isinstance(c, str) and c in CARD_IDS})
    except Exception:
        raise HTTPException(status_code=400, detail="Expected {\"hidden\": [...]}")
    conn = db.get_db()
    notifications.set_setting(conn.cursor(), "hidden_cards", json.dumps(hidden))
    conn.commit()
    conn.close()
    return {"ok": True, "hidden": hidden}


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
    return render("settings.html", ctx)


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
    # Only the Budget card sends this; every other settings form (and
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
        _prune_restore_copies()

    # Copy the backup in through SQLite rather than swapping the file on
    # disk. Swapping the file leaves the live database's WAL/shm files
    # behind, and SQLite can replay those stale pages over the restored
    # data. The backup API takes the proper locks and replaces every page.
    try:
        restore_src = sqlite3.connect(tmp_path)
        db.reset_request_connection()
        live = db._connect()   # a dedicated connection: SQLite's backup needs a real one
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
        db.reset_request_connection()
        chk = db._connect()
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
    # the default (Comics) - without this the pages would error until
    # the container was restarted.
    db.reset_request_connection()
    db.init_db()
    logger.info("BACKUP RESTORE: schema upgrade applied to restored database")

    return RedirectResponse(url=f"{back}?restore_result=ok&restore_count={item_count}", status_code=303)
