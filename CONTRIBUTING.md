# Contributing to Franklink-iMessage

Thanks for your interest in contributing! This project started life as the Avalanche Team 1 × foundry _Start-Up In a Weekend_ Hackathon winner on the Hybrid Intelligence Track, and we're now iterating in the open.

This document explains how to get set up, the conventions we follow, and how to propose changes.

## Ground rules

- Be kind. All interactions are governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
- Never commit secrets. See [SECURITY.md](SECURITY.md). The `.env` file is `.gitignore`d for a reason — if in doubt, add the pattern to `.gitignore` before staging.
- Small, focused PRs land faster than big ones. If a change needs a design discussion, open an issue first.

## Getting set up

```bash
git clone https://github.com/<your-fork>/Franklink-iMessage.git
cd Franklink-iMessage

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
#  Fill in at minimum: Azure OpenAI, Supabase, Redis, Photon, Zep.
#  See the comments in .env.example for how to obtain each.

#  Apply the schema on a fresh Supabase project
#  (run the files in the order listed in db/MIGRATION_ORDER.md)

uvicorn app.main:app --reload
```

If you just want to run the test suite without a live Photon / Supabase:

```bash
pytest
```

Tests that need a live service are skipped automatically when their required env vars are missing.

## Branching and PRs

1. Fork the repo and create a branch off `main` — name it after the change, e.g. `feat/location-ranking`, `fix/empty-match-payload`.
2. Make your change. Keep the diff focused: one feature or fix per PR.
3. Run `pytest` and make sure nothing you touched regresses.
4. Open a PR against `main` with:
   - A short description of **what** changed and **why**
   - Screenshots or transcripts if the change affects Frank's messages
   - A note on any new env vars or DB migrations you added
5. A maintainer will review. Expect comments — we care about code being easy to reason about.

## Code style

- **Python**: target 3.11+. Prefer explicit types on public functions. We use `black`-style formatting (100 char lines).
- **Imports**: stdlib → third-party → local, separated by blank lines.
- **Logging**: use `logging.getLogger(__name__)`. Don't `print`.
- **Secrets**: always read from environment via `app.config`. Never hardcode.
- **New env vars**: add them to `.env.example` with a comment explaining where to get the value.
- **New DB tables / columns**: add a SQL file under `db/schema/` and append it to `db/MIGRATION_ORDER.md`.

## Testing

- Unit and integration tests live under `tests/`.
- Name test files `test_<subject>.py` and test functions `test_<behavior>()`.
- Prefer real integration against ephemeral test doubles over mocks that can drift from reality.
- If you add a new agent tool, add at least one test that exercises the routed path.

## Commit messages

One-line summary in the imperative mood, optional body explaining the why:

```
feat: rank matches by distance when both users share a city

Weights distance at 0.35 relative to semantic match score so that
local introductions surface above long-distance ones when quality is
comparable.
```

Common prefixes: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.

## Reporting bugs

Open an issue with:
- What you expected to happen
- What actually happened (logs / transcripts welcome — redact real phone numbers)
- Steps to reproduce
- Your environment: Python version, OS, whether Docker, relevant env flags

## Proposing features

Open a discussion or an issue first before writing a large patch. Describe the user story, the trade-offs you considered, and any breaking changes. This saves everyone time.

## Thank you

Every thoughtful issue, review, and PR makes Frank a little better at being a useful friend. We appreciate it.
