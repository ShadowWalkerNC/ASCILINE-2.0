# AGENTS.md — ASCILINE-2.0

> **Extends:** `ShadowWalkerNC/.github/AGENTS.md` — all global rules apply unconditionally.
> **Purpose:** Project-specific overrides and context for AI agents working in this repository.
> **Auto-loaded by:** Claude Code · GitHub Copilot · OpenAI Codex · Cursor · Windsurf

---

## Project Identity

```
Project:      ASCILINE-2.0
Description:  A high-performance, real-time ASCII video rendering engine. Streams binary-encoded frames via WebSockets for ultra-low latency, 30 FPS playback using HTML5 Canvas and requestAnimationFrame.
Status:       planning
Phase:        0 — Initial setup
Priority:     active
```

---

## Tech Stack

```
Language:     Detected: Unknown — update manually
Framework:    [e.g. React + Vite, Node.js, Android (Jetpack Compose)]
Database:     [e.g. Supabase (PostgreSQL), SQLite]
Hosting:      [e.g. Railway, Vercel, self-hosted]
Key APIs:     [e.g. Anthropic Claude, Stripe, Google Maps]
CI/CD:        [e.g. GitHub Actions, manual]
```

---

## Repository Structure

```
[paste top-level directory tree with one-line descriptions]

Example:
src/
  components/    ← React UI components
  lib/           ← shared utilities and helpers
  routes/        ← page-level route components
supabase/
  migrations/    ← all DB migrations (forward-only)
public/          ← static assets
```

---

## Key Files for Every Agent Session

```
ARCHITECTURE.md    ← system design, data flows, module responsibilities
TODO.md            ← current open work — read this every session
CHANGELOG.md       ← record of what changed and when
.env.example       ← required environment variables (no values)
```

---

## Active Agents for This Project

Default agents per AGENT_DISPATCH activation matrix. List any project-specific overrides:

```
Always active:  COHERENCE · SECURITY · DOCS
Project default on-demand: [list agents most commonly needed for this project]
Rarely needed:  [list agents to skip unless explicitly required]
```

Example for a React + Supabase app:
```
Always active:  COHERENCE · SECURITY · DOCS
Project default: ENGINEER · DATABASE · UX · QA
Rarely needed:  BUSINESS · AI (load only when working on AI features)
```

---

## Project-Specific Rules

List any rules that override or extend the global AGENTS.md for this project only.
Global Tier 1–3 rules cannot be overridden here.

```
[Example rules:]
- All Supabase migrations must be reviewed by DATABASE agent before any push.
- Components must follow the design system in /src/components/ui/.
- Do not touch the /supabase/seed.sql file without explicit permission.
- All API routes require auth middleware — no exceptions.
- Branch naming: feature/[ticket-id]-[short-description]
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `[VAR_NAME]` | Yes / No | [what it does] |

Never commit values. Always use `.env.example` for the key list.

---

## Current Phase Context

```
Phase goal:     [what this phase is trying to accomplish]
Definition of done: [how we know this phase is complete]
Blocking issues: [anything preventing progress]
Next phase:     [what comes after this]
```

---

## Known Issues / Watch List

List anything an agent should be aware of before working in this repo:

```
[Example:]
- The auth flow has a known race condition in offline mode — do not modify without reading issue #42.
- Migration 0004 has a manual data step — see ARCHITECTURE.md §3.2 before running.
- The scheduler module is tightly coupled to the timezone utility — changes to either affect both.
```

---

## Agent Confirmation for This Repo

After loading this file, the agent must add to its `DISPATCH CONFIRMED` block:

```
Project AGENTS.md: loaded
Project: [name]
Stack: [language + framework]
Phase: [current phase]
Project rules active: [count] overrides
Known issues noted: [yes | none]
```

---

*Template version: 1.0 | Extends: ShadowWalkerNC/.github/AGENTS.md | Copy to project repo root as AGENTS.md*


---
*Auto-generated: 2026-06-28 | Extends: ShadowWalkerNC/.github/AGENTS.md | Repo: [ASCILINE-2.0](https://github.com/ShadowWalkerNC/ASCILINE-2.0)*
*Action required: Fill in all placeholder fields before using this repo in a session.*
