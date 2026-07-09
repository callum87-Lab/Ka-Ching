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


def store_parsed_items(items, order_totals):
    """The storage/refresh logic for Forbidden Planet items - takes an
    already-parsed items list (which may have been reviewed/edited on the
    confirm screen first) rather than deriving them fresh from raw text.
    This is the exact same logic direct import always used; splitting it
    out just lets the review-and-confirm pipeline reuse it unchanged."""
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
    }


def parse_and_store(text: str):
    items, order_totals, skipped_no_order = parse_order_history(text)
    logger.info(
        "IMPORT PARSE: found %d items, captured %d order totals, skipped %d (no order number): %s",
        len(items), len(order_totals), len(skipped_no_order), order_totals,
    )
    result = store_parsed_items(items, order_totals)
    result["skipped_no_order"] = len(skipped_no_order)
    return result


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


def detect_import(text: str):
    """Tries each known parser in turn and returns a single unified preview
    structure for the review-and-confirm screen. Never touches the database
    - this is pure detection, so a bad guess costs nothing until confirmed.

    Forbidden Planet's exact format is tried first since it's the only one
    that's fully reliable; anything it can't recognise falls through to the
    generic parser, which still extracts what it safely can (order number,
    total, shipping, item rows where the structure is clear enough) and
    leaves the rest blank for the person to fill in themselves."""
    fp_items, fp_order_totals, fp_skipped = parse_order_history(text)
    if fp_items:
        preview_items = [
            {
                "name": it["name"],
                "price": it["price"],
                "release_date": it["release_date"].isoformat() if it["release_date"] else "",
                "order_number": it["order_number"],
                "placed_date": it["placed_date"].isoformat() if it["placed_date"] else "",
                "status": it["status"],
                "charge_status": it["charge_status"] or "",
                "note": it["note"] or "",
            }
            for it in fp_items
        ]
        return {
            "parser": "forbidden_planet",
            "source_guess": "Forbidden Planet",
            "rows": preview_items,
            "order_totals": fp_order_totals,
            "skipped_no_order": len(fp_skipped),
            "declared_total": None,
            "shipping": None,
            "order_number": None,
        }

    if looks_like_ebay(text):
        ebay = parse_ebay_order(text)
        default_status = "dispatched" if ebay["already_delivered"] else "preorder"
        default_charge = "charged" if ebay["already_delivered"] else "not_charged"
        preview_items = [
            {
                "name": it["name"],
                "price": it["price"],
                "release_date": it["release_date"] or "",
                "order_number": ebay["order_number"] or "",
                "placed_date": "",
                "status": default_status,
                "charge_status": default_charge,
                "note": it["note"] or "",
            }
            for it in ebay["items"]
        ]
        return {
            "parser": "generic",
            "source_guess": ebay["source_guess"],
            "rows": preview_items,
            "order_totals": {},
            "skipped_no_order": 0,
            "declared_total": ebay["declared_total"],
            "shipping": None,
            "order_number": ebay["order_number"],
        }

    generic = parse_generic_order(text)
    preview_items = [
        {
            "name": it["name"],
            "price": it["price"],
            "release_date": it["release_date"] or "",
            "order_number": generic["order_number"] or "",
            "placed_date": "",
            "status": "preorder",
            "charge_status": "",
            "note": it["note"] or "",
        }
        for it in generic["items"]
    ]
    return {
        "parser": "generic",
        "source_guess": "",
        "rows": preview_items,
        "order_totals": {},
        "skipped_no_order": 0,
        "declared_total": generic["declared_total"],
        "shipping": generic["shipping"],
        "order_number": generic["order_number"],
    }


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


# --- Generic order confirmation parser ("Shopify-style") --------------------
#
# For anything that isn't Forbidden Planet. Built around patterns common to
# small-shop checkouts generally - most run on shared platforms (Shopify
# chief among them), so order confirmations tend to share a recognisable
# shape (Order Number / itemised list / Subtotal / Shipping / Total) even
# though the exact wording varies shop to shop. This is deliberately a
# best-effort parser: anything it can't confidently extract is left blank
# rather than guessed at wrong - the review screen is where a person fills
# in whatever's missing.

