# Security Policy

Ka-Ching! is a personal, self-hosted project maintained by one person in
their spare time. There's no dedicated security team, but reports are taken
seriously and looked at promptly.

## Reporting a vulnerability

Please use GitHub's [private vulnerability reporting](https://github.com/callum87-Lab/Ka-Ching/security/advisories/new)
for this repository rather than opening a public issue. This lets us discuss
and fix the problem before it's disclosed publicly.

You should expect an initial response within a few days. This is a hobby
project without paid support, so please be patient - there's no SLA, but
genuine reports won't be ignored.

## Supported versions

Only the latest release is supported. Older versions won't receive security
fixes - please update instead of reporting an issue against an old release.

## Scope

This is a self-hosted app you run on your own infrastructure, so most
traditional hosted-SaaS concerns don't apply. Relevant concerns include:
- Dependency vulnerabilities (tracked via Dependabot)
- Anything that could expose data beyond your own instance
- Any unexpected outbound network activity from the container
