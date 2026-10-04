# Contributing to Ka-Ching!

Thanks for wanting to help. Ka-Ching! is a small project with one maintainer
(see [MAINTAINERS.md](MAINTAINERS.md)), so contributions are welcome but are
reviewed carefully to keep it simple, private and reliable.

## Before you start

- **Found a bug?** Open an [issue](https://github.com/callum87-Lab/Ka-Ching/issues)
  with what happened, what you expected, and how to reproduce it.
- **Found a security problem?** Don't open an issue - follow [SECURITY.md](SECURITY.md).
- **Want to add something?** Open an issue first to talk it through, so you
  don't spend time on something that won't fit.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make your change, keeping it focused on one thing.
3. Run it locally with `docker compose up -d --build` and check the pages you
   touched, on a desktop and a phone-sized window.
4. Commit with a sign-off (`git commit -s`, see below).
5. Open a pull request to `main` describing what changed and why. Screenshots
   help for anything visual.

## What makes a contribution acceptable

A pull request is merged when it:

- **Keeps your data yours.** No analytics, tracking, telemetry, external
  fonts, CDNs or scripts. The app must make no network calls except ones the
  user has set up (their notification provider).
- **Doesn't break existing data.** Database changes must upgrade older
  databases automatically and keep old backups restorable.
- **Works on phones and desktops.** Pages must fit a phone screen with no
  sideways scrolling.
- **Matches the rest of the app.** Same look, same wording style (plain
  English, no jargon), and the same size for buttons and pills.
- **Adds no new dependencies** unless they're clearly needed - see
  "Dependencies" in the README.
- **Includes tests** for what it adds or fixes, and **passes every
  automated check** (tests, CodeQL, dependency review, sign-off).
- **Is signed off** on every commit (below).

## Tests

**When they run:** automatically on every pull request and every change to
`main`, by the **Tests** workflow (`.github/workflows/tests.yml`). A pull
request can't be merged until they pass. The **CodeQL**, **Dependency
review** and **Sign-off** checks run at the same time.

**Running them yourself** (Python 3.12):

```bash
pip install --require-hashes -r requirements-dev.txt
python -m pytest
```

They take a few seconds and never touch real data - each test uses its own
throwaway database. The same command also runs in CI, plus
`pip-audit -r requirements.txt` for known vulnerabilities in dependencies.

**Tests are required for changes.** Every pull request that adds a feature,
changes behaviour or fixes a bug must add or update tests in `tests/` that
cover it - a bug fix should include a test that fails without the fix.
Pull requests that only change documentation or styling are the exception.

**Changing dependencies:** edit `requirements.in` (or `requirements-dev.in`),
then regenerate the locked files with
`pip-compile --generate-hashes requirements.in` (and the same for
`requirements-dev.in`) - never edit the `.txt` files by hand.

## Sign-off (Developer Certificate of Origin)

Every commit must be signed off, which states that you have the right to
submit the work under the project's licence (AGPL-3.0), as set out in the
[Developer Certificate of Origin](https://developercertificate.org/).
Add the sign-off by committing with `-s`:

```bash
git commit -s -m "Fix the calendar total on empty days"
```

which adds a line like `Signed-off-by: Your Name <you@example.com>` to the
commit message.

## Adding a shop parser

Ka-Ching! reads order pages from a few shops directly. The README section
"Contributing a new shop's parser" explains what's needed - mainly real,
anonymised example pages from that shop - and how parsers are tested.
