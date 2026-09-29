# Changelog

## v3.0.0 — new interface

A complete redesign of the web interface, plus categories and a lot of
fixes. Your data upgrades itself on first start; nothing needs doing by hand.

### The Android app is dormant

The [Android app](https://github.com/callum87-Lab/Ka-Ching-App) is on hold
until it can be reworked to match this release and fully re-tested. **Phone
sync is off and hidden by default** — set `KACHING_PHONE_SYNC=1` to bring it
back (unsupported until the app update ships). Your sync key is kept.

### New interface

- Dark neon redesign with a sidebar layout, now at the normal addresses
  (`/`, `/orders`, `/calendar`, `/search`, `/items/new`, `/insights/…`,
  `/alerts`, `/settings`)
- The old interface is kept at **`/classic/`** for this release only, and
  will be removed in the next one
- New pages: **Orders** (by shipment, All / Unpaid & cancelled), **Alerts**
  (awaiting charge, duplicates, ghost items, history, notification setup)
- Insights split into **Overview**, **Spend by shop**, **Price creep** and
  **Top titles**
- A full **phone layout**: slide-out menu, section pills under the page
  title, shop dropdowns, readable chart labels, tables as one block per row
- The sidebar **budget box** stays in view while scrolling, with a choice of
  what it shows (spent of budget, left/over, percentage, daily allowance,
  spent + still due, or hidden) and turns pink when you're over
- Toggles, tabs and ranges are remembered between visits
- Consistent buttons and pills throughout (same hover, selected and disabled
  behaviour everywhere)
- Installable to a phone home screen, as before

### Categories

- Every item has a category, with a starter list and your own on top; each
  gets its own colour, and "has series" is set per category in Settings
- Pick a category when logging orders (per paste or per item)
- **Spend by category** on Insights; category chips on Price creep and Top
  titles
- Existing items become Comics; category is in backups and both CSV exports

### Insights

- **Price creep now uses cover price** (the cheapest copy of each issue), so
  variants no longer look like huge price rises; a new **Variants cost
  extra** column shows the average variant mark-up. Replaces the old
  sparklines
- **Cumulative spend** redesigned: spent so far vs budget, what's still due
  this month, and a bar per release day
- Calendar: tap a day to show only its releases

### Fixes

- Backups (download, daily automatic, and the pre-restore safety copy) could
  miss the most recent changes; they now always include everything
- Restoring a backup now can't be affected by leftover database files, and
  restoring an older backup upgrades it immediately
- Items with no release date or placed date no longer break the Orders page
- Items synced from the app no longer arrive without a category
- Orders: the selected shop filter now shows as selected
- Dashboard: the budget ring shows how much you're over, not a capped 100%
- "Alert-free streak" replaced with honest open / resolved / last-resolved
  counts
- The default landing page setting applies to the new interface

## v2.0.0

Shared shop colours and shipping default via sync, and the note-wiping sync
fix. See the git history for details.
