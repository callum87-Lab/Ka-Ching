# Ka-Ching!

A tiny self-hosted dashboard that answers two questions and nothing else:

- **What's due this week, and what will it cost?**
- **What's my forecast for this month (and next)?**

It does not catalogue your collection, track variants you own, or store cover
art — you've already got apps for that. This just watches your bank balance.

> Note: the folder, Docker container, and database file are all still called
> `pullcost` under the hood — only the name on the page changed. Renaming the
> deployed stack wasn't worth the disruption for a cosmetic change.

The logo is a real image now (`app/static/img/logo.png`), not CSS text —
doubles as the browser tab favicon too. Swap that file for anything the same
rough proportions to change it.

## How it works

You paste your retailer's order history page (Forbidden Planet etc.) into the
Import page every so often. A parser pulls out item name, release date,
price, and charge status, and ignores everything else (addresses, card
numbers, pagination). Already-seen items are skipped automatically, so it's
safe to re-paste the same page next month — nothing gets double-counted.

The dashboard then shows:

- A **still-due** total up top for the current month — this is the number that
  actually matters day to day
- **Spent so far** and the **forecast total** alongside it, so you can see
  spent / remaining / total at a glance
- **This year so far** — spent vs. tracked total across every issue this year
- **This week**, grouped by likely shipment
- **This month**, broken down shipment by shipment, browsable to any month
- A **spend trend** chart with Week / Month / 6M tabs — each bar is a time
  bucket (a week, a month), never a single day: Week shows several weeks of
  totals, Month restores the original view (a handful of months either side
  of now), and 6M shows just the next 6 months forecast.

Shipping is no longer a flat guess. If you paste in a Forbidden Planet
**order detail page** (the page for one specific order, not the order-history
list — its Order Summary shows a "Postage" breakdown with the exact cost of
each shipment), Ka-Ching! reads those figures directly and uses your real
average once there are a few. Multiple order-detail pages can be pasted
concatenated together in one go. Without that, it falls back to an
approximation from the order-history list (declared total minus item costs,
split across shipments), and only falls back further to a flat guess if
neither has enough data yet. A note under the hero shows which one it's
currently using.

Every item has a small circle next to it — tap it to mark that item paid.
Its cost moves from "still due" into "spent" immediately (it stays visible,
just dimmed and ticked). Tap the circle again to undo it. There's also a
small &times; next to the price to cancel an item, and a **Remove** button
next to that.

These do different things. **Cancel** means "this was a real order and it's
been cancelled" — it's reversible (an Undo button appears in "Recently
cancelled"), and matches what re-importing would show once Forbidden Planet
itself marks it cancelled. **Remove** is a permanent delete, for bad data
rather than a real-world event — a duplicate line item that shouldn't exist,
or a parsing artifact — and asks for confirmation since there's no undo.

The "still due" number at the top always reflects the real current month.
The "by shipment" list below it is separately browsable — use the &larr; / &rarr;
arrows either side of the month name to look ahead or back, and "Today" to
jump straight back to the current month. Handy for checking a release date
actually moved after a Forbidden Planet notification, without waiting for it
to become "this month."

Re-pasting your order history later won't undo a manual tick — once you've
marked something, that overrides whatever the retailer's page says about it,
until you hit Undo.

### Duplicate order detection

If the same comic, same release date, ends up tracked under two different
order numbers, a warning shows up on the dashboard — this is almost always an
accidental double-order rather than two genuinely different things releasing
the same day (this is exactly how Ka-Ching! caught a real duplicate order
during testing). Each entry gets its own &times; (cancel) and **Remove**
button, or a "Not a duplicate" button on the group if it's genuinely
intentional — dismissing it stops that specific pairing being flagged again.

This only catches duplicates spread across two different order numbers. If
the same item shows up twice under the *same* order number — a genuine
double-line-item, not a double-order — it won't trigger this warning, since
there's nothing to "cancel" on Forbidden Planet's side. Use **Remove**
directly on the extra line instead.

### A note on "ghost" duplicates with no order number

