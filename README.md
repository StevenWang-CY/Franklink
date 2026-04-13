# Franklink-iMessage

> **Avalanche Team 1 × foundry Start-Up In a Weekend Hackathon 2025 — First Prize, Hybrid Intelligence Track.**

Franklink is an AI professional-networking concierge that lives inside iMessage. Users talk to **Frank** the way they'd text a friend; Frank understands what they need, matches them with other users who can help, and drops them into a small group chat where he keeps the collaboration moving.

It's the first chat that becomes your startup. The first chat where interview questions actually get shared. The first chat with the people grinding finals with you at 2 a.m.

---

## What Frank does

| Capability | What users experience | How it works |
|---|---|---|
| **Need & value matching** | "I need someone who's shipped a Next.js app" → Frank introduces you to one. | A two-tier LLM pipeline extracts demands and offerings from every message, embeds them, and matches against other users' profiles. |
| **Email-context intelligence** | Frank knows you're going to an AI conference because your ticket email said so. | Read-only Gmail signals via Composio are summarized into searchable highlights and stored alongside each user's profile. |
| **Location-aware introductions** | "Anyone near Berkeley this weekend?" actually finds people near you. | Handles + coordinates are resolved per-user; the matcher weighs distance as a ranking feature. |
| **AI-seeded group chats** | Frank creates a three-way chat with a tailored icebreaker already written. | Once both users accept, Frank provisions a group, generates a context-rich opener, and stays in the chat to nudge, summarize, and follow up. |
| **Proactive outreach** | Frank circles back a day later to check in, never feels spammy. | Background workers reason over unresolved connections and inactivity windows and decide whether a follow-up adds value. |

## Architecture at a glance

Two LangGraph agents, a stateless orchestrator, and a handful of background workers.

```
iMessage  ──►  Photon webhook  ──►  FastAPI  ──►  Orchestrator
                                                      │
                                                      ├─► InteractionAgent  (conductor — routes intent, drafts payloads)
                                                      │         │
                                                      │         └─► ExecutionAgent (worker — validates and executes tools)
                                                      │
                                                      ├─► Supabase   (users, conversations, matches, group chats)
                                                      ├─► Zep        (long-term memory + semantic signals)
                                                      ├─► Redis      (idempotency, rate-limit, job queue)
                                                      └─► Azure OpenAI
```

Flows active in the router:

- **Onboarding** — name → school → career interests, with Photon reactions and contact-card exchange
- **Networking** — match candidates on need / value / location, confirm with initiator, invite target, seed group chat
- **Recommendation** — books, videos, and resources from a curated catalog, ranked against Zep memory
- **General** — casual chat with fast-path acknowledgements when no deeper action is required

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the longer version and [docs/FRANK_SYSTEM_PROMPT.md](docs/FRANK_SYSTEM_PROMPT.md) for Frank's full persona and mode system.

## Stack

- **Runtime**: Python 3.11, FastAPI, Uvicorn, Supervisor (background workers)
- **Agents**: LangGraph (no checkpoints — stateless per turn, state rebuilt from DB + webhook)
- **Models**: Azure OpenAI (`gpt-5-mini` for interaction and reasoning)
- **Data**: Supabase (Postgres + pgvector), Zep, Redis
- **Messaging**: Photon iMessage bridge
- **Integrations**: Composio (Gmail), Stripe (helpers retained), BrightData / Scrapingdog (optional LinkedIn enrichment)

## Quick start

Prerequisites: Python 3.11+, Docker (optional), a Supabase project, an Azure OpenAI deployment, a Photon account, and a Zep workspace.

```bash
git clone https://github.com/<you>/Franklink-iMessage.git
cd Franklink-iMessage

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
#  ↑ fill in Azure OpenAI, Supabase, Redis, Photon, Zep, Composio (others optional)

#  Apply the database schema (see db/MIGRATION_ORDER.md for the correct order)
psql "$POSTGRES_CONNECTION_STRING" -f db/schema/user_profiles_table.sql
#  … repeat for every file listed in db/MIGRATION_ORDER.md

uvicorn app.main:app --reload
```

Docker Compose is also provided:

```bash
docker compose up --build
```

## Repository layout

```
app/              FastAPI app, agents, tools, integrations, workers
db/
  MIGRATION_ORDER.md    Run these SQL files in this order on a fresh DB
  schema/               Supabase schema and RPC definitions
docs/
  ARCHITECTURE.md       LangGraph flow overview
  EMAIL_EXTRACTION.md   Composio-based Gmail extraction pipeline
  FRANK_SYSTEM_PROMPT.md   Frank's persona and mode system
  UPDATE_TASK.md        Update flow ("dumb executor" pattern)
infrastructure/
  supervisor/           Supervisord config for the container
scripts/          One-off operational scripts (Zep migrations, query experiments)
tests/            Pytest suite (group chat, location flow, etc.)
```

## Development

```bash
pytest                    # run the test suite
pytest tests/groupchat    # scope to one package
```

## Security

If you believe you've found a vulnerability, please do **not** open a public issue. See [SECURITY.md](SECURITY.md) for how to report it privately.

## Contributing

Contributions are welcome — please read [CONTRIBUTING.md](CONTRIBUTING.md) first. All contributors are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Franklink
