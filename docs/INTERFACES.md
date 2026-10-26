# External interfaces

Every way data gets into or out of Ka-Ching!. The app listens on one HTTP
port (`8000` inside the container, mapped to `8091` in the example
`docker-compose.yml`) and makes no outbound connections unless you set up
notifications.

When the optional login is on (see the README, "Signing in"), every interface
below except the sign-in pages, static files, the phone sync API and the
private calendar feed link requires a signed-in session.

## 1. Web interface (browser)

HTML pages, used by people through a browser.

| Path | What it is |
| --- | --- |
| `/` | Dashboard |
| `/orders` | Orders, by shipment |
| `/calendar` | Calendar and releases list |
| `/search` | Search |
| `/items/new`, `/items/{id}/edit` | Log orders (paste or manual), edit an item |
| `/insights`, `/insights/spend-by-shop`, `/insights/price-creep`, `/insights/top-titles` | Insights |
| `/alerts` | Alerts and notification setup |
| `/settings` | Settings |
| `/login`, `/logout` | Optional sign-in |
| `/classic/...`, `/v2/...` | Old addresses - permanent redirects to the current pages |

**Form actions** (HTTP POST, sent by those pages): marking items paid,
bulk actions and delays (`/items/...`), paste-import preview and confirm
(`/import`, `/import/confirm`), dismissing duplicates and ghost items, and
everything saved in Settings (`/settings/...`: budget, categories, shops,
layout, security, notifications, backups and restores, factory reset).
These accept standard form or JSON bodies and redirect back to the page.

## 2. File downloads and uploads

| Path | Direction | Format |
| --- | --- | --- |
| `/settings/backup` | out | The whole database (SQLite file) |
| `/settings/backups/{file}` | out | One automatic daily backup |
| `/settings/restore-copies/{file}` | out | A copy saved just before a restore |
| `/settings/restore` | in | A Ka-Ching! backup file (SQLite), replaces all data |
| `/settings/export-all.csv` | out | Every tracked item as CSV |
| `/search/export.csv` | out | The current search results as CSV |
| `/settings/export-notifications.json` | out | Notification settings as JSON |
| `/settings/import-notifications` | in | Notification settings JSON |
| `/import` | in | Pasted text of a shop's order page |

## 3. Calendar feed

| Path | Format |
| --- | --- |
| `/calendar/export.ics` | iCalendar (`.ics`) of upcoming releases, one event per release day |

Calendar apps can subscribe to it (`webcal://`). While the login is on, the
subscription link carries a private key (`?key=...`) instead of needing a session.

## 4. JSON API

| Path | Method | Purpose | Authentication |
| --- | --- | --- | --- |
| `/api/summary` | GET | Small summary (this week, this month) for scripts and digests | Session, if login is on |
| `/api/sync` | POST | Two-way sync for the Android app | Sync key header. Disabled unless `KACHING_PHONE_SYNC=1` and sync is switched on in Settings |
| `/debug/shipping-groups` | GET | Shipping diagnostic | Only exists if `DEBUG_TOOLS_ENABLED=true`, and needs the sync key |

## 5. Outbound notifications (only if you set them up)

When a notification provider is configured in Alerts → Notifications, the app
sends HTTPS (or HTTP, for a local server) POST requests to **that provider
only**:

| Provider | Destination |
| --- | --- |
| ntfy | the ntfy server URL and topic you enter |
| Gotify | the Gotify server URL you enter, with your app token |
| Telegram | `api.telegram.org`, with your bot token and chat ID |
| Webhook | the URL you enter, with the message template you set |

Nothing else is ever contacted: no analytics, update checks, fonts or CDNs.

## 6. Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_PATH` | `/data/kaching.db` | Where the database lives |
| `SHIPPING_ESTIMATE` | `4.00` | Fallback shipping cost per parcel |
| `KACHING_PASSWORD` | unset | Optional master password for the login |
| `KACHING_PHONE_SYNC` | unset | Set to `1` to make phone sync available |
| `DEBUG_TOOLS_ENABLED` | unset | Set to `true` to enable the shipping diagnostic |

## 7. Files on disk

Inside the `/data` volume: the database (`kaching.db`, plus SQLite's `-wal`
and `-shm` files), automatic daily backups in `backups/` (last 7 kept), and
up to 3 `kaching.db.before-restore-...` safety copies.
