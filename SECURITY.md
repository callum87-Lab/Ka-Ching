# Security Policy

Ka-Ching! is a personal, self-hosted project maintained by one person in
their spare time (see [MAINTAINERS.md](MAINTAINERS.md)). There's no dedicated
security team, but reports are taken seriously and looked at promptly.

## Reporting a vulnerability

Please report vulnerabilities **privately** using GitHub's
[private vulnerability reporting](https://github.com/callum87-Lab/Ka-Ching/security/advisories/new)
for this repository, not by opening a public issue. This goes straight to the
maintainer and lets the problem be fixed before it's made public.

Please include what you found, how to reproduce it, the version you tested,
and what an attacker could do with it.

## What happens next (coordinated disclosure)

| Step | Timeframe |
| --- | --- |
| Your report is acknowledged | within **7 days** |
| The issue is confirmed (or explained if it isn't one) | within **14 days** |
| A fix, or a plan with a date, for a confirmed issue | within **30 days** of confirming it |
| Public disclosure | once a fixed release is out, or after **90 days** at most - agreed with you |

You'll be kept up to date along the way, and credited in the advisory if you'd
like to be.

## How vulnerabilities are published

Every confirmed vulnerability is published as a
[GitHub Security Advisory](https://github.com/callum87-Lab/Ka-Ching/security/advisories)
for this repository once a fix is released, describing the affected versions,
the impact, and the version that fixes it. The fix is also listed in
[CHANGELOG.md](CHANGELOG.md) and the release notes.

## Supported versions

Only the latest release is supported. Older versions won't receive security
fixes - please update instead of reporting an issue against an old release.

## Scope

This is a self-hosted app you run on your own infrastructure, so most
traditional hosted-SaaS concerns don't apply. Relevant concerns include:
- Anything that lets someone change or read your data without permission
  (including through the optional login, forms, uploads or restores)
- Dependency vulnerabilities (tracked via Dependabot)
- Anything that could expose data beyond your own instance
- Any unexpected outbound network activity from the container

The interfaces the app exposes are listed in [INTERFACES.md](INTERFACES.md).