An earlier version of the parser could, in rare cases with very large pastes,
lose track of which order it was currently reading partway through (usually
a page-break mangling an "Order#" line) and record an item with no order
number attached at all. Those orphaned rows never matched the duplicate
detector (which compares *different* order numbers) and never got cleaned up
by re-importing (a missing order number can't be matched against anything).
If you were tracking a comic and it looked like you owned it twice with only
one order to show for it, this was almost certainly why.

## How importing actually works now

Pasting an order no longer saves anything straight away. Instead:

1. **Ka-Ching! tries to recognise what you pasted**, in order: Forbidden
   Planet's exact format first (the only one that's fully reliable), then
   eBay's order-detail format (also a real dedicated parser, since eBay's
   page shape is consistent regardless of seller - detects the order
   number, seller, total, and every item and price; since these aren't
   pre-orders, items default to the order's own delivery date, or its
   placed date if it hasn't been delivered yet, and are marked
   paid/dispatched automatically if the order shows as delivered), then a
   generic parser built around patterns common to small-shop checkouts
   generally (most run on shared platforms like Shopify, so confirmations
   tend to share a recognisable shape - Order Number / itemised list /
   Subtotal / Shipping / Total - even when the exact wording differs).
2. **You land on a review screen** showing exactly what it found - name,
   price, and release date per item, all editable, plus the shop, order
   number, and shipping if detected. Nothing has been written to the
   database at this point.
3. **Anything it couldn't confidently work out is left blank**, never
   guessed at - an item with only "expected in stock late August" rather
   than an actual date gets no release date, so you fill that in yourself
   rather than Ka-Ching! inventing one.
4. **Possible duplicates are flagged right on this screen** - if something
   with the same name already exists under a different order number, it's
   called out before you've committed to anything, not after.
5. **Untick anything you don't want, fix anything that's wrong, add a row
   by hand if something got missed, then confirm.** Only then does anything
   actually get saved.

For a shop Ka-Ching! doesn't recognise as Forbidden Planet, you'll be asked
to confirm which shop it's from on the review screen - type it once, and it
becomes a known option in the shop dropdown everywhere else in the app from
then on. Detected shipping gets stored as that shop's own real shipping
figure, feeding into...

## Per-shop shipping calibration

Different shops charge different amounts for postage, so shipping is now
calculated separately per shop rather than one blended number applied to
everyone. Each shop works through the same three-tier preference as before
(exact figures first, then Forbidden Planet's own calibration from declared
order totals, then a default estimate) - it's just scoped to that shop's own
data now, so a shop you've only used once won't skew the numbers for
Forbidden Planet or vice versa.

This is also where the review screen mentioned above adds real value -
now that a parsing mistake shows up on-screen before anything's saved,
rather than needing to be caught after the fact.

## Running it

```bash
docker compose up -d --build
```

Then visit `http://<server-ip>:8091`.

Data lives in `./data/pullcost.db` (SQLite) — back it up like you would any
other stack config.

### Upgrading from an earlier version

If you already had Ka-Ching! (née Pull Cost) running before, just replace the
code and rebuild — your existing tracked comics aren't touched. The database
upgrades itself automatically the first time it starts back up (new columns
and tables get added as needed), and everything you'd already imported stays
exactly as it was.

```bash
docker compose up -d --build
```

### Configuration

One environment variable, set in `docker-compose.yml`:

- `SHIPPING_ESTIMATE` — flat cost added per distinct release date within a
  month (default `4.00`). Change this to match what your retailer actually
  charges you per parcel.

## Importing your order history

1. Open your retailer's order history page (log in first).
2. Select all the text on the page (or as many pages as you want) and copy it.
3. Paste the whole thing into the textarea on the **Import** page here and
   hit Import.
4. Repeat monthly, or whenever you place new pre-orders — duplicates are
   silently skipped.

