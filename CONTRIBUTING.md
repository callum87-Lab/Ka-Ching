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
- **Passes the automated checks**, once they exist for this repository.
- **Is signed off** on every commit (below).

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
