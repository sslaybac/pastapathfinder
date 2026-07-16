# pastapathfinder — Backlog

Ideas deferred from v1. This file is a memory, not a specification: an entry here records intent and context so a future planning pass (human or agent) can find it, interrogate it, and specify it properly at that time. Nothing in this file creates a v1 obligation.

**Routing rule** (from the requirements review, 2026-07-15): if building v1 with no knowledge of an idea would make the idea materially more expensive later, the idea's *insurance* belongs in `requirements.md` (as an FR, SA, or §6 exclusion note) — the idea itself still lives here. If v1 ignorance costs nothing, the idea lives only here.

**Entry fields:** What — the idea in a sentence or two. Origin — what prompted it. Depends on — open questions, requirements, or events that gate it. V1 insurance — what, if anything, v1 does to keep this cheap (with requirement references), or explicitly "none." Notes — priority signals, risks, source references.

**Exit rule** (adopted 2026-07-16): entries leave this file in exactly two ways — *promoted* into a release's requirements process (the entry is replaced by a one-line dated trace pointing at the requirements it became), or *deleted* with a one-line dated note of why. A backlog that only grows becomes noise; origin dates make it easy to prune entries that survive several releases without being missed.

---

## B-1 — Readers/writers pivot (attribute and module-level variable access)

