# Changelog

## v3.4.0 - fits what you collect

Ka-Ching! started with comics, and plenty of it still assumed you collect
comics. This release makes it fit whatever you collect: cards, figures,
vinyl, films, games or a mix. New installs get a short setup guide, and a
round of fixes from a full audit makes the everyday jobs quicker.

### What you collect
- **A setup guide on new installs.** What you collect, whether you
  pre-order, which pages to show, currency and budget, theme and layout, and
  an optional password. Every step can be skipped, and you can run it again
  from Settings -> Help. Existing installs never see it unless they open it.
- **What you track** (Settings -> Appearance) replaces the three layout
  presets. Tick what you collect and whether you pre-order, and the pages
  that don't fit are switched off for you: Price creep only shows for
  numbered things like comics and manga, Top titles is hidden if nothing
  you collect comes in sets or series, and the pre-order cards go if you
  don't pre-order. You can still switch any card back on, and **Reset
  cards to preset** puts things back.
- **Price creep and Top titles get their own on/off switch** ("Show this
  page") in Customise and in Settings -> Appearance. A hidden page also
  leaves the sidebar.
- **A type for every category:** Comics, Manga and books, Trading cards,
  Figures and toys, Vinyl and music, Films and TV, Video games or Something
  else, set in Settings -> General -> Categories. The type decides the
  words used: a **Set** for trading cards, a **Line** for figures, an
  **Artist** for vinyl, and so on. Existing categories get a type from
  their name (you can change it), and new ones you type in get a guess.
- **A Series / Set box** on Log orders (both tabs), the import review
  screen and Edit item, worded to match the category. For comics and manga
  it's worked out from the title as before, so you can leave it empty. It
  suggests names you've used before, and is included in both CSV exports.
- **Top titles groups by Series / Set**, with its headings and wording
  following what you collect ("Top sets by total spend" for cards, and so
  on). Its category chips start with every category except "Something
  else".
- **Gold / silver is no longer a starter category** on new installs. If you
  already have it, it stays.

### Orders, Calendar and Alerts
- **A floating bar for bulk select** that stays in view as you scroll:
  Mark paid, **Delay** (+30, +60 or +90 days), Cancel and Remove. Esc or
  the ✕ clears the selection, and each action says what it did.
- **Calendar:** a **Day / Whole month** switch replaces "Show whole month",
  so it's always clear which you're looking at.
- **Alerts: "Fine as it is".** Items with no order number or no release
  date can be marked as fine, so they stop showing up. They're listed under
  "Marked as fine", where you can bring any of them back.
- "Ghost items" are now called **items with no order number**, and the
  Alerts summary only lists the kinds of alert you actually have.

### Smaller changes
- **Typing a new category** in a category box: the "Add ..." row comes first
  and is highlighted, and **+ New category...** stays at the top of the list.
- **Insights -> Overview:** the 10 releases card loses its title and just
  has **Recent**, **Most expensive** and **Lowest** tabs, and the "Edit
  priciest item" link is gone.
- **Where each series sits** (Price creep) shows the same hover box as the
  charts: first and latest price, change and category.
- **Log orders** tabs are now **Paste an order** and **Add by hand**. Figures say
  "item" rather than "issue", and "Busiest day of the week" is clearer
  about what it means.
- **The sidebar budget box** sits at the very bottom, below Collapse.
- Text boxes have a clearer border and focus ring in every theme, and
  date pickers and scroll bars match light or dark themes properly.

### Fixes
- **Shipping was counted too many times for orders with several release
  dates.** An order's real postage (from a pasted order page) was added to
  every parcel in it, so a pre-order spread over several weeks could show
  its whole postage again on each one. It's now shared across the order's
  parcels: matched one-to-one when there's a figure per parcel, otherwise
  split evenly. Still due, budgets, shipping figures and charts that use
  real postage will come out lower and correct.
- The Dashboard's **year so far** and **all time** figures now include
  shipping, like every other total.
- Price creep only offers categories with **Numbered issues** switched on,
  so things like Funko Pops with a "#" in the name no longer show up as a
  series; manga is counted in volumes.
- Series typed in capitals (common in eBay listings) group with the same
  series written normally.
- Top titles names every type in a mixed collection ("Series / Set /
  Artist"), and the price buckets on Overview use your currency.
- The Alerts page shows open alerts first, with the history underneath,
  and dates read "15 Sep" rather than "09-15" everywhere.
- Failed notifications show why they failed.
- The 12-month trend line no longer dips below zero between quiet months.
- Search filters, the Delay buttons, the calendar legend, shop rows and
  alert rows could run off the edge of narrow screens; the This week list
  could cut off its last link on a phone.
- Wording tidied throughout: "due" instead of "left of" when nothing's
  paid yet, clearer labels on Spend by shop and Price creep, shorter help
  text, and fewer repeated figures.
- Leftover mentions of the removed `/classic/` interface.

## v3.3.0 - a calmer, premium look

A restyle release. Every page has been redesigned to be quieter and easier
to read: no neon glows, no shouting capitals, big figures in their own
typeface, and four themes to choose from. Nothing about how your data is
tracked or added has changed.

### New look
- **Four themes** in Settings -> Appearance: **Sand** (the new default, a
  warm light theme), **Grey**, **Blue** and **Noir**. Each has its own set of
  accent colours, all checked for contrast, so text and charts stay readable
  whichever you pick. Warning colours (over budget, unpaid) never change.
  If you were on the White theme from the test builds, you're moved to Sand.
- **A new logo:** the "K!" monogram, with a matching app icon, favicon and
  home-screen icon.
- **Fonts are bundled with the app** (Inter for text, Instrument Serif for
  headline figures, both under the SIL Open Font Licence). Nothing is loaded
  from outside your server.
- **Shop colours follow the theme**, so each shop's colour suits whichever
  theme you're on (the Android app's API keeps its fixed colours).
- **Calmer pages throughout:** no glows, icon badges or all-capitals labels,
  plain underlined tabs instead of pills, and one set of corner sizes,
  spacing and hover/press effects used everywhere.
- Text contrast checked and raised where needed in every theme.

### Dashboard
- **A new "This month" summary** at the top: what's still due, how that
  sits against your budget, and the single biggest thing still to come.
- **Key figures** underneath: this year, all time, still on order, and what
  needs attention (tap it to open Alerts).
- **The Spending chart** (was Spend trend) now shows **paid** as solid bars
  and **still due** as hatched, with your budget as a line. Switch to a
  **line** style with the button by the range tabs; your choice is
  remembered on each device.
- The Backup card is folded into Alerts, and "Still due & budget", "Your
  spending" and "Biggest still to come" are replaced by the new summary.
  A Dashboard card order you'd saved goes back to the default once, so the
  new cards don't end up at the bottom.

### Insights
- **Spend by shop:** shop spend over time is now stacked bars (one bar per
  month, one segment per shop), and the share-of-spend donut shows the
  total in the middle.
- **Price creep:** **Where each series sits** shows every series as a dot on
  one scale, so you can see at a glance which are climbing; the extra-spend
  chart is a stepped running total starting from the first price rise.

### Sidebar and layout
- **A collapsible sidebar:** shrink it to a slim strip of icons, and it
  stays that way on that device. Search now sits in the sidebar.
- **Settings sits at the bottom** of the sidebar, and its sections
  (General, Data backup, Appearance, Security, About, Help) are tabs at the
  top of the Settings page. Lines join each section to its sub-pages.
- **Compact** replaces the Three columns layout: tighter spacing and smaller
  figures so more fits without scrolling, on any screen size. If you were
  using Three columns, you're moved to Compact.

### Sign-in page
- Redesigned, with a Show button for the password, a **Caps Lock** warning,
  how many tries are left before the short lock, and a countdown while
  locked.

### Fixes
- The chart's hover line could get in the way of the mouse and make the
  tooltip flicker.

## v3.2.0 — safe, sound and checked

A tidy-up and security release: everything was audited - the code, the
pages on real data, and security against OWASP ASVS Level 1 - and every
problem found was fixed. No new features to learn; things just work
better and more safely.

### Security
- **Fixed: names could run script code (stored XSS).** An item, shop or
  category name containing script code ran on the Insights and Spend by
  shop pages. Every name is now escaped everywhere, including inside charts,
  tooltips and legends.
- **Forms can't be submitted from other websites** (cross-site request
  forgery protection), with or without the login.