The parser is tolerant of pagination artifacts (page breaks that split an
item's price across two pages) — if an item's price genuinely gets lost in
the copy-paste, that one item is skipped rather than risk recording a wrong
figure. Everything else still imports fine.

Re-pasting the same page updates anything that's changed since (e.g. a
pre-order that's since shipped and been charged) — unless you've manually
ticked Paid or Cancel on it yourself, in which case your tick always wins.

### Release-date-change emails

Forbidden Planet emails you separately whenever a pre-order's release date
moves — a completely different format to the order-history page (no price,
no images, dates written as DD/MM/YYYY). You can paste one of these emails
into the same Import box and it'll update just that one item's date, without
needing a full re-import. The import banner will say "Release-date update: 1
item(s) updated" instead of the usual found/added/skipped line.

This only understands the one email template Forbidden Planet was sending as
of when this was built — if they change the wording or you spot one that
doesn't get picked up, paste an example and it can be adjusted.

## More than one shop

Ka-Ching! only knows how to *read* Forbidden Planet's pages — it can't parse
any other retailer's site. For anything else, use **+ Add** in the nav (which
also has the paste-in Import box further down the same page). Set the release
date and shop once, then add every comic from that same order as its own row
(name + price) before submitting — one shipment, one form, rather than
repeating the whole form per comic. Click any item's name on the dashboard to
edit those same details later, one at a time.

**eBay purchases from different sellers share one filter tab.** Each seller
still gets its own colour and label within the grouped item lists (so you can
tell "Sad Lemon Comics" apart from "Bearsgames" at a glance), but the shop
filter row shows a single combined "eBay" tab rather than one per seller,
which got unwieldy fast. Since eBay orders are typically already paid and
delivered, filtering to eBay on the dashboard's forward-looking views (This
Week, This Month) will usually turn up nothing due — a note there points to
Search instead, which covers your full purchase history across all time.

## Notifications

The **Settings** page sets up a daily check — once a day, Ka-Ching! looks at
what's releasing tomorrow and sends a single push through whichever service
you configure, grouped by shop, e.g. *"Tomorrow: 3 comics, £11.48 — Forbidden
Planet: 2 items, £8.49 · Cocktails and Comics: 1 item, £2.99."* Stays
completely silent on quiet days — no pointless daily pings when nothing's due.

Supports **ntfy**, **Gotify**, or **Telegram** — pick one from the dropdown,
fill in its details (server URL + topic for ntfy, server URL + app token for
Gotify, bot token + chat ID for Telegram), and set what time of day the check
should run.

Two buttons let you confirm it's actually working before relying on it:
- **Send test notification** — an immediate, generic ping, just to prove the
  connection details are right
- **Test tomorrow's digest now** — sends the real message you'd get
  tomorrow (or "nothing releasing" on a quiet day), without waiting for the
  scheduled time

This all runs inside the container itself — no cron job to set up, no
external scheduler. It just needs the container running once a day at the
time you pick.

## A few smaller extras

- **Monthly budget bar** — set a target on the Settings page and the
  dashboard shows a progress bar for how close this month's forecast total
  is to it, colour-shifting from green to amber to pink as you approach or
  pass it. Leave it blank to hide the bar entirely.
- **CSV export** — a "Download CSV" button on the Search page exports
  whatever's currently filtered (same name/shop/status/date-range filters)
  as a spreadsheet-ready file.
- **Search** — find any comic you've ever tracked, filterable by shop, paid
  status, or a custom date range, with sort options and a running total
  (spent / still due) for whatever's currently filtered — not locked to the
  current week or month like the dashboard.
- **All-time totals** — alongside "this year so far" on the dashboard,
  there's now a lifetime total across everything ever tracked.
- **Data backup and restore** — a "Download backup" button on the Settings
  page gives you the whole database as a single file, separate from GitHub
  (which never sees your real order data). A matching "Restore backup"
  upload puts it back — it checks the file is genuinely a Ka-Ching database
  before touching anything, and automatically keeps a safety copy of
  whatever was live just before a restore (sitting in `/data` as
  `pullcost.db.before-restore-<timestamp>`, not shown in the UI, but there
  if a restore ever needs undoing manually).
- **Install to home screen** — Ka-Ching! can be added to your phone's home
  screen like a normal app (look for "Add to Home Screen" or an install
  icon in your browser). Still the same container underneath, just opens
  full-screen with its own icon instead of a browser tab.
- **Awaiting charge** — a dashboard warning for anything whose release date
  has already passed but is still sitting unpaid and unmarked. Usually just
  a normal short delay before the retailer charges, but worth a look if
  something's been sitting there a while.
- **Version number** — the footer now shows which build is currently
  running, so "did the update actually land" is a glance instead of a
  guessing game.
