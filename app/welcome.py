"""The first-run setup guide (/welcome): what you collect, pre-orders, which
pages show, currency and budget, theme and layout, and an optional password.
Every step can be skipped. Shown once, on an install with no items that
hasn't finished or skipped it; existing installs never see it unless they
open it from Settings."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers

CURRENCIES = {"gbp": "£ GBP", "usd": "$ USD", "eur": "€ EUR"}
BUDGET_CYCLES = {"monthly": "Monthly", "weekly": "Weekly", "28day": "28-day rolling"}
STEP_NAMES = ("collect", "preorder", "pages", "budget", "look", "password", "done")


def setup_needed(cur):
    """True on a brand-new install: nothing logged yet and the guide not
    finished or skipped."""
    if notifications.get_setting(cur, "setup_done", "") == "yes":
        return False
    return cur.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def _page_cards():
    """The toggles shown on the 'Your pages' step: the cards the presets decide."""
    names = {c["id"]: c["name"] for pg in CARD_LAYOUT for c in pg["cards"]}
    pretty = {"page.creep": "Price creep", "page.titles": "Top titles"}
    out = []
    for cid in list(TYPE_CARDS) + _preorder_cards():
        page = next(pg["page"] for pg in CARD_LAYOUT if any(c["id"] == cid for c in pg["cards"]))
        out.append({"id": cid, "name": pretty.get(cid, names.get(cid, cid)), "page": page.replace("Insights · ", "Insights: "),
                    "preorder": cid in _preorder_cards()})
    return out


@app.get("/welcome")
def welcome(request: Request, step: str = "collect", error: str | None = None):
    conn = db.get_db()
    cur = conn.cursor()
    tr = _tracking()
    values = {
        "kinds": tr["kinds"] if tr["saved"] else [],
        "preorder": tr["preorder"],
        "currency": notifications.get_setting(cur, "currency_symbol", "gbp"),
        "budget": notifications.get_setting(cur, "monthly_budget", ""),
        "cycle": notifications.get_setting(cur, "budget_cycle", "monthly"),
        "theme": _ui_theme()["theme"],
        "layout": notifications.get_setting(cur, "wide_layout", "standard"),
    }
    conn.close()
    return render("welcome.html", {
        "request": request,
        "values": values,
        "start_step": step if step in STEP_NAMES else "collect",
        "error": error,
        "page_cards": _page_cards(),
        "currencies": CURRENCIES,
        "cycles": BUDGET_CYCLES,
        "login_on": login_enabled(),
        "login_env": bool(_env_password()),
    })


@app.post("/welcome")
async def welcome_save(request: Request):
    """Saves every step that wasn't skipped, then finishes. A password
    problem sends you back to that step with everything else already saved."""
    form = await request.form()
    skipped = set(form.getlist("skip"))
    conn = db.get_db()
    cur = conn.cursor()

    if form.get("skip_all"):
        notifications.set_setting(cur, "setup_done", "yes")
        conn.commit()
        conn.close()
        logger.info("SETUP GUIDE: skipped")
        return RedirectResponse(url="/", status_code=303)

    kinds = form.getlist("kinds") if "collect" not in skipped else _tracking()["kinds"]
    preorder = form.get("preorder", "yes") != "no" if "preorder" not in skipped else True
    extra_on, extra_off = [], []
    if "pages" not in skipped:
        for c in _page_cards():
            v = form.get("card_" + c["id"])
            if v == "on":
                extra_on.append(c["id"])
            elif v == "off":
                extra_off.append(c["id"])
    if not ({"collect", "preorder", "pages"} <= skipped):
        apply_tracking(cur, kinds, preorder, extra_on=extra_on, extra_off=extra_off)

    if "budget" not in skipped:
        currency = form.get("currency", "gbp")
        if currency in CURRENCIES:
            notifications.set_setting(cur, "currency_symbol", currency)
        budget = (form.get("budget") or "").strip().lstrip("£$€").replace(",", "")
        try:
            budget_val = float(budget) if budget else None
        except ValueError:
            budget_val = None
        if budget_val is None or 0 < budget_val <= 1_000_000:
            notifications.set_setting(cur, "monthly_budget", f"{budget_val:g}" if budget_val else "")
        cycle = form.get("cycle", "monthly")
        if cycle in BUDGET_CYCLES:
            notifications.set_setting(cur, "budget_cycle", cycle)

    if "look" not in skipped:
        theme = form.get("theme", "")
        if theme in UI_THEMES:
            t, accent_id, _ = resolve_ui_theme(theme, "")
            notifications.set_setting(cur, "ui_theme", t)
            notifications.set_setting(cur, "ui_accent", accent_id)
        layout = form.get("layout", "standard")
        if layout in ("standard", "compact", "panel"):
            notifications.set_setting(cur, "wide_layout", layout)

    set_password = None
    password = form.get("password") or ""
    if "password" not in skipped and password and not login_enabled():
        if password != (form.get("confirm") or ""):
            problem = "mismatch"
        else:
            problem = password_problem(password)
        if problem:
            conn.commit()
            conn.close()
            return RedirectResponse(url=f"/welcome?step=password&error={problem}", status_code=303)
        _, _, version = _login_settings()
        notifications.set_setting(cur, "login_password_hash", _hash_password(password))
        notifications.set_setting(cur, "login_version", str(int(version) + 1))
        _end_all_sessions(cur)
        notify_login_change(cur, "turned on")
        set_password = True

    notifications.set_setting(cur, "setup_done", "yes")
    conn.commit()
    conn.close()
    logger.info("SETUP GUIDE: finished (kinds=%s preorder=%s skipped=%s password=%s)", kinds, preorder, sorted(skipped), bool(set_password))
    next_url = "/items/new" if form.get("then") == "log" else "/"
    response = RedirectResponse(url=next_url, status_code=303)
    if set_password:
        _set_session_cookie(response, True, request)
    return response
