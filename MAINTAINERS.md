# Maintainers

Ka-Ching! has a single maintainer.

| Name | GitHub | Role |
| --- | --- | --- |
| Callum | [@callum87-Lab](https://github.com/callum87-Lab) | Owner and sole maintainer |

## Access to sensitive resources

Only the maintainer has access to:

- **The repository** - admin rights: settings, branch protection, merging pull requests
- **Releases** - creating tags and publishing GitHub releases
- **Security reports** - private vulnerability reports and security advisories
- **Dependabot and secret scanning alerts**
- **The OpenSSF Best Practices entry** for this project (bestpractices.dev project 15163)

There are no other collaborators, and no CI/CD secrets or signing keys held by
anyone else. If that changes, this file is updated in the same pull request.

## Giving someone more access

Nobody is given access beyond opening issues and pull requests until they
have been reviewed:

1. **A track record first:** several merged pull requests over at least
   three months, reviewed by the maintainer, showing they follow
   [CONTRIBUTING.md](CONTRIBUTING.md) and the security policy.
2. **Identity and security checked:** a GitHub account with two-factor
   authentication on, and agreement to the [security policy](SECURITY.md).
3. **The smallest role that does the job:** GitHub's *Triage* role before
   *Write*; *Maintain* or *Admin* only if truly needed. Every role, with its
   date, is listed in this file in the same pull request that grants it.
4. **Reviewed every year,** and removed promptly when no longer needed.

## Roles and responsibilities

As owner and sole maintainer, Callum is responsible for:

- **Code review** - every change reaches `main` through a pull request
  (direct pushes to `main` are blocked by a branch ruleset)
- **Releases** - versioning, release notes, and keeping [CHANGELOG.md](CHANGELOG.md) up to date
- **Security** - responding to vulnerability reports as set out in
  [SECURITY.md](SECURITY.md), publishing advisories, and keeping dependencies patched
- **Contributions** - reviewing issues and pull requests against
  [CONTRIBUTING.md](CONTRIBUTING.md)
- **Documentation** - keeping the README and the project documents accurate