- **Shop label always shows** — every item now shows which shop it's from,
  even when everything on a given day is from the same place. Previously
  this only showed up when a day had comics from more than one shop.
- **Chart tabs no longer reload the page** — clicking Week/Month/6M used to
  send you back to the top of the dashboard. All three versions of the
  chart now load at once (hidden until picked) and switching between them
  happens instantly in the browser, with no reload and no lost scroll
  position. Your last-picked range is also remembered between visits.

Once more than one shop is being tracked, a small filter row appears above
"This week" (All shops / each shop by name), and any day with releases from
more than one place shows each shop labelled separately with its own colour,
rather than one undifferentiated pile — so a Wednesday with both a
Forbidden Planet delivery and a Cocktails & Comics order due shows clearly
which is which.

## If something looks wrong

Every action that changes an item's status or release date — clicking the
paid circle, cancelling, undoing, a re-import refreshing something, a
release-date email updating something — gets logged, along with the exact
before/after state. If a duplicate warning reappears, or anything else looks
like it silently changed on its own, check:

```bash
docker logs pullcost
```

Look for lines starting `MARK request`, `MARK result`, `IMPORT REFRESH`,
`EMAIL DATE UPDATE`, or `DUPLICATE CHECK` — between them they show exactly
what happened to a given item and when, rather than needing to reconstruct it
from memory afterwards.

There's also a standing check on the dashboard for items tagged Forbidden
Planet with no order number at all — always a parser artifact from an
earlier version, never legitimate, since every real Forbidden Planet item
comes with an order number attached. If any show up under "Items with no
order number", they're almost certainly duplicates of something already
tracked properly; check before removing.

## Calendar view and export

The **Calendar** tab shows a compact month grid — release days are
highlighted with that day's total and issue count, kept deliberately simple
since a full title list doesn't fit a calendar cell without turning into a
mess. Full detail (every item, with the same tap-to-pay circles and cancel
&times; as the dashboard) lives in the "releases" list underneath the grid.
Navigate month to month the same way as the dashboard's shipment view.

The **Download .ics** button exports every upcoming (non-cancelled) release
as a calendar file, one event per release day rather than one per variant, so
importing it into Google Calendar, Apple Calendar, or Outlook doesn't flood
your calendar with near-duplicate entries. Only upcoming releases are
included, not your whole history, so it stays useful as a "what's coming out"
reminder rather than a log of everything you've ever ordered.

## Optional: a weekly/monthly nudge via ntfy

The app exposes a small JSON endpoint at `/api/summary`:

```json
{
  "week_total": 27.88,
  "week_item_count": 3,
  "month_total_estimate": 72.88,
  "month_item_count": 9,
  "month": "May 2026"
}
```

`scripts/ntfy-digest.sh` wraps this into an ntfy push. It needs `curl` and
`jq` (Aegis likely already has both; otherwise `apt install jq`).

```bash
export NTFY_TOPIC=pullcost          # required
export PULLCOST_URL=http://192.168.0.178:8091   # default shown, override if needed
export NTFY_URL=https://ntfy.sh     # default shown, point at your own ntfy server if self-hosted

./scripts/ntfy-digest.sh weekly     # "This week: £27.88 across 3 issue(s)."
./scripts/ntfy-digest.sh monthly    # "May 2026 forecast: £72.88 across 9 issue(s), incl. est. shipping."
```

Suggested crontab, run from wherever the script lives (e.g. alongside your
other scheduled jobs on Aegis):

```cron
0 8 * * 1   NTFY_TOPIC=pullcost /opt/stacks/pullcost/scripts/ntfy-digest.sh weekly
0 8 1 * *   NTFY_TOPIC=pullcost /opt/stacks/pullcost/scripts/ntfy-digest.sh monthly
```

This is left as a script you run yourself, rather than built into the
container, so the container keeps doing exactly one job — serving the
dashboard — reliably.

## What this deliberately doesn't do

- No collection cataloguing, no barcode scanning, no cover art
- No login/auth — put it behind your existing reverse proxy (NPM/Authelia)
  the same way as everything else, since it has no auth of its own
- No automatic scraping of the retailer site — paste-in only, so nothing
  breaks silently when a retailer changes their page markup