- **Every "go back to" address stays on Ka-Ching!** - crafted links could
  previously redirect to another site (including via old `/v2/` and
  `/classic/` addresses).
- **Crafted pastes can't tie the app up:** the import reader's patterns were
  rewritten so they can't slow down on malicious text (one took over 25
  seconds; now 0.02s), and pastes are capped at 1 MB.
- **Size limits:** backups up to 100 MB, settings files 1 MB, any request
  110 MB.
- **Security headers on every response:** a content security policy, no
  framing, no sniffing, no caching of personal data, a referrer policy, and
  HSTS when reached over HTTPS. The server no longer announces its software.
- **Stronger login:** passwords of 12+ characters (existing ones keep
  working), the most common passwords refused, a strength bar and Show
  button, sessions recorded on the server so **signing out ends a session
  for good**, every other session ended on a password change, a
  notification when login details change, and the cookie marked secure
  over HTTPS.
- **Framework upgrade:** FastAPI 0.142 / Starlette 1.7 / Uvicorn 0.54,
  clearing 7 known vulnerabilities in the old Starlette.

### Fixes
- Importing the same order twice no longer crashes; items already tracked
  are marked on the review screen and skipped.
- The word "None" no longer appears as a note (50 existing ones are
  cleared automatically).
- The general paste reader no longer puts lines like "Order Number: ..." or
  "Qty: 1" into item names.
