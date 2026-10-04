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

## Supported versions and how long they're supported

| Version | Security fixes | Other fixes |
| --- | --- | --- |
| The latest release (e.g. 3.2.x) | Yes | Yes |
| The release before it (e.g. 3.1.x) | Yes, for **90 days** after the newer release comes out, then no more | No |
| Anything older | No | No |

When a version stops getting security fixes, that's noted in the
[CHANGELOG](CHANGELOG.md) and the next release's notes. Upgrading is always
`git pull` and `docker compose up -d --build`, and your data carries over,
so the advice is simply to stay on the latest release.

## Verifying a release

Every release from v3.2.0 onwards is built by GitHub Actions from the tagged
code, not on anyone's computer, and comes with three files on its
[releases page](https://github.com/callum87-Lab/Ka-Ching/releases):

- `Ka-Ching-<version>.tar.gz` - the source code
- `Ka-Ching-<version>-sbom.cdx.json` - the software bill of materials (every package it installs, at its exact version)
- `Ka-Ching-<version>-SHA256SUMS` - the fingerprints of the two files above

**1. Check the files haven't changed** (integrity). In the folder you
downloaded them to:

```bash
sha256sum -c Ka-Ching-v3.2.0-SHA256SUMS
```

Both lines must say `OK`.

**2. Check who built them** (authenticity). Each file is signed with a
GitHub artifact attestation, which records exactly which repository and
workflow produced it. With the [GitHub CLI](https://cli.github.com/):

```bash
gh attestation verify Ka-Ching-v3.2.0.tar.gz --repo callum87-Lab/Ka-Ching \
  --signer-workflow callum87-Lab/Ka-Ching/.github/workflows/release.yml
```

It must report a verified attestation from the `release.yml` workflow of
`callum87-Lab/Ka-Ching`. Anything else means the file didn't come from this
project's release process - don't use it. All attestations are also listed at
<https://github.com/callum87-Lab/Ka-Ching/attestations>.

## Dependency and code scanning policy

Every pull request is checked automatically, and can't be merged while a
check fails:

- **Tests** - the test suite, plus `pip-audit` against every Python
  dependency (fails on any known vulnerability).
- **Dependency review** - fails if a pull request adds or updates a
  dependency with a known vulnerability of *moderate* severity or worse,
  one flagged as malware, or one whose licence isn't on the allowed list
  (MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0, LGPL, GPL-3.0, AGPL-3.0 and
  similar open-source licences compatible with this project's AGPL-3.0).
- **CodeQL** (static analysis, SAST) - fails on any new security finding of
  *high* severity or worse, or any error-level finding.

**How quickly findings are fixed** (from when they're known):

| Severity | Dependency (SCA) findings | Code scanning (SAST) findings |
| --- | --- | --- |
| Critical | 7 days | 7 days |
| High | 14 days | 14 days |
| Moderate / medium | 30 days | 30 days |
| Low | Next release | Next release |

A licence finding is fixed by replacing or removing the dependency before
the next release.

**Before every release:** no release is made while any known critical, high
or moderate dependency vulnerability, licence violation, or high or critical
code scanning finding is open - unless it has been recorded as not affecting
Ka-Ching! in the VEX file below.

**Findings that don't affect Ka-Ching!** - for example, a vulnerability in
part of a library this app never uses - are recorded with the reason in
[`vex.openvex.json`](vex.openvex.json) (OpenVEX format), and dismissed in
GitHub with the same reason, so the decision is public and checkable.

## Secrets and credentials

- **The project holds no long-lived secrets.** GitHub Actions use only the
  short-lived token GitHub creates for each run, limited to read-only unless
  a job needs more (signing releases). There are no stored repository
  secrets, deploy keys or API tokens.
- **Nothing secret goes in the repository.** GitHub secret scanning and push
  protection are on, so a password or key accidentally committed is
  blocked or flagged straight away.
- **Access:** only the maintainer has write or admin access (see
  [MAINTAINERS.md](MAINTAINERS.md)), protected by GitHub two-factor
  authentication.
- **If a secret is ever exposed**, it's revoked and replaced immediately,
  the history is cleaned if needed, and affected users are told through a
  security advisory.
- **Rotation:** if a long-lived secret is ever added (for example a
  publishing token), it must be stored as an encrypted GitHub Actions
  secret, scoped to the one job that needs it, and rotated at least every 12
  months and whenever anyone with access leaves.
- **Your own instance's secrets** (the login password, calendar and sync
  keys, notification tokens) live only in your database; see
  [SECURITY-ASSESSMENT.md](SECURITY-ASSESSMENT.md) for how they're
  protected.

## Scope

This is a self-hosted app you run on your own infrastructure, so most
traditional hosted-SaaS concerns don't apply. Relevant concerns include:
- Anything that lets someone change or read your data without permission
  (including through the optional login, forms, uploads or restores)
- Dependency vulnerabilities (tracked via Dependabot)
- Anything that could expose data beyond your own instance
- Any unexpected outbound network activity from the container

The interfaces the app exposes are listed in [INTERFACES.md](INTERFACES.md).
