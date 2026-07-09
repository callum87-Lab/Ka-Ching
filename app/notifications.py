import json
import logging
import urllib.request
from datetime import date, timedelta

from . import db

logger = logging.getLogger("kaching")

SETTINGS_KEYS = [
    "notify_provider",      # "none" | "ntfy" | "gotify" | "telegram"
    "notify_hour",          # "0".."23"
    "ntfy_url",
    "ntfy_topic",
    "gotify_url",
    "gotify_token",
    "telegram_bot_token",
    "telegram_chat_id",
    "monthly_budget",       # "" or a number, e.g. "80.00"
]

DEFAULTS = {
    "notify_provider": "none",
    "notify_hour": "8",
    "ntfy_url": "https://ntfy.sh",
}


def get_setting(cur, key, default=None):
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None or row["value"] is None:
        return DEFAULTS.get(key, default)
    return row["value"]


def get_all_settings(cur):
    return {key: get_setting(cur, key, "") for key in SETTINGS_KEYS}


def set_setting(cur, key, value):
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_settings(form_values: dict):
    conn = db.get_db()
    cur = conn.cursor()
    for key in SETTINGS_KEYS:
        if key in form_values:
            set_setting(cur, key, form_values[key])
    conn.commit()
    conn.close()


# --- Provider sends -----------------------------------------------------------
#
# Each raises on failure (caught by the caller) rather than swallowing errors,
# so a failed test notification can actually tell the person what went wrong
# instead of silently doing nothing.

def send_ntfy(url: str, topic: str, title: str, message: str):
    if not url or not topic:
        raise ValueError("ntfy needs both a server URL and a topic")
    full_url = f"{url.rstrip('/')}/{topic}"
    req = urllib.request.Request(full_url, data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", "books")
    urllib.request.urlopen(req, timeout=10)


def send_gotify(url: str, token: str, title: str, message: str):
    if not url or not token:
        raise ValueError("Gotify needs both a server URL and an app token")
    full_url = f"{url.rstrip('/')}/message?token={token}"
    payload = json.dumps({"title": title, "message": message, "priority": 5}).encode("utf-8")
    req = urllib.request.Request(full_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=10)


def send_telegram(bot_token: str, chat_id: str, title: str, message: str):
    if not bot_token or not chat_id:
        raise ValueError("Telegram needs both a bot token and a chat ID")
    full_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"{title}\n{message}" if message else title
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(full_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=10)


def send_via_configured_provider(cur, title: str, message: str):
    """Returns (ok: bool, error: str | None)."""
    provider = get_setting(cur, "notify_provider", "none")
    try:
        if provider == "ntfy":
            send_ntfy(get_setting(cur, "ntfy_url"), get_setting(cur, "ntfy_topic"), title, message)
        elif provider == "gotify":
            send_gotify(get_setting(cur, "gotify_url"), get_setting(cur, "gotify_token"), title, message)
        elif provider == "telegram":
            send_telegram(get_setting(cur, "telegram_bot_token"), get_setting(cur, "telegram_chat_id"), title, message)
        else:
            return False, "No notification provider is set up yet."
        return True, None
    except Exception as exc:
        logger.warning("NOTIFICATION SEND FAILED: provider=%s error=%s", provider, exc)
        return False, str(exc)


# --- The actual daily check ----------------------------------------------------

def check_and_notify_tomorrow(force: bool = False):
    """Looks at what's releasing tomorrow and sends one digest notification,
    grouped by shop. Stays silent on quiet days unless force=True (used by
    the manual "test" button, so clicking it always gives visible feedback)."""
    conn = db.get_db()
    cur = conn.cursor()

    provider = get_setting(cur, "notify_provider", "none")
    if provider == "none" and not force:
        conn.close()
        return None

    tomorrow = date.today() + timedelta(days=1)
    cur.execute(
        """
        SELECT * FROM items
        WHERE status != 'cancelled' AND release_date = ?
        ORDER BY source, name
        """,
        (tomorrow.isoformat(),),
    )
    items = [dict(r) for r in cur.fetchall()]

    if not items:
        if force:
            ok, err = send_via_configured_provider(cur, "Ka-Ching!", "Nothing releasing tomorrow.")
            conn.close()
            return (ok, err)
        conn.close()
        return None

    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)

    total = sum(i["price"] for i in items)
    lines = []
    for src, its in sorted(by_source.items()):
        sub = sum(i["price"] for i in its)
        lines.append(f"{src}: {len(its)} item(s), £{sub:.2f}")
    message = "\n".join(lines)
    title = f"Tomorrow: {len(items)} comic{'s' if len(items) != 1 else ''}, £{total:.2f}"

    result = send_via_configured_provider(cur, title, message)
    conn.close()
    logger.info("DAILY DIGEST: tomorrow=%s items=%d result=%s", tomorrow.isoformat(), len(items), result)
    return result