- **What:** Statically resolved "find all readers/writers of an attribute or module-level variable," as a manual pivot when call chains go cold.
- **Origin:** Interview summary; named the nearest mitigation for the absence of data-flow analysis. The summary notes v1's call slices may go cold at shared-mutable-state bugs — the exact pathology of the originating project.
- **Depends on:** *C-6 resolved 2026-07-15: confirmed out of v1; expected to be among the first backlog items implemented.* Needs a schema addition: variable/attribute nodes and read/write edge types — design these against the OQ-1 engine's *actual* resolution capabilities (what it can and cannot resolve for `self.x` through inheritance, `setattr`, properties, module-attribute aliasing), not from the armchair; this is the same prototype-first discipline that deferred OQ-1 itself.
- **V1 insurance:** FR-21's generic-schema discipline and FR-39 (schema version identifier, promoted 2026-07-15) make the schema addition the *first planned schema migration* rather than a rework. No v1 behavior anticipates it. Until implemented, the documented manual workaround (requirements.md §6 item 1) is: identify candidate writers by text search, backward-slice each.
- **Notes:** Top follow-on candidate, now by decision rather than default. First thing to interrogate after v1 ships. Scope warning from the C-6 discussion: done honestly, attribute resolution under Python dynamism needs its own over-approximation and ambiguity posture (FR-14's equivalent for accesses) — budget it as a feature the size of the call graph, not a patch.

## B-2 — Full data-flow / taint analysis

- **What:** Track how values propagate through the program, not just which functions call which.
- **Origin:** Interview summary; acknowledged as the highest-value capability overall.
- **Depends on:** Research-grade effort; engine capabilities (OQ-1 outcome constrains feasibility); B-1 is the incremental step toward it.
- **V1 insurance:** None, deliberately — the summary defers it as out of reach for a solo/portfolio v1.
- **Notes:** Long-term ambition. Revisit only after B-1 proves the demand.

## B-3 — UML class diagrams

- **What:** Class-diagram artifacts, plausibly by wrapping pyreverse under the unified-presenter thesis.
- **Origin:** Interview summary; deferred to an early follow-on, explicitly not indefinitely.
- **Depends on:** Nothing structural; low-effort orchestration once the pipeline exists.
- **V1 insurance:** None needed; the orchestration architecture (FR-23 adapter pattern) is the natural attachment point.
- **Notes:** Cheap win for the portfolio narrative ("unified presenter of existing analyses").

## B-4 — Standalone data/control-flow graph artifacts

- **What:** Per-function control-flow and data-flow graph outputs as first-class artifacts.
- **Origin:** Interview summary; user-ranked highest-value non-v1 feature.
- **Depends on:** Engine capabilities (OQ-1); presentation decisions in the viewer.
- **V1 insurance:** None.
- **Notes:** Ranked above configuration analysis by the stakeholder.

## B-5 — Configuration analysis

- **What:** Analysis of configuration files and their relationship to code behavior.
- **Origin:** Interview summary; ranked next after B-4 among follow-on candidates.
- **Depends on:** Scoping interrogation — "configuration analysis" was never specified.
- **V1 insurance:** None.
- **Notes:** Needs its own idea-interrogation session before any specification.

## B-6 — Ambiguity / technical-debt report

- **What:** A report treating call-resolution ambiguity itself as a technical-debt signal: where the codebase's dynamism defeats static resolution, ranked or grouped for refactoring attention.
- **Origin:** Interview summary ("ambiguity itself is treated as a technical-debt signal and may later become its own report").
- **Depends on:** *Dependency satisfied 2026-07-15:* SA-3 was promoted as requirements FR-40, so per-edge ambiguity data is recorded at analysis time and this report is a query over the existing index, requiring no re-analysis.
- **V1 insurance:** FR-40 (promoted). This entry is now purely a presentation/report feature.
- **Notes:** Fits the tech-debt survey workflow (US-2) naturally.

## B-7 — Changed-code review surface (broad form)

- **What:** A user-facing view presenting what changed in the codebase between analysis runs, aimed at reviewing AI-agent edits when the user doesn't know which files were touched.
- **Origin:** Review discussion of US-4 (2026-07-13); was SA-7 in requirements.md. *Disposition final 2026-07-15: deferred; this entry is its permanent home.*
- **Depends on:** FR-35's re-analysis report already contains most of the raw data; the remaining work is diff sourcing and viewer presentation. The trigger-decision concern raised at disposition was split off as B-21 (staleness check), with B-19 as its viewer surface — this entry retains only the *review* use case.
- **V1 insurance:** FR-35 (promoted) — the pipeline records what was re-processed and why, so this becomes largely a presentation layer.
- **Notes:** Main cost is viewer scope — the project's highest-mortality component. Partially duplicated by version control; the non-redundant part is dependent-of-change visibility.

## B-8 — Partial analysis of files with syntax errors

- **What:** Analyze the well-formed regions of syntactically broken files (via an error-recovering parser) instead of whole-file skip, introducing a third "partially analyzed" file status with identified unresolved regions.
- **Origin:** Review question about IDE behavior on broken files (2026-07-15); currently SA-8 in requirements.md, pending disposition.
- **Depends on:** OQ-1 — the selected engine must have an error-recovering parser. Requires propagating the third status through FR-7's reconciliation, the index, and all presentations.
- **V1 insurance:** *Disposition final 2026-07-15: SA-8 deferred; this entry is its permanent home.* Insurance inventory from the deferral review: (1) report format-version field — AC-42.4, added as this feature's specific accommodation, so the fourth file status is a versioned format change; (2) index-side needs are already covered by existing generic mechanisms — the file-status attribute rides FR-21's attribute fields, unresolved-region spans reuse FR-37's span vocabulary, per-file keying (FR-24) is unaffected, and the index schema change is an FR-39 version bump; (3) FR-43's exit-code semantics should be *designed* (design-stage note) in terms of "fully analyzed vs. not" so a fourth status maps without recoding; (4) "has an error-recovering parser" is a recorded OQ-1 tiebreaker. No other v1 requirement needs anticipatory change.
- **Notes:** The hard part is presentation honesty (gaps loudly reported), not parsing. v1's whole-file skip is the deliberate conservative floor.

## B-9 — AI-consumable artifacts / vector embeddings

- **What:** Convert the analysis and code breakdown into vector embeddings or other AI-consumable forms.
- **Origin:** Stakeholder review comment (2026-07-13) revising the original "explicitly not the point" stance to "possible long-term direction."
- **Depends on:** Several release stages out by stakeholder statement; undefined use case.
- **V1 insurance:** None, explicitly — requirements.md §6 item 5 states no v1 requirement anticipates or prepares for it. A future planner should budget for zero existing accommodation.
- **Notes:** The recorded decision *not* to insure is itself the important context here.

## B-10 — Additional language support: Java, JavaScript/Angular

- **What:** Implement analyzer adapters, entry-point detectors, and exclusion convention sets for Java and JavaScript/Angular — the proven-pain targets.
- **Origin:** Interview summary; the motivating case for the entire extensibility architecture.
- **Depends on:** v1 shipping; OQ-1's adapter interface being real; expect a schema-revision pass (the summary's accepted cost: agnostic-schema decisions made against one language will be partly wrong).
- **V1 insurance:** Extensive and deliberate — FR-21 (generic schema), FR-22 (namespaced IDs), FR-23 (adapter boundary), FR-8 (pluggable detectors), FR-2 (per-language convention sets), plus the §6 anti-requirement barring a speculative plugin framework.
- **Notes:** When this starts, first task is the schema-revision review the summary predicts, *before* writing the second adapter.

## B-11 — Cross-language boundary analysis