- Prices must be between £0 and £100,000; an unreadable date no longer
  quietly becomes today's.
- A form missing something shows a normal page with a way back, not
  technical text.
- Money shows thousands separators everywhere (£1,274.86).
- Chart tooltips sit beside the cursor in every layout (they drifted on
  wide screens).
- The Price creep tooltip shows the right year; Insights' figures and shop
  toggles no longer run off a phone screen.
- The budget alert now follows your budget cycle and rollover, like the
  sidebar.

### Under the hood
- **Faster pages:** one database connection per page (was 14 to 21) and
  60 to 80% fewer queries.
- The code is split into clear parts, old leftovers from the v2 interface
  removed, and shared calculations used everywhere so figures always agree.
- **An automated test suite** (120 tests) runs on every change, alongside
  CodeQL code scanning, dependency vulnerability and licence checks, and a
  sign-off check.

### Project
- **Signed releases:** each release comes with a software bill of materials
  and a checksums file, signed by GitHub - see
  [Verifying a release](SECURITY.md#verifying-a-release).
- New documents: [ARCHITECTURE.md](ARCHITECTURE.md),
  [SECURITY-ASSESSMENT.md](SECURITY-ASSESSMENT.md),
  [INTERFACES.md](INTERFACES.md), [MAINTAINERS.md](MAINTAINERS.md), and
  fuller [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
- OpenSSF Best Practices: Baseline Level 1 and Level 2.

### Upgrading
- If you use the login, you'll be asked to sign in once more.

## v3.1.0 — make it yours

### Customise every page
- **⚙ Customise** on Dashboard, Calendar and the four Insights pages: show or
  hide any card from a strip across its top (nothing on the card is
  covered), and **drag cards into your own order** with the ⠿ handle (mouse
  or touch; holding near the screen edge scrolls). Half-width cards pair up;
  a lone one widens to fill the row. **Reset order** per page.
- **Settings → Layout**: every card on every page in one place, with
  *Everything*, *I don't pre-order* and *Just the essentials* presets.
- Layout and order are saved on the server, so every device matches.

### Wide landscape monitors
- Settings → Layout → **On wide screens** (1,900px+ only; phones, tablets,
  laptops and portrait screens are unaffected): **Standard** (centred),
  **Three columns** (three cards to a row, full width) or **Side panel** (a
  sticky "At a glance" column on every page: this week, alerts, biggest
  still to come).

### Budget by category
- Optional **category limits** per budget cycle (Settings → General).
- New **Budget by category** Dashboard card: one bar split into category
  colours, limits in the legend, pink when a category's over.
- Insights → Cumulative spend's budget bar splits by category too.
- Over-limit categories show in Alerts, with a push (once per category per
  cycle) if budget alerts are on. Shipping is shared across a parcel's
  categories by price, so categories always add up to the budget figure.

### Optional login
- **Settings → Security**: turn a login on, change the password, or turn it
  off. Stored hashed; "keep me signed in for 30 days"; Sign out in the
  sidebar; five wrong tries lock sign-in for a minute.
- Optional `KACHING_PASSWORD` in docker-compose: a master password that keeps
  the login on and is the recovery route if you forget yours.
- Calendar subscriptions keep working through a private link while the login
  is on.

### Smaller things
- **/** jumps to search from anywhere (desktop).
- **Alerts** now flags items with **no release date** (with a *Set date*
  button) and categories over their limit.
- **Before-restore copies** are listed in Settings → Data backup (newest 3
  kept, older ones tidied up automatically).
- Dashboard: "Year so far" and "Year in review" merged into one **Your
  spending** card; **This week** says "Nothing due" instead of £0.00 of
  £0.00; the **Backup** card offers to turn daily backups on when they're off.
- Calendar: each day in the releases list shows items + shipping, matching
  the calendar's figure.
- Price creep: **Typical increase** is now the median, and each series has a
  category-coloured dot.
- "Has series" is now **Numbered issues**, with a hint saying what it does.
- Every pill-shaped button and tab is the same size everywhere, and every
  expand/collapse arrow is now a clear round chevron button.
- Better "Install app" support on phones (the app's worker now covers every
  page).

### Removed
- The old interface at **/classic/**, as announced in v3.0. Old /classic/
  links open the same page in the current interface.

## v3.0.0 — new interface

A complete redesign of the web interface, plus categories and a lot of
fixes. Your data upgrades itself on first start; nothing needs doing by hand.

### The Android app is dormant

The Android app (its repository is now private) is on hold
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