_GENERIC_PRICE_RE = re.compile(r"(?:£|GBP\s?)\s?(\d+\.\d{2})")
_GENERIC_EXCLUDE_KEYWORDS = ["subtotal", "total", "postage", "p&p", "shipping"]
_GENERIC_START_ANCHORS = [r"Line Items", r"Items Ordered", r"Item Description.*?Price", r"Order Details"]
_GENERIC_ORDER_NUM_RE = re.compile(r"(?:Order\s*(?:Number|Ref|#)|Order\s*ID)\s*:?\s*#?\s*([A-Za-z0-9\-]+)", re.IGNORECASE)
_GENERIC_TOTAL_RE = re.compile(r"\b(?:Grand\s*Total|Total)\b\s*:?\s*(?:£|GBP\s?)\s?(\d+\.\d{2})", re.IGNORECASE)
_GENERIC_SHIPPING_RE = re.compile(r"(?:Postage\s*&?\s*Packaging|P\s*&\s*P|Shipping|Postage)\s*:?\s*(?:£|GBP\s?)\s?(\d+\.\d{2})", re.IGNORECASE)
_GENERIC_EXACT_DATE_RE = re.compile(
    r"(?:Expected Release|Release Date|Ships?)\s*:?\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)
_GENERIC_INFORMAL_NOTE_RE = re.compile(
    r"\((expected[^)]*|ships?[^)]*|pre-?order[^)]*|in stock[^)]*)\)",
    re.IGNORECASE,
)


def _generic_find_start(text, first_price_pos):
    """Prefer the LATEST recognised header that still comes before the first
    price - avoids stopping at an earlier, less specific anchor and dragging
    in header-row text as part of the first item's name."""
    best = None
    for pat in _GENERIC_START_ANCHORS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m and m.end() <= first_price_pos:
            if best is None or m.end() > best:
                best = m.end()
    return best if best is not None else 0


def _generic_clean_name(s):
    s = re.sub(r"^[\s,;:\-]+(and\s+)?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s,;:\-]+$", "", s)
    s = re.sub(r"\s+\d+\s+(In Stock|Pre-?order)\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


# --- eBay order parser -------------------------------------------------------
#
# eBay's "Order details" page has a very distinctive, consistent shape
# regardless of seller - Order number / Sold by / Total up top, then each
# item repeated as a heading line (twice, back to back - an artifact of
# eBay's own page layout), followed immediately by "£X.XXUnit price £X.XX"
# glued together with no space, then "Item number: ...". Not pre-orders, so
# no release dates - these are completed purchases being logged for record.

_EBAY_ORDER_NUM_RE = re.compile(r"Order number\s*\t?\s*([\w\-]+)", re.IGNORECASE)
_EBAY_TOTAL_RE = re.compile(r"Total\s*\t?\s*£(\d+\.\d{2})", re.IGNORECASE)
_EBAY_SELLER_RE = re.compile(r"Sold by\s*\t?\s*(\S+)", re.IGNORECASE)
_EBAY_ITEM_PRICE_RE = re.compile(r"£(\d+\.\d{2})\s*Unit price", re.IGNORECASE)
_EBAY_SKIP_EXACT = {"Item details", "incl.", "Buyer Protection", "Buy again", "More actions", "Track package"}
_EBAY_PLACED_RE = re.compile(r"Time placed\s*\t?\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE)
_EBAY_DELIVERED_RE = re.compile(r"Delivered on\s+[A-Za-z]+,?\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE)


def looks_like_ebay(text: str) -> bool:
    return bool(re.search(r"Item number:\s*\d+", text)) and bool(_EBAY_ORDER_NUM_RE.search(text))


def parse_ebay_order(text: str):
    """Returns {order_number, declared_total, source_guess, items: [{name,
    price, release_date, note}]}. Pure parsing, never touches the database."""
    order_match = _EBAY_ORDER_NUM_RE.search(text)
    order_number = order_match.group(1) if order_match else None

    total_match = _EBAY_TOTAL_RE.search(text)
    declared_total = float(total_match.group(1)) if total_match else None

    seller_match = _EBAY_SELLER_RE.search(text)
    seller = seller_match.group(1) if seller_match else None
    source_guess = f"eBay - {seller}" if seller else "eBay"

    already_delivered = "Delivered" in text or "delivered" in text.lower()

    # eBay orders aren't pre-orders, so there's no "release date" in the FP
    # sense - but the order itself has a real, unambiguous date attached
    # (when it was delivered, or failing that when it was placed), so use
    # that rather than leaving every item blank for no reason.
    default_date = None
    delivered_match = _EBAY_DELIVERED_RE.search(text)
    if delivered_match:
        d = parse_date(delivered_match.group(1))
        default_date = d.isoformat() if d else None
    if default_date is None:
        placed_match = _EBAY_PLACED_RE.search(text)
        if placed_match:
            d = parse_date(placed_match.group(1))
            default_date = d.isoformat() if d else None

    idx = text.find("Item details")
    item_section = text[idx:] if idx != -1 else text
    lines = [l.strip() for l in item_section.splitlines()]

    items = []
    pending_name = None
    for line in lines:
        if not line or line in _EBAY_SKIP_EXACT:
            continue
        price_match = _EBAY_ITEM_PRICE_RE.search(line)
        if price_match:
            if pending_name:
                items.append({
                    "name": pending_name,
                    "price": float(price_match.group(1)),
                    "release_date": default_date,
                    "note": None,
                })
                pending_name = None
            continue
        if line.startswith("Item number") or line.startswith("Return window"):
            continue
        if line != pending_name:
            pending_name = line

    return {
        "order_number": order_number,
        "declared_total": declared_total,
        "source_guess": source_guess,
        "already_delivered": already_delivered,
        "items": items,
    }


def parse_generic_order(text: str):
    """Returns {order_number, declared_total, shipping, items: [{name, price,
    release_date, note}]}. Never touches the database - pure parsing."""
    order_match = _GENERIC_ORDER_NUM_RE.search(text)
    order_number = order_match.group(1) if order_match else None

    total_match = _GENERIC_TOTAL_RE.search(text)
    declared_total = float(total_match.group(1)) if total_match else None

    shipping_match = _GENERIC_SHIPPING_RE.search(text)
    shipping = float(shipping_match.group(1)) if shipping_match else None

    price_matches = list(_GENERIC_PRICE_RE.finditer(text))
    items = []
    if price_matches:
        start = _generic_find_start(text, price_matches[0].start())
        cursor = start
        for i, m in enumerate(price_matches):
            context_before = text[max(0, m.start() - 40):m.start()].lower()
            if any(kw in context_before for kw in _GENERIC_EXCLUDE_KEYWORDS):
                cursor = m.end()
                continue

            prefix = text[cursor:m.start()]
            release_date = None
            note = None

            dm = _GENERIC_EXACT_DATE_RE.search(prefix)
            im = _GENERIC_INFORMAL_NOTE_RE.search(prefix)
            name_source = prefix
            if dm:
                paren = re.search(r"\([^)]*" + re.escape(dm.group(0)) + r"[^)]*\)", prefix, re.IGNORECASE)
                if paren:
                    name_source = prefix[:paren.start()] + prefix[paren.end():]
                try:
                    release_date = datetime.strptime(
                        f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %B %Y"
                    ).date().isoformat()
                except ValueError:
                    pass
            elif im:
                name_source = prefix[:im.start()] + prefix[im.end():]
                note = im.group(1).strip()
            else:
                # Only look past the price if there's a clear comma to stop
                # at - without one, there's no safe boundary and we'd risk
                # stealing the NEXT item's own note instead.
                comma_pos = text.find(",", m.end())
                if comma_pos != -1 and comma_pos - m.end() < 80:
                    suffix = text[m.end():comma_pos]
                    dm2 = _GENERIC_EXACT_DATE_RE.search(suffix)
                    im2 = _GENERIC_INFORMAL_NOTE_RE.search(suffix)
                    if dm2:
                        try:
                            release_date = datetime.strptime(
                                f"{dm2.group(1)} {dm2.group(2)} {dm2.group(3)}", "%d %B %Y"
                            ).date().isoformat()
                        except ValueError:
                            pass
                    elif im2:
                        note = im2.group(1).strip()

            name = _generic_clean_name(name_source)
            cursor = m.end()
            if name:
                items.append({
                    "name": name,
                    "price": float(m.group(1)),
                    "release_date": release_date,
                    "note": note,
                })

    return {
        "order_number": order_number,
        "declared_total": declared_total,
        "shipping": shipping,
        "items": items,
    }
