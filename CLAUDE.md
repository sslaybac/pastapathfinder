# CLAUDE.md — pastapathfinder

Standing instructions for AI agents working in this repository. Read this file at the start of every session.

## What this project is

**pastapathfinder** — a static-analysis tool for legacy and AI-generated Python codebases (the name: finding paths through spaghetti code). Batch analysis pipeline producing a queryable index, plus a local interactive viewer. Flagship capability: sliced call traces (forward/backward from a selected entry point). Full definition: `specs/requirements.md`; full construction plan: `specs/design.md`.

## Repository map

- **`specs/`** — documents that govern *building* the product. Agents consult these; Do not modify files under specs/ on your own initiative. Spec files may be created or edited only when the user's prompt for this session explicitly directs it (e.g., producing specs/tasks.md, checking off a completed task, applying an approved amendment). Treat any other spec change as out of scope and flag it instead.
  - `specs/requirements.md` — the requirements baseline (WHAT the software must do). **APPROVED 2026-07-16; revised 2026-07-18** (engine-evaluation amendments: §6 item 10, FR-14/C-11, FR-44, FR-30 trace, OQ-1/OQ-7 resolutions). Source of truth for scope, behavior, and acceptance criteria.
  - `specs/design.md` — HOW the system is built. **APPROVED 2026-07-20.** Source of truth for architecture, component interfaces, schemas, and file formats; its §2 decision log records every technology choice with its evidence.
  - `specs/backlog.md` — deferred ideas with provenance and v1-insurance status. Not obligations; build nothing toward them beyond insurance already recorded in requirements.md.
  - `specs/tasks.md` — (arrives at workflow step 4) ordered, verifiable implementation tasks.
- **`prototypes/`** — empirical evidence from prototype/experiment sessions (currently: `prototypes/engine-eval/`, the FINDINGS files from the six-session engine evaluation, 2026-07-17/18). Entries are historical record: append new findings, never edit existing ones. Findings are *evidence cited by* `specs/` documents, never themselves normative — where a finding and a spec disagree, the spec governs and the disagreement is a rule-4 stop. Throwaway prototype *code* lives outside this repository and is not preserved here; only findings are.
- **`docs/`** — user-facing documentation only: content that ships with or is published for the product. The owed set is enumerated in design.md §6 (`report-formats.md`, `exit-codes.md`, `wsl.md` — the FR-31 Linux-filesystem condition, `configuration.md`, `install.md`).

**Boundary rule:** ships with or is published for the product → `docs/`. Exists to build the product → `specs/`. Empirical findings from prototype sessions → `prototypes/`. If a document doesn't clearly fit, ask the stakeholder; do not guess.

## Spec governance (binding on all agents)

1. **Re-read before working.** Before starting any task, re-read the requirement(s) and acceptance criteria it implements, plus §4's conventions block in requirements.md and the relevant design.md sections. Task descriptions summarize; the spec governs.
2. **Never modify `specs/` without explicit stakeholder approval in the current session.** This includes "helpful" fixes, reformatting, and renumbering. The same rule protects existing files under `prototypes/` (append-only; new findings are new files).
3. **Numbering is append-only.** FR/AC/EC/OQ/C/SA/B/D/R/O identifiers are stable and referenced across documents. New items get new numbers; retired items keep their IDs with a dated trace. Never renumber.
4. **Divergence is a stop, not a workaround.** If implementation reality contradicts the spec (a requirement is infeasible, an assumption fails, a revision trigger fires, a measured finding contradicts a spec statement), stop, state the conflict, and propose a spec amendment for approval. Never let code silently drift from the documents: stale specs are worse than no specs, because agents trust them.
5. **Approved spec changes are committed with dated traces**, in the style already used throughout (e.g., "*Resolved 2026-07-18*"), alongside the code they affect. For open questions (requirements.md §8), resolution is a two-sided update: the decision is recorded in the resolving document (normally `specs/design.md`) *and* the §8 entry is updated to point at it with a dated trace. An OQ whose answer exists only in a design section, a conversation, or someone's head is not resolved. (Still open this way: OQ-2 and OQ-4, both deliberately deferred to the viewer milestone — see design.md §8 O2/O3.)
6. **Scope discipline.** §6 of requirements.md lists non-goals: build nothing toward them, regardless of an item's long-term status. Backlog entries get no anticipatory accommodation. When in doubt whether something is in scope, it isn't — ask.
7. **Ambiguity is surfaced, never guessed.** If a requirement admits two readings, ask the stakeholder; this repository's documents were built on that rule.
8. **Planning and implementation happen in separate sessions.** Each workflow stage opens in a fresh session whose input is the committed documents, not prior conversation history. Implementing agents see only the finished spec — a spec that needs its drafting conversation to be understood has failed, and that failure should surface as a question (rule 7), not be papered over with remembered context.

## Coding conventions (per design.md §6; approved 2026-07-20)

- Implementation language & version: **Python; `requires-python >= 3.12`; developed and verified on 3.13.** The floor is load-bearing: mypy parses target code with the host interpreter's grammar, and older interpreters silently drop modern-syntax files (design.md D2).
- Pinned engine: **`mypy==2.3.0`, exact.** Never bump the pin casually — upgrades follow the D1a revalidation procedure (design.md §2), and the mypy internals the adapter may touch are the enumerated list in design.md §3.5. Code outside `src/pastapathfinder/adapters/python/` must never import `mypy.*` (AC-23.1; enforced by a unit test).
- Other runtime dependencies: `pathspec`, `flask`. Adding a dependency is a design change (rule 4), not a convenience.
- Code style / linter & formatter: **`ruff`** (lint and format; configuration in `pyproject.toml`).
- Test framework & test command: **`pytest`**, invoked as `pytest`. Determinism regression checks use `tests/regression/compare.py` (the FR-44 comparator: volatile fields stripped; in-variance-class diffs reported, never silently passed; everything else fails).
- Build / run commands: `pip install -e .` then `pastapathfinder <analyze|query|view> …`. Exit codes 0/1/2 per design.md D10.
- Commit message conventions: imperative-mood subject; body references the FR/AC (and design.md section) the change implements.

## Current state (update as stages complete)

- [x] Step 1 — Idea interrogation (`sa_tool_interview_summary.md`, 2026-07-13)
- [x] Step 2 — Requirements (`specs/requirements.md`, approved 2026-07-16; revised 2026-07-18)
- [x] Step 3 — Technical design (`specs/design.md`, approved 2026-07-20). Engine evaluation completed via six prototype sessions (evidence: `prototypes/engine-eval/`); OQ-1/OQ-7 resolved (mypy 2.3.0 / Python); OQ-2 and OQ-4 deliberately remain open until the viewer milestone.
- [ ] Step 4 — Task breakdown (`specs/tasks.md`): derive ordered, verifiable tasks from design.md, honoring the pipeline-before-viewer sequencing mandate (requirements §7) and design.md §8's open items (O5 benchmark-pin copy-through, O6 WSL verification pass as a test-plan item).
- [ ] Step 5 — Iterative execution
