# Security assessment

A threat model and attack surface analysis for Ka-Ching!: what's worth
protecting, every way in, the ways it could realistically be attacked, how
likely and how damaging each one is, what defends against it, and what's
still to do. Written from the code itself and
re-checked with every release.

- **Version assessed:** v3.2 (in development)
- **Last reviewed:** 3 October 2026
- **How it's kept current:** this file is reviewed before each release, and
  whenever a vulnerability report, CodeQL alert or Dependabot alert comes in.
  Reports go through [SECURITY.md](SECURITY.md).

## What it is, in one paragraph

A self-hosted web app in one Docker container, normally reached over plain
HTTP on a home network, keeping everything in one SQLite file. It has no
user accounts beyond an optional single password, makes no outbound
connections except to a notification service you set up, and loads nothing
from the internet. See [ARCHITECTURE.md](ARCHITECTURE.md).

## What's worth protecting

| Asset | Why it matters |
| --- | --- |
| Your order data | What you've bought, from where, for how much, and when - personal financial information |
| Being able to use and trust it | Wrong totals or lost items defeat the point of the app |
| The login | The password (stored as a salted PBKDF2-SHA256 hash, 240,000 rounds) and the signing secret for sign-in cookies |
| Keys in the settings | The calendar feed key, the phone sync key, and any notification service token (e.g. a Telegram bot token) |
| Backups | Complete copies of all of the above |

## Who could attack, and how

1. **Someone else on your network** opening the app in a browser.
2. **A malicious website you visit**, making your browser send requests to
   Ka-Ching! behind your back.
3. **Crafted content you bring in yourself**: a pasted order page, a
   backup file, or names inside them.
4. **The software supply chain**: a compromised or vulnerable dependency or
   build step.
5. **Someone who can reach it from the internet**, if it's exposed beyond
   the home network.

## Attack surface and critical paths

Everything that accepts input from outside the app (full list in
[INTERFACES.md](INTERFACES.md)), and the code paths that matter most if
something goes wrong:

| Entry point | Who can reach it | Critical path it leads to | Main protections |
| --- | --- | --- | --- |
| Web pages and forms (`/`, `/items/...`, `/settings/...`) | Anyone on the network; the login limits this to the owner | Changing or deleting items; changing settings; factory reset | Optional login, cross-site request blocking, server-side checks on every value, escaping of everything shown |
| Paste import (`/import`, `/import/confirm`) | As above | `parser.py` reading untrusted text; saving items | 1 MB cap, patterns that can't backtrack, a review screen before anything is saved, duplicate skipping |
| Backup restore (`/settings/restore`) | As above | Replacing the whole database | Size limit, SQLite header check, SQLite backup API, a copy of the previous data kept first |
| Sign-in (`/login`) | Anyone on the network | Gaining a session | Hashed password, 12+ characters, common passwords refused, lockout after 5 tries, server-side sessions |
| Calendar feed (`/calendar/export.ics`) | Calendar apps | Reading release dates (read-only) | Private key in the link while the login is on |
| Phone sync API (`/api/sync`) | The Android app | Creating and changing items | Off unless enabled twice (environment variable and setting); sync key required |
| Outbound notifications | Only the service you configure | Sending messages out | Only ever contacts the configured provider |
| Dependencies and the build | The supply chain | Everything | Pinned versions, pip-audit, dependency review, CodeQL, pinned actions, signed releases with an SBOM |

The threat model below follows each of these paths: who could misuse it,
how, and what stops them.

## Threats

Likelihood and impact are rated Low / Medium / High for a typical home setup.

