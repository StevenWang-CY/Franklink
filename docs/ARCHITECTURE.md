# LangGraph Architecture (Current)

- Router graph with four flows: onboarding, recommendation, networking, general.
- Optional interaction-engine layer (feature-flagged) selects actions post-onboarding and can call graphs sequentially.
- Pending confirmations for profile updates and match proposals live in user personal_facts.
- No checkpoints; each message builds state from Supabase + incoming webhook, runs router, returns response.
- Onboarding: name -> school -> career interests; persists to Supabase; Photon reaction on name.
- Recommendation: Azure OpenAI + resources DB search; uses Zep memory when enabled.
- Networking: match users, present match to initiator, confirm, invite target, create group chat.
- Email context: read-only Gmail signals via Composio (DM-only connect), stored in personal_facts and Zep signals metadata.
- General: personality prompt + Zep context; fast acknowledgements for short thanks.
- Messaging: Photon only (typing, reactions, sends). No SendBlue, Google OAuth, BrightData, or Celery.
