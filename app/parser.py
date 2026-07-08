import logging
import re
from datetime import datetime, timezone

from . import db

logger = logging.getLogger("kaching")

DATE_FORMATS = ["%d %b %Y", "%d %B %Y"]

BLOCK_HEADER_PATTERN = re.compile(
    r"\b(Placed|Generated|Order#|Subscription Order#|Delivery To|Billing)\b"
)


def parse_date(raw: str):
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_price(raw: str):
    raw = raw.strip().replace("£", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def classify_status(line: str) -> str:
    if line.startswith("Pre-Order"):
        return "preorder"
    if line.startswith("Dispatched"):
        return "dispatched"
    if line.startswith("Processing"):
        return "processing"
    if line.startswith("Cancelled"):
        return "cancelled"
    return "unknown"


def parse_order_history(text: str):
    """Parse a pasted order-history export into a list of item dicts.

    The parser is line-based and deliberately tolerant: it only looks for the
    handful of anchor lines it needs ([image] marker, item name, status word,
    "Release date:", charge status, price) and skips everything else
    (addresses, card details, pagination footers, etc).
    """
    lines = text.splitlines()
    n = len(lines)
    i = 0

    current_order = None
    current_placed_date = None
    items = []
    order_totals = {}
    skipped_no_order = []

    def next_nonblank(idx):
        while idx < n and lines[idx].strip() == "":
            idx += 1
        return idx

    while i < n:
        line = lines[i].strip()

        if line == "":
            i += 1
            continue

        if line in ("Placed", "Generated"):
            i = next_nonblank(i + 1)
            if i < n:
                d = parse_date(lines[i])
                if d:
                    current_placed_date = d
                i += 1
            continue

        if line in ("Order#", "Subscription Order#"):
            i = next_nonblank(i + 1)
            if i < n:
                current_order = lines[i].strip()
                i += 1
            continue

        if line == "Total":
            # The order-level declared total (appears once near the top of
            # each order block, before any items) - used later to back out
            # an implied shipping cost for calibration.
            i = next_nonblank(i + 1)
            if i < n:
                val = parse_price(lines[i])
                if val is not None and current_order:
                    order_totals[current_order] = val
                i += 1
            continue

        if line.startswith("[") and line.endswith("]"):
            # Image marker line -> next non-blank line is the item's display name
            i = next_nonblank(i + 1)
            if i >= n:
                break
            item_name = lines[i].strip()
            i = next_nonblank(i + 1)
            if i >= n:
                break
            status_line = lines[i].strip()
            status = classify_status(status_line)
            i += 1

            release_date = None
            charge_status = None
            note = None
            price = None

            while i < n:
                l = lines[i].strip()
                if l.startswith("£"):
                    price = parse_price(l)
                    i += 1
                    break
                if l == "":
                    i += 1
                    continue
                # Page-break artifact: an order header showed up before we
                # found this item's price. Stop without consuming the line,
                # so the outer loop can pick it up as a fresh order header.
                if l == "Total" or BLOCK_HEADER_PATTERN.search(l):
                    price = None
                    break
                m = re.match(r"Release date:\s*(.+)", l)
                if m:
                    release_date = parse_date(m.group(1))
                elif l == "Not charged":
                    charge_status = "not_charged"
                elif l == "Fully charged":
                    charge_status = "charged"
                elif "running late" in l.lower():
                    note = "running late"
                i += 1

            if price is None:
                # Either malformed, or a page-break ate this item's price -
                # skip recording it rather than guessing.
                continue

            if current_order is None:
                # We've lost track of which order this item belongs to -
                # almost certainly a page-break or pagination artifact that
                # broke the "Order#" line earlier in this block. Recording
                # it anyway would create an orphaned row with no order
                # number, which can never be matched against its real
                # counterpart or deduplicated on a later re-import - so skip
                # it and log full context instead of inserting bad data.
                logger.warning(
                    "SKIPPED item with no known order number: name=%r price=%s release_date=%s "
                    "(likely a page-break artifact nearby in the pasted text)",
                    item_name, price, release_date,
                )
                skipped_no_order.append({"name": item_name, "price": price, "release_date": release_date})
                continue

            items.append({
                "name": item_name,
                "order_number": current_order,
                "placed_date": current_placed_date,
                "status": status,
                "release_date": release_date,
                "charge_status": charge_status,
                "price": price,
                "note": note,
            })
            continue

        i += 1

    return items, order_totals, skipped_no_order


def parse_and_store(text: str):
    items, order_totals, skipped_no_order = parse_order_history(text)
    logger.info(
        "IMPORT PARSE: found %d items, captured %d order totals, skipped %d (no order number): %s",
        len(items), len(order_totals), len(skipped_no_order), order_totals,
    )
    conn = db.get_db()
    cur = conn.cursor()
    imported = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for order_number, declared_total in order_totals.items():
        cur.execute(
            """
            INSERT INTO orders (order_number, declared_total, last_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_number) DO UPDATE SET
                declared_total = excluded.declared_total,
                last_seen_at = excluded.last_seen_at
            """,
            (order_number, declared_total, now),
        )

    for it in items:
        order_number = it["order_number"]
        placed_date = it["placed_date"].isoformat() if it["placed_date"] else None
        release_date = it["release_date"].isoformat() if it["release_date"] else None

        cur.execute(
            """
            INSERT OR IGNORE INTO items
                (name, order_number, placed_date, status, release_date, charge_status, price, note, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                it["name"], order_number, placed_date, it["status"],
                release_date, it["charge_status"], it["price"], it["note"], now,
            ),
        )
        if cur.rowcount:
            imported += 1
            continue

        # Already tracked from a previous import. Refresh its status from
        # this newer import - e.g. a pre-order that's since shipped and
        # been charged - but only if the person hasn't manually ticked it
        # themselves, so a manual "paid"/"cancelled" mark always wins.
        cur.execute(
            "SELECT id, release_date, status FROM items WHERE order_number IS ? AND name = ? AND price = ?",
            (order_number, it["name"], it["price"]),
        )
        existing = cur.fetchone()
        if existing and existing["release_date"] != release_date:
            logger.info(
                "IMPORT REFRESH: id=%s name=%r order=%s release_date %r -> %r (status %r -> %r)",
                existing["id"], it["name"], order_number,
                existing["release_date"], release_date, existing["status"], it["status"],
            )

        cur.execute(
            """
            UPDATE items
            SET status = ?, release_date = ?, charge_status = ?, note = ?, imported_at = ?
            WHERE order_number IS ? AND name = ? AND price = ? AND manual_override = 0
            """,
            (
                it["status"], release_date, it["charge_status"], it["note"], now,
                order_number, it["name"], it["price"],
            ),
        )
        if cur.rowcount:
            updated += 1

    conn.commit()
    conn.close()

    return {
        "found": len(items),
        "imported": imported,
        "updated": updated,
        "skipped": len(items) - imported - updated,
        "order_totals_captured": len(order_totals),
        "skipped_no_order": len(skipped_no_order),
    }


# --- Release-date-change notification emails -------------------------------
#
# Forbidden Planet sends a short email whenever a pre-order's release date
# shifts, e.g.:
#
#   Update to order number: 54502590
#   We have received a revised Release Date for this item:
#   7466038
#   Star Wars: ... (Cover A Jake Bartok)
#   Release Date 19/08/2026
#
# This is a completely different shape to the order-history export (no
# price, no [image] markers, DD/MM/YYYY dates instead of "DD Mon YYYY") so it
# needs its own small parser. re.DOTALL is used throughout so this still
# works if an email client collapses the message onto a single line.

_RELEASE_ORDER_RE = re.compile(r"order number:\s*(\d+)", re.IGNORECASE)
_RELEASE_ITEM_RE = re.compile(
    r"for this item:\s*(.*?)\s*Release Date\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE | re.DOTALL,
)


def _parse_slash_date(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_release_date_updates(text: str):
    """Pull (order_number, item name, new release date) out of a Forbidden
    Planet release-date-change email. Returns a list of dicts."""
    order_match = _RELEASE_ORDER_RE.search(text)
    order_number = order_match.group(1) if order_match else None

    updates = []
    for m in _RELEASE_ITEM_RE.finditer(text):
        new_date = _parse_slash_date(m.group(2))
        if not new_date:
            continue
        # FP prefixes the item name with its own numeric product id on its
        # own line/space, e.g. "7466038 Star Wars: ...". Strip that off.
        name = re.sub(r"^\s*\d+\s+", "", m.group(1))
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        updates.append({"order_number": order_number, "name": name, "release_date": new_date})
    return updates


def apply_release_date_updates(updates):
    if not updates:
        return {"matched": 0, "unmatched": 0, "changes": []}

    conn = db.get_db()
    cur = conn.cursor()
    matched = 0
    unmatched = 0
    changes = []

    for u in updates:
        if u["order_number"]:
            cur.execute(
                "SELECT id, release_date FROM items WHERE name = ? AND order_number = ? AND manual_override = 0",
                (u["name"], u["order_number"]),
            )
        else:
            cur.execute(
                "SELECT id, release_date FROM items WHERE name = ? AND manual_override = 0",
                (u["name"],),
            )
        rows = cur.fetchall()
        if not rows:
            unmatched += 1
            continue

        new_iso = u["release_date"].isoformat()
        for row in rows:
            logger.info(
                "EMAIL DATE UPDATE: id=%s name=%r order=%s release_date %r -> %r",
                row["id"], u["name"], u["order_number"], row["release_date"], new_iso,
            )
            cur.execute("UPDATE items SET release_date = ? WHERE id = ?", (new_iso, row["id"]))
            changes.append({
                "name": u["name"],
                "old_date": row["release_date"],
                "new_date": new_iso,
            })
        matched += 1

    conn.commit()
    conn.close()
    return {"matched": matched, "unmatched": unmatched, "changes": changes}


# --- Order-detail pages: exact per-shipment postage -------------------------
#
# A single order's own detail page (forbiddenplanet.com/orders/<id>/) shows
# real, exact postage per shipment - much better than inferring it from an
# order-history list's declared total, e.g.:
#
#   Order #54503699
#   ...
#   * PostageEstimated 2 shipments
#   One package with 5 items already shipped!4.99
#   One package with 3 items already shipped!3.50
#
# Multiple order-detail pages can be pasted concatenated together; each is
# scoped to its own "Order #<n>" heading so postage never gets misattributed
# to the wrong order.

_ORDER_DETAIL_HEADING_RE = re.compile(r"Order #(\d+)")
_SHIPMENT_POSTAGE_RE = re.compile(r"already shipped!\s*£?\s*([\d,]+\.\d{2})", re.IGNORECASE)


def parse_shipment_postage(text: str):
    """Returns a list of {order_number, shipment_index, amount} dicts, one
    per shipment postage figure found, scoped to its enclosing order."""
    headings = list(_ORDER_DETAIL_HEADING_RE.finditer(text))
    samples = []
    for idx, heading in enumerate(headings):
        order_number = heading.group(1)
        start = heading.end()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        block = text[start:end]
        for shipment_index, m in enumerate(_SHIPMENT_POSTAGE_RE.finditer(block)):
            try:
                amount = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            samples.append({
                "order_number": order_number,
                "shipment_index": shipment_index,
                "amount": amount,
            })
    return samples


def store_shipment_postage(samples):
    if not samples:
        return 0
    conn = db.get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for s in samples:
        cur.execute(
            """
            INSERT INTO shipment_postage (order_number, shipment_index, amount, captured_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(order_number, shipment_index) DO UPDATE SET
                amount = excluded.amount, captured_at = excluded.captured_at
            """,
            (s["order_number"], s["shipment_index"], s["amount"], now),
        )
        logger.info(
            "POSTAGE CAPTURE: order=%s shipment_index=%s amount=%.2f",
            s["order_number"], s["shipment_index"], s["amount"],
        )
    conn.commit()
    conn.close()
    return len(samples)


def import_text(text: str):
    """Single entry point the Import page calls - handles a pasted
    order-history export, a pasted release-date-change email, and pasted
    order-detail pages (for exact shipment postage), since there's no
    reason to make the person pick which kind of paste it is."""
    result = parse_and_store(text)
    updates = parse_release_date_updates(text)
    update_result = apply_release_date_updates(updates)
    result["release_matched"] = update_result["matched"]
    result["release_unmatched"] = update_result["unmatched"]
    result["release_changes"] = update_result["changes"]

    postage_samples = parse_shipment_postage(text)
    result["postage_captured"] = store_shipment_postage(postage_samples)
    return result