| # | Threat | Likelihood | Impact | What defends against it now | Status |
| --- | --- | --- | --- | --- | --- |
| T1 | Someone else on the network views or changes your data | Medium | Medium | Optional login (Settings → Security): passwords of 12+ characters, common passwords refused, hashed; signed cookies (HttpOnly, SameSite=Lax, Secure over HTTPS) tied to sessions recorded on the server, so signing out ends a session for good; five-attempt lockout; every session ended on password change; a push notification when login details change; `KACHING_PASSWORD` for recovery | **Mitigated when the login is on.** Off by default, which the README explains |
| T2 | A malicious website makes your browser submit Ka-Ching! forms (cross-site request forgery) - e.g. delete items or reset data | Medium | High | Every form post and background save from another website or subdomain is refused, using the browser's Sec-Fetch-Site and Origin headers; with the login on, the SameSite=Lax cookie adds a second layer | **Fixed in v3.2**, with or without the login |
| T3 | A crafted name (from a pasted page or a forged form) runs script in your browser (stored cross-site scripting) | Medium | High | Every page escapes names; data placed inside scripts escapes `<`, `>` and `&`; chart tooltips and legends escape names before display. Automated test covers all pages | **Fixed in v3.2** (Insights and Spend by shop were vulnerable before) |
| T4 | A crafted link sends you from Ka-Ching! to another site (open redirect) | Low | Low | Every "go back to" address is checked to be a page on Ka-Ching! itself, including the old-address redirects | **Fixed in v3.2** |
| T5 | A crafted paste ties the app up (slow-pattern denial of service) | Low | Medium | Parser patterns rewritten so they can't backtrack; pastes over 1 MB are cut short; tests feed 100,000-character attacks and require under 2 seconds | **Fixed in v3.2** |
| T6 | A crafted or huge backup file breaks the database or fills the disk on restore | Low | High | Restores check the file is a valid SQLite database before use, copy it in through SQLite's backup API, keep a copy of the previous data first, and upgrade old formats; backups over 100 MB, settings files over 1 MB and any request over 110 MB are refused | **Fixed in v3.2** |
| T7 | Pages framed by another site, content sniffing, or data left in caches | Low | Low | Content-Security-Policy (nothing loads from other sites; no framing), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy; `Cache-Control: no-store` on every page, export and backup; HSTS when reached over HTTPS; the server software isn't announced | **Fixed in v3.2** |
| T8 | Keys or tokens leaked from settings or backups | Low | Medium | Data never leaves your machine; keys are long random values; backups are only downloadable by whoever can use the app | **Accepted.** Tokens are stored as-is because the app must use them; protect the `/data` volume and backups like any other private file |
| T9 | A vulnerable or compromised dependency | Low | High | Four direct dependencies, pinned; Dependabot alerts; `pip-audit` on every pull request; GitHub Actions pinned to exact commits; CodeQL on every pull request; signed release checksums | **Mitigated.** v3.2 upgraded Starlette, clearing 7 known vulnerabilities |
| T10 | The app exposed directly to the internet | Low | High | The login, plus HTTPS-aware cookies | **Advice:** don't expose it directly; use a VPN or a reverse proxy with HTTPS and the login on |
| T11 | Something internal shown to visitors in an error | Low | Low | Incomplete forms get a normal error page; debug tools are off unless `DEBUG_TOOLS_ENABLED=true` and need the sync key | **Mitigated** |

## OWASP ASVS Level 1

v3.2 was reviewed against the OWASP Application Security Verification
Standard 4.0.3, Level 1. Everything that applies is met, including:
passwords of at least 12 characters, common passwords refused, a strength
meter and show-password option, notification when login details change,
lockout after repeated failures, sessions that end for good on sign-out,
cross-site request protection, output escaping, parameterised SQL, upload
limits, security headers, no caching of personal data, and no debug pages.

Not applicable to a single-password, self-hosted app: multi-factor and
one-time-password sections, account recovery questions, and user
registration. Transport security (TLS) is the deployment's job: use a
reverse proxy with HTTPS if Ka-Ching! is reachable beyond your home network.

## Tools that check this continuously

- **CodeQL** (GitHub code scanning) on every pull request and weekly.
- **pip-audit** and **Dependabot** for known vulnerabilities in dependencies.
- **Secret scanning and push protection** on the repository.
- **OpenSSF Scorecard** for how the repository is run.
- **The automated test suite**, including tests for every fixed issue above.
