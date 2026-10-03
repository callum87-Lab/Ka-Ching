# Security assessment

What's worth protecting in Ka-Ching!, the ways it could realistically be
attacked, how likely and how damaging each one is, what already defends
against it, and what's still to do. Written from the code itself and
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

## Threats

Likelihood and impact are rated Low / Medium / High for a typical home setup.

| # | Threat | Likelihood | Impact | What defends against it now | Status |
| --- | --- | --- | --- | --- | --- |
| T1 | Someone else on the network views or changes your data | Medium | Medium | Optional login (Settings → Security): hashed password, signed cookies (HttpOnly, SameSite=Lax, Secure over HTTPS), five-attempt lockout, sign-out everywhere on password change; `KACHING_PASSWORD` for recovery | **Mitigated when the login is on.** Off by default, which the README explains |
| T2 | A malicious website makes your browser submit Ka-Ching! forms (cross-site request forgery) - e.g. delete items or reset data | Medium | High | With the login on, the SameSite=Lax cookie means cross-site form posts arrive signed out and are refused | **Gap when the login is off.** Planned: a per-session token on every form, so forged posts are refused either way |
| T3 | A crafted name (from a pasted page or a forged form) runs script in your browser (stored cross-site scripting) | Medium | High | Every page escapes names; data placed inside scripts escapes `<`, `>` and `&`; chart tooltips and legends escape names before display. Automated test covers all pages | **Fixed in v3.2** (Insights and Spend by shop were vulnerable before) |
| T4 | A crafted link sends you from Ka-Ching! to another site (open redirect) | Low | Low | Every "go back to" address is checked to be a page on Ka-Ching! itself, including the old-address redirects | **Fixed in v3.2** |
| T5 | A crafted paste ties the app up (slow-pattern denial of service) | Low | Medium | Parser patterns rewritten so they can't backtrack; pastes over 1 MB are cut short; tests feed 100,000-character attacks and require under 2 seconds | **Fixed in v3.2** |
| T6 | A crafted or huge backup file breaks the database or fills the disk on restore | Low | High | Restores check the file is a valid SQLite database before use, copy it in through SQLite's backup API, keep a copy of the previous data first, and upgrade old formats | **Partly mitigated.** Planned: an upload size limit |
| T7 | Pages framed by another site, or content sniffing | Low | Low | - | **Gap.** Planned: security headers (Content-Security-Policy, frame-ancestors, X-Content-Type-Options, Referrer-Policy) |
| T8 | Keys or tokens leaked from settings or backups | Low | Medium | Data never leaves your machine; keys are long random values; backups are only downloadable by whoever can use the app | **Accepted.** Tokens are stored as-is because the app must use them; protect the `/data` volume and backups like any other private file |
| T9 | A vulnerable or compromised dependency | Low | High | Four direct dependencies, pinned; Dependabot alerts; `pip-audit` on every pull request; GitHub Actions pinned to exact commits; CodeQL on every pull request; signed release checksums | **Mitigated.** v3.2 upgraded Starlette, clearing 7 known vulnerabilities |
| T10 | The app exposed directly to the internet | Low | High | The login, plus HTTPS-aware cookies | **Advice:** don't expose it directly; use a VPN or a reverse proxy with HTTPS and the login on |
| T11 | Something internal shown to visitors in an error | Low | Low | Incomplete forms get a normal error page; debug tools are off unless `DEBUG_TOOLS_ENABLED=true` and need the sync key | **Mitigated** |

## What's still to do (planned for v3.2's security review)

1. **Cross-site form protection (T2):** a per-session token on every form
   and action.
2. **Security headers (T7).**
3. **Upload size limits (T6)** on restores and imports.
4. **A full OWASP ASVS Level 1 review**, with results recorded here.

## Tools that check this continuously

- **CodeQL** (GitHub code scanning) on every pull request and weekly.
- **pip-audit** and **Dependabot** for known vulnerabilities in dependencies.
- **Secret scanning and push protection** on the repository.
- **OpenSSF Scorecard** for how the repository is run.
- **The automated test suite**, including tests for every fixed issue above.
