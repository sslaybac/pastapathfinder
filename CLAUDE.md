# CLAUDE.md — pastapathfinder

Standing instructions for AI agents working in this repository. Read this file at the start of every session.

## What this project is

**pastapathfinder** — a static-analysis tool for legacy and AI-generated Python codebases (the name: finding paths through spaghetti code). Batch analysis pipeline producing a queryable index, plus a local interactive viewer. Flagship capability: sliced call traces (forward/backward from a selected entry point). Full definition: `specs/requirements.md`.

## Repository map

- **`specs/`** — documents that govern *building* the product. Agents consult these; agents do not modify them (see Governance).
  - `specs/requirements.md` — the requirements baseline (WHAT the software must do). **APPROVED 2026-07-16.** Source of truth for scope, behavior, and acceptance criteria.
  - `specs/backlog.md` — deferred ideas with provenance and v1-insurance status. Not obligations; build nothing toward them beyond insurance already recorded in requirements.md.
  - `specs/design.md` — (arrives at workflow step 3) HOW the system is built.
  - `specs/tasks.md` — (arrives at workflow step 4) ordered, verifiable implementation tasks.
- **`docs/`** — user-facing documentation only: content that ships with or is published for the product. Currently owes two items from the requirements: the WSL Linux-filesystem condition (FR-31) and the structured report format documentation (FR-42/AC-42.4).

**Boundary rule:** ships with or is published for the product → `docs/`. Exists to build the product → `specs/`. If a document doesn't clearly fit either, ask the stakeholder; do not guess.

## Spec governance (binding on all agents)

1. **Re-read before working.** Before starting any task, re-read the requirement(s) and acceptance criteria it implements, plus §4's conventions block. Task descriptions summarize; the spec governs.
2. **Never modify `specs/` without explicit stakeholder approval in the current session.** This includes "helpful" fixes, reformatting, and renumbering.
3. **Numbering is append-only.** FR/AC/EC/OQ/C/SA/B identifiers are stable and referenced across documents. New items get new numbers; retired items keep their IDs with a dated trace. Never renumber.
4. **Divergence is a stop, not a workaround.** If implementation reality contradicts the spec (a requirement is infeasible, an assumption fails, a revision trigger fires — e.g., FR-30's clause), stop, state the conflict, and propose a spec amendment for approval. Never let code silently drift from the documents: stale specs are worse than no specs, because agents trust them.
5. **Approved spec changes are committed with dated traces**, in the style already used throughout (e.g., "*Resolved 2026-07-16*"), alongside the code they affect. For open questions (requirements.md §8), resolution is a two-sided update: the decision is recorded in the resolving document (normally `specs/design.md`) *and* the §8 entry is updated to point at it with a dated trace. An OQ whose answer exists only in a design section, a conversation, or someone's head is not resolved.
6. **Scope discipline.** §6 of requirements.md lists non-goals: build nothing toward them, regardless of an item's long-term status. Backlog entries get no anticipatory accommodation. When in doubt whether something is in scope, it isn't — ask.
7. **Ambiguity is surfaced, never guessed.** If a requirement admits two readings, ask the stakeholder; this repository's documents were built on that rule.
8. **Planning and implementation happen in separate sessions.** Each workflow stage opens in a fresh session whose input is the committed documents, not prior conversation history. Implementing agents see only the finished spec — a spec that needs its drafting conversation to be understood has failed, and that failure should surface as a question (rule 7), not be papered over with remembered context.

## Coding conventions — TBD

The implementation language is deliberately undecided until OQ-1/OQ-7 resolve (see `specs/requirements.md` §8). Do not assume Python, Node, or any other stack for the *tool's own* implementation. This section is filled when `specs/design.md` is approved:

- Implementation language & version: **TBD (OQ-7)**
- Code style / linter & formatter: **TBD**
- Test framework & test command: **TBD**
- Build / run commands: **TBD**
- Commit message conventions: **TBD**

## Current state (update as stages complete)

- [x] Step 1 — Idea interrogation (`sa_tool_interview_summary.md`, 2026-07-13)
- [x] Step 2 — Requirements (`specs/requirements.md`, approved 2026-07-16)
- [ ] Step 3 — Technical design: first activities are the language-neutral analyzer interface + index-schema skeleton, then the timeboxed OQ-1/OQ-7 engine prototype on the reference machine (Django core = performance benchmark; pandas = dynamism/robustness benchmark)
- [ ] Step 4 — Task breakdown (`specs/tasks.md`)
- [ ] Step 5 — Iterative execution