- **What:** Edges across language boundaries — the Java-backend/Angular-frontend case (e.g., HTTP route on one side, HTTP call on the other).
- **Origin:** Interview summary.
- **Depends on:** B-10 (at least two languages implemented).
- **V1 insurance:** FR-22's language-namespaced node IDs exist specifically so cross-language edges are a schema addition, not a migration.
- **Notes:** Do not attempt to design the edge semantics until both sides exist.

## B-12 — Editor jump-to-source from the viewer

- **What:** Open a selected node's source location directly in the user's editor from a trace view.
- **Origin:** Review discussion of FR-37 (2026-07-15); explicitly drawn as the v1 boundary line — display the location (AC-27.3), don't jump to it.
- **Depends on:** A decision to soften §6 item 9 (IDE/editor integration exclusion); per-editor protocol handling (file:line URI schemes vary).
- **V1 insurance:** FR-37 — nodes carry root-relative path and line spans, which is all a jump needs.
- **Notes:** Small feature, large workflow payoff for US-1; natural first crack in the IDE-integration exclusion if user demand appears.

## B-13 — macOS support

- **What:** macOS as a supported platform.
- **Origin:** Interview summary; excluded solely because no test environment is available, despite the audience including macOS developers.
- **Depends on:** Access to a test environment.
- **V1 insurance:** None formal; the practical insurance is incidental (Python-ecosystem tooling and a local web viewer tend toward portability).
- **Notes:** Revisit if the portfolio audience makes it matter or an environment becomes available.

## B-14 — In-viewer source display

- **What:** On mouseover or click of a node, UML class, or other visible element, display the referenced code in-viewer with syntax highlighting, so the user can examine relevant code without the overhead or focus shift of switching to an editor.
- **Origin:** Stakeholder backlog addition (2026-07-15), extending the FR-37/AC-27.3 discussion.
- **Depends on:** FR-37 location data plus read access to the source files at view time — which raises one design question B-12 doesn't have: whether the viewer reads source from disk (contents may have drifted from the index since analysis; interacts with EC-15-style staleness) or from source text captured into the index at analysis time (always consistent, but grows the index and the FR-24 incremental surface). Also the first feature to put source *text* rather than graph data in the viewer, and the first with a syntax-highlighting dependency (per-language, so the highlighter choice should follow the per-language pattern, not hardcode Python).
- **V1 insurance:** FR-37 (path + line spans) is sufficient; no additional v1 accommodation needed under the read-from-disk design. If the capture-into-index design is preferred, that decision should be made before the index schema settles — worth noting when resolving OQ-1/OQ-2.
- **Notes:** Convenience-based alternative to B-12, not a replacement — B-12 remains for when the user wants to edit or navigate beyond the displayed span. Complements rather than competes: display-in-place for examination, jump-out for action. Does not breach §6 item 9 (IDE/editor integration), since no editor is involved.

## B-15 — Whole-program graph rendering

