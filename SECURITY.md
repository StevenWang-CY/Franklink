# Security Policy

## Reporting a vulnerability

If you believe you've found a security vulnerability in Franklink-iMessage, please report it privately so we can investigate and fix it before it is disclosed publicly.

**Please do not open a public GitHub issue for security reports.**

Report by email to **security@franklink.ai** with:

- A description of the issue and the impact you believe it has
- Steps to reproduce (proof-of-concept code, HTTP requests, logs — anything helpful)
- Your name / handle if you'd like to be credited in the fix's release notes

We aim to acknowledge reports within **3 business days** and to ship a fix or mitigation within **30 days** for high-severity issues. If you don't hear back, feel free to nudge us.

## Scope

In scope:
- The code in this repository (FastAPI app, agents, tools, background workers)
- Database schema under `db/schema/` (RLS gaps, RPC authorization issues)
- Default configurations we ship in `.env.example`, `Dockerfile`, `docker-compose.yml`, and the supervisor config

Out of scope (report to the upstream provider instead):
- Vulnerabilities in Azure OpenAI, Supabase, Photon, Zep, Composio, Stripe, Redis Cloud, or any other managed dependency
- Social-engineering attacks against Franklink staff or users
- Denial-of-service via commodity traffic flooding
- Missing security headers on surface area that was never intended to be user-facing (e.g. `/health`)

## Safe-harbor

We will not pursue legal action against researchers who:
- Make a good-faith effort to avoid privacy violations, data destruction, and service disruption
- Report the issue to us before disclosing it publicly
- Do not exploit the issue beyond the minimum needed to demonstrate impact

## Handling secrets

If you discover credentials, tokens, or other secrets accidentally committed to this repository (or to any fork or mirror), please report it the same way as a vulnerability and **do not** open a PR that demonstrates the exposure. We will rotate the credentials, remove them from history, and credit you in the advisory.

## Supported versions

Only the `main` branch is supported. We do not back-port security fixes to older tags.