- **What:** Render the full call graph of the codebase as a visual artifact. v1 treats the spaghetti hairball as a failure mode to avoid (EC-6, FR-28); this entry reframes it as evidence: a visual of the tangle is a strong management-facing justification for refactoring investment, and to a degree helps locate technical-debt pain points directly.
- **Origin:** Stakeholder review comment on EC-6 (2026-07-15).
- **Depends on:** Viewer maturity (rendering and layout at whole-program scale is precisely the problem that made v1 exclude it — layout algorithms and rendering-library limits at ~100k-line graph sizes need evaluation, likely alongside OQ-2's library choice or its successor decision). Design consideration recorded up front: the audience for this artifact is persuasion and triage, not navigation — aggregate presentations (module-level rollups, edge-density heatmaps, cluster coloring) may serve the purpose better than a raw node-edge render, and a static exportable image may matter more than interactivity for the show-it-to-management use.
- **V1 insurance:** None needed. The index already contains the entire graph — slicing is a query-time restriction, not a storage one — so this is purely a presentation feature. FR-28 says full-graph rendering is "not required" and "never the default"; it does not prohibit it, so no requirements change is needed to unlock this later.
- **Notes:** Natural companion to B-6 (ambiguity/tech-debt report) — overlaying B-6's ambiguity data on this rendering is the obvious combined artifact for the US-2 tech-debt survey workflow. Requirements.md is intentionally unchanged by this entry.

## B-16 — Public-API entry-point detector (libraries)

- **What:** A detector that treats a library's public API as entry points — e.g., `__all__` members, non-underscore-prefixed top-level functions/classes, symbols re-exported in `__init__.py` — so reachability and dead-code analysis are meaningful for codebases invoked by import rather than execution.
- **Origin:** Stakeholder review question on EC-9 (2026-07-15): a library legitimately has zero detectable entry points under the v1 set.
- **Depends on:** Nothing structural — FR-8's pluggable detector interface is the designed extension point. Needs a heuristic decision (which conventions constitute "public") and probably a per-run toggle, since public-API entry points would mask genuinely dead public functions if always on.
- **V1 insurance:** FR-8 (detectors are additions, not surgery); FR-17 is the documented v1 workaround (slice from any selected function). EC-9 in requirements.md now cross-references this entry.
- **Notes:** Likely the highest-demand detector addition; libraries are a common inherited-codebase category. Interacts with B-18 — for libraries, B-18's origin points and this detector's output should largely coincide, which is a useful validation check for both.

## B-17 — Test entry-point detector and "reachable only from tests" signal

- **What:** A detector recognizing test functions (pytest/unittest conventions) as a *distinct class* of entry point, enabling the query "code reachable only from test entry points" as an explicit technical-debt signal — production code whose sole callers are tests is a strong dead-code indicator.
- **Origin:** Stakeholder review question on EC-9 (2026-07-15): do tests count as entry points? v1 answer: no — counting them as ordinary entry points would mark test-only production code as live, corrupting the dead-code report in the worst direction.
- **Depends on:** Entry-point *classes* (or an attribute distinguishing test origins from production origins) so reachability can be computed per-class — a schema attribute addition, not a new node type. Test-detection conventions must be chosen (file patterns, decorator/fixture recognition).
- **V1 insurance:** None needed beyond FR-8/FR-21; the class distinction rides in attribute fields. Note: until this exists, test functions appear as unreachable noise in the dead-code report — recorded as an OQ-5 presentation consideration in requirements.md.
- **Notes:** Companion to B-6; "reachable only from tests" belongs in the same tech-debt artifact family. Also supplies the "non-test" qualifier B-18 requires.

## B-18 — Non-entry origin points (apexes of unreachable call structures)

- **What:** Report the functions at the *top* of unreachable call structures — unreachable functions not called by any other non-test function. Code unreachable from accepted entry points still has internal call structure, and its apexes are informative: they may be the intended starting points of some task the code was designed to perform. For libraries especially, non-entry origin points are frequently the intended call points for the library's users.
- **Origin:** Stakeholder review addition (2026-07-15), extending the EC-9 discussion.
- **Depends on:** Computable from v1 index data alone (call graph + reachability) — it is a query/report over existing data, requiring no new analysis. The "non-test" qualifier depends on distinguishing test callers (B-17's detection, or interim path conventions). Caveat to design around: FR-14 over-approximation can attach spurious callers to a true origin point, silently demoting it from the apex set — an under-reporting direction the presentation should acknowledge.
- **V1 insurance:** None needed; derivable from the shipped index.
- **Notes:** Strong synergy with B-16: for a library, this report's output approximates the public API from the graph's shape, while B-16 derives it from naming conventions — agreement between the two validates both, and divergence is itself a findings report (documented API vs. structural API). Candidate for surfacing in the EC-9/zero-entry-point flow: "no entry points detected; here are the apparent origin points."

## B-19 — Viewer-side index freshness indicator

- **What:** The viewer displays the index's build time and whether the codebase has changed since — e.g., "index built at T; k files changed since" — with a one-action path to trigger re-analysis. Staleness matters at consumption time, so this is the successor to FR-38's end-of-run check, which only covers changes *during* a run.
- **Origin:** Review of EC-14 (2026-07-15); split from the FR-38 promotion — pipeline check promoted, viewer indicator deferred.
- **Depends on:** Viewer budget (the recurring constraint); a decision on check timing (on load, on query, on interval) and cost (hashing ~100k lines from the viewer on every load vs. mtime pre-check per FR-38's pattern). "Trigger re-analysis from the viewer" also crosses a line v1 deliberately keeps: the viewer currently only reads the index (FR-25) and never invokes the pipeline — that separation would need a deliberate exception.
- **V1 insurance:** FR-24's content hashes in the index are the comparison baseline; FR-38 establishes the check semantics (best-effort, hash-confirmed, never a freshness guarantee). Nothing further needed.
- **Notes:** Pairs with EC-15 (stale node selection) — a freshness indicator makes that error state predictable instead of surprising. The read-only-viewer exception is the design decision to interrogate first; a weaker version (indicator only, user re-runs from the CLI) avoids it entirely.
- **Viewer-control guidance (recorded 2026-07-16, from the step-2 review discussion; governs this entry and any other viewer control that triggers pipeline events):** (1) *Gradient of commitment* — "view report" is read-only rendering of run artifacts and crosses no boundary; "check status" invokes a fast, side-effect-free pipeline operation (B-21); "analyze" is long-running and index-mutating. Climb in that order. (2) *Preconditions* — the v1 viewer has shipped and survived; there is an in-view decision loop the control closes (the freshness indicator is what creates the re-analyze decision in-view — a trigger button before the indicator answers a question the viewer never poses); and the three problem classes a read-only viewer structurally lacks are budgeted: long-running-operation UX (FR-41 progress surfaced in-view), concurrency (viewer reading an index the pipeline is rewriting — likely needs a write-then-swap discipline; extends the EC-13/EC-14 family), and process management (spawned processes must preserve FR-33/FR-34 posture). (3) *One contract, no privileged path* — the viewer invokes the pipeline only through the same public surface every caller uses (CLI + FR-42 structured reports + FR-43 exit codes), never internal APIs; the FR-23 discipline applied to this seam. (4) *Sequence* — B-21 first (pipeline-side, immediately agent-callable), then this indicator rendering it, then a status button, then analyze only once concurrency has a designed answer; report viewing can ride along anywhere.

## B-20 — Bare-script entry-point detector (C-1 interpretation (c) completion)

- **What:** A detector recognizing directly invoked scripts that have neither a `__main__` guard (FR-9's territory) nor a packaging declaration (FR-10's) — e.g., a legacy ops script with `argparse` construction and executable statements at module top level, run as `python rotate_logs.py`. Completes C-1's interpretation (c): v1 ships (a); this adds (b).
- **Origin:** C-1 resolution (2026-07-15): interpretation (a) adopted for v1 with this blind spot explicitly accepted and recorded.
- **Depends on:** FR-8's detector structure (pure addition). Two design problems to interrogate before building: (1) *precision posture* — the heuristic ("module-level executable statements beyond imports/definitions," or narrower, "CLI-parser construction at module scope") over-fires on modules with import-time side effects (a `config.py` building dicts at top level is not an entry point); over-firing is the safe direction for dead-code findings (spurious entry points suppress false "dead" results, never create them) but pollutes the entry-point list, so the detector needs its own stated posture the way FR-14 states one for edges. (2) *Slice-target semantics* — the emitted node represents module top-level code, not a function; whether module-level flow is sliceable is exactly the AC-17.2 boundary, so the detector's node type/attributes must be designed together with that answer.
- **V1 insurance:** FR-8 (detectors are additions); the blind spot is documented at FR-10 and in the C-1 resolution trace, so v1 users and future planners know it exists.
- **Notes:** Concentrates in exactly the target domain — legacy repos accumulate bare cron/ops/migration scripts; AI agents emit runnable straight-line scripts and rarely add packaging metadata. Belongs to the same detector wave as B-16 (public API) and B-17 (tests); consider designing the three together, since each forces the "entry-point class/precision" questions the v1 detectors avoided.

## B-21 — Pipeline staleness check

- **What:** A CLI operation that, without analyzing anything, compares the index's recorded content hashes against current disk state and reports changed/added/removed files — the direct input to the user's (or an agent's) re-analysis trigger decision. Output through all three v1 channels: human-readable terminal output, FR-42-structured report with format version, and FR-43-style distinct exit codes (fresh vs. stale), enabling `status || analyze` chaining with no parsing.
- **Origin:** SA-7 disposition discussion (2026-07-15): the trigger-decision rationale offered for promoting SA-7 was recognized as pointing at this distinct, cheaper capability — SA-7/B-7 is retrospective (between-runs review); this is a freshness comparison (index vs. disk now).
- **Depends on:** Nothing new — reuses FR-24's stored hashes and FR-38's comparison mechanics (including the mtime pre-check / hash-confirm pattern). Pull-only by design: no filesystem watching, consistent with the v1 incremental posture.
- **V1 insurance:** None needed; all ingredients shipped in v1 (FR-24, FR-38, FR-42, FR-43).
- **Notes:** B-19 (viewer freshness indicator) becomes a rendering of this operation's structured output — implement this first and B-19 reduces to presentation plus the viewer-control question (see B-19 notes). Deliberately agent-callable: "check staleness; if stale, re-analyze" is the agent workflow the trigger-decision rationale described.
