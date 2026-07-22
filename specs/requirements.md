# pastapathfinder — Requirements Specification (v1)

**Project name:** pastapathfinder (named 2026-07-16; appears as "Static Analysis Workbench" in the source interview summary, which predates the naming).

**Status:** APPROVED by stakeholder 2026-07-16. Step-2 review gate (Concept-to-Spec Workflow) passed: all clarifications (§10 C-1–C-10) resolved, all suggested additions (§9 SA-1–SA-8) dispositioned, all checklist items (§11) closed. This document is the requirements baseline for the technical design stage; changes from this point follow the revision triggers recorded herein and are versioned alongside the code (workflow step 5 rule).
**Revision 2026-07-18 (APPROVED by stakeholder 2026-07-18):** outcomes of the OQ-1/OQ-7 engine evaluation (prototype sessions 1–5) folded in: §6 item 10 amended (bounded syntactic mechanisms exempted from the orchestration-only exclusion), FR-14 clarified (definition of "statically possible targets"; §10 C-11), FR-44 amended (documented engine-variance class), FR-30's feasibility trigger closed with a dated trace, and §8 OQ-1/OQ-7 resolved. No requirement was renumbered; no acceptance criterion was changed.
**Source:** `sa_tool_interview_summary.md` (interview session 2026-07-07, revised 2026-07-13)
**Purpose:** This document specifies *what* the software must do. It makes no technology or architecture choices except where the source summary explicitly mandates architectural requirements (see §4.6); those are carried over as stakeholder requirements, not introduced here. It is written to be consumed by AI coding agents that cannot ask clarifying questions; ambiguities found in the source are listed in §10 rather than silently resolved.

---

## 1. Problem statement

Maintainers who inherit legacy or AI-generated codebases lack a fast way to understand structure, dependencies, and change impact. Manual static analysis is slow, error-prone, and must be redone as the code evolves. This tool analyzes a codebase folder and produces a unified set of static-analysis artifacts, with an interactive sliced call trace — forward or backward from a chosen starting point — as the flagship capability for bugfix and impact-analysis workflows.

## 2. Target users

- **Primary user:** the project author — a hands-on developer maintaining legacy and AI-generated Python codebases, who needs to debug and extend code they did not write (or that an AI agent wrote).
- **Secondary users:** software engineers and maintainers responsible for bugfixing or extending inherited codebases.

What they are trying to accomplish:

1. **Bugfix / impact analysis:** starting from a known entry point (a `main`, CLI command, or API route), follow the flow of calls to locate suspect functions.
2. **Tech-debt survey / onboarding:** build a mental model of an unfamiliar codebase and identify refactoring targets from generated artifacts.
3. **Human verification of AI-generated code:** inspect the structure and problems of agent-written code before accepting it.

The tool serves human judgment. Producing AI-consumable artifacts (e.g., vector embeddings of the analysis) is not a goal of v1 nor of the next several planned stages, though it is acknowledged as a possible long-term direction (see §6, item 5).

## 3. User stories

- **US-1 (Bugfix trace — flagship):** As a maintainer debugging an error whose entry point I know, I want to select that entry point in an interactive viewer and follow a forward call slice, so that I can locate the suspect function without reading or rendering the whole codebase.
- **US-2 (Tech-debt survey / onboarding):** As an engineer inheriting an unfamiliar codebase, I want the tool to generate structural artifacts (entry-point list, call graph, reachability results, skip/coverage report), so that I can build a mental model and identify refactoring targets.
- **US-3 (AI-code verification):** As a developer reviewing AI-generated code, I want to run the same analysis against the agent's output, so that I can see its structure and problems before accepting it.
- **US-4 (Live re-analysis):** As a user whose codebase changed mid-debugging-session — whether I edited it myself or directed an AI agent to make the change — I want re-analysis to process only what changed, so that the trace I am following stays current without waiting for a full re-run.
- **US-5 (Coverage audit):** As a user of any workflow, I want an explicit report of every file that was excluded or skipped and why, so that I can trust that the analysis is not silently hiding relevant code.

## 4. Functional requirements

Conventions used below:

- "The system" means the analysis pipeline plus its artifacts; "the viewer" means the interactive component (§4.7). The two-part split is a stakeholder-mandated architectural requirement (§4.6, FR-20).
- "Analyzed file" = a discovered, non-excluded file that was successfully parsed and resolved. "Skipped file" = discovered, non-excluded, but not successfully analyzed. "Excluded file/path" = removed from analysis by an exclusion rule before parsing.
- "The run's diagnostics" (referenced throughout) = a per-run artifact, produced on every run and subject to FR-42's structured-format and format-version rules, collecting the non-fatal anomalies recorded during analysis — e.g., unresolved call sites (AC-14.2), detector errors (AC-8.2), probe and symlink-target skips (AC-1.5, AC-1.6), span omissions (AC-37.3), `.gitignore` parse problems (AC-3.2), and change-check failures (AC-38.3). An anomaly-free run still produces the (empty) artifact.
- Each requirement states one behavior. Acceptance criteria (AC) include at least one failure-path criterion.

### 4.1 Source discovery and exclusion

**FR-1 — Recursive source discovery.** The system shall recursively enumerate, under a single user-specified root folder, all files recognized as source files by the configured per-language analyzer(s); in v1 the sole configured analyzer is Python, which recognizes (a) files with the `.py` extension and (b) extensionless files whose first line is a shebang (`#!`) naming a Python interpreter.
- AC-1.1: When the root folder contains recognized source files (v1: rule (a) or (b)) at any nesting depth, then every such file appears in the coverage report (FR-7) with a status of analyzed, skipped, or excluded.
- AC-1.2: When the root folder contains files not recognized by any configured language analyzer, then those files do not appear as analysis inputs.
- AC-1.3 (failure): When the specified root folder does not exist or is not readable, then the system terminates the run with an error message identifying the path and the reason, and does not report the run as successful.
- AC-1.4: When an extensionless file's first line is a shebang naming a Python interpreter (e.g., `#!/usr/bin/env python3`), then the file is discovered as a Python source and appears in the coverage report.
- AC-1.5 (failure): When an extensionless file is binary or its first line is not a Python shebang, then it is not an analysis input; when an extensionless file cannot be read during the probe, then the failure is recorded in the run's diagnostics and the run continues.
- AC-1.6 (failure): When a symbolic link under the root folder targets a path outside the root folder, then the link is not followed, and the skipped target is recorded in the run's diagnostics; when symbolic links form a cycle, then discovery terminates without infinite recursion.

**FR-2 — Default exclusion conventions.** The system shall, by default, exclude paths matching the exclusion convention set(s) of the configured per-language analyzer(s); v1 ships the Python convention set, which shall contain at minimum `venv/`-style virtual-environment directories, `.git/`, `build/`, and `dist/` (exact list is Open Question OQ-3).
- AC-2.1: When a codebase contains a `venv/` directory (a member of the v1 Python set), then no file under it is parsed, and the directory appears in the exclusion report (FR-5) with the rule that excluded it.
- AC-2.2 (failure): When a default exclusion rule matches nothing in the codebase, then the run completes normally (unmatched rules are not errors).

**FR-3 — `.gitignore` incorporation.** The system shall incorporate the patterns in the target codebase's `.gitignore` file(s) into the default exclusion set.
- AC-3.1: When `.gitignore` contains a pattern matching a directory of `.py` files, then those files are excluded and attributed to the `.gitignore` rule in the exclusion report.
- AC-3.2 (failure): When a `.gitignore` file cannot be read or contains unparseable lines, then the system emits a warning identifying the file and line(s), continues the run using the remaining exclusion rules, and records the problem in the run's diagnostics.

**FR-4 — User exclusion overrides.** The system shall accept user configuration that (a) adds exclusion patterns and (b) re-includes paths that the defaults would exclude, with user configuration taking precedence over defaults.
- AC-4.1: When the user re-includes a path excluded by default, then files under that path are analyzed on the next run.
- AC-4.2: When the user adds an exclusion pattern, then matching files are excluded and attributed to the user rule in the exclusion report.
- AC-4.3 (failure): When the user configuration contains an invalid pattern, then the system reports an error identifying the pattern and does not silently ignore it.

**FR-5 — Exclusion report.** The system shall produce, on every analysis run, a report listing every excluded path together with the specific rule (default, `.gitignore`, or user override) that excluded it; on the first run against a given codebase, the system shall present this report (or its location) to the user as part of the run output.
- AC-5.1: When any path is excluded, then it appears in the report with its excluding rule.
- AC-5.2: When the first analysis of a codebase completes, then the run output directs the user to the exclusion report.
- AC-5.3 (failure): When nothing is excluded, then the report is still produced and states that no exclusions occurred.

### 4.2 Partial-analysis posture ("analyze what you can, loudly report what you skipped")

**FR-6 — Continue past per-file failures.** When an individual file cannot be parsed or its references cannot be resolved, the system shall record the failure with its reason and continue analyzing the remaining files; a per-file failure shall not terminate the run.
- AC-6.1: When one file contains a syntax error that defeats the configured analyzer's parser (v1: a Python syntax error), then the run completes, produces all artifacts for the remaining files, and lists the failed file in the skip report with a reason identifying it as a parse failure.
- AC-6.2 (failure): When *every* discovered file fails to parse, then the run completes, produces an index and reports (both reflecting zero analyzed files), and the run output states explicitly that no files were analyzed.

**FR-7 — Skip/coverage report.** The system shall produce, as a first-class artifact of every run, a coverage report listing every discovered file with exactly one status — analyzed, skipped (with reason), or excluded (with rule) — such that the three categories sum to the total discovered.
- AC-7.1: When a run completes, then `discovered = analyzed + skipped + excluded` holds for the report's counts.
- AC-7.2: When a file is skipped, then its entry includes a human-readable reason (e.g., syntax error, unresolvable import).
- AC-7.3 (failure): When the report itself cannot be written (e.g., output location not writable), then the run terminates with an error identifying the location; the report is not silently omitted.

**FR-42 — Machine-readable report formats.** Every pipeline report artifact — the exclusion report (FR-5), the coverage report (FR-7), the re-analysis report (FR-35), the post-run change warning (FR-38), and the run diagnostics — shall be produced in a documented, machine-readable structured format; human-readable renderings accompany or are derived from the structured form, and where the two disagree the structured form is authoritative. (Promoted from SA-4, 2026-07-15. Rationale: implementation labor is agent-driven, and structured reports are what let agents verify their work against this document's acceptance criteria mechanically.)
- AC-42.1: When a run completes, then each report it produces exists in the structured format and parses per that format's documentation.
- AC-42.2: When the FR-7 reconciliation (discovered = analyzed + skipped + excluded) is checked, then it is computable from the structured coverage report's fields alone, without parsing human-readable text.
- AC-42.3 (failure): When a structured report cannot be written, then the run terminates with an error identifying the location (per AC-7.3); a human-readable rendering is never silently substituted for a missing structured one.
- AC-42.4: When any structured report is written, then it contains a format-version identifier, and consumers refuse formats they do not support rather than misreading them (mirroring FR-39 for the index). This is the v1 insurance for future report evolution — e.g., backlog B-8's fourth file status, which would change the FR-7 status set and reconciliation as a versioned format change rather than a silent one.

### 4.3 Entry-point detection

FR-9 through FR-11 specify the v1 (Python) detector set. Unlike other requirements in this document, they are *deliberately* language- and framework-specific: each is an implementation of the FR-8 structure, and future languages add parallel detectors rather than generalizing these.

**FR-8 — Pluggable detector structure.** Entry-point detection shall be implemented as a set of independent detectors, each of which emits entry-point nodes conforming to the index schema; adding a new detector shall require no modification to existing detectors or to the core schema's node/edge types. A *detector* is a self-contained component that recognizes exactly one entry-point pattern (such as a `__main__` block or a Flask route) in analyzed code or project metadata and emits a schema-conformant entry-point node for each occurrence.
- AC-8.1: When a new detector is added, then the diff touches no existing detector and no core schema type definitions.
- AC-8.2 (failure): When one detector raises an error on a given file, then other detectors still run and the error is recorded in the run's diagnostics.

**FR-9 — `__main__` block detection.** The system shall detect `if __name__ == "__main__":` blocks and emit an entry-point node for each.
- AC-9.1: When a module contains a `__main__` block, then an entry-point node referencing that module appears in the index.
- AC-9.2 (failure): When a file containing a `__main__` block fails to parse, then no entry-point node is emitted for it and the file appears in the skip report.

**FR-10 — CLI entry-point detection.** The system shall detect CLI entry points declared in the codebase's packaging metadata (`console_scripts` / equivalent declarations in `pyproject.toml`, `setup.py`, or `setup.cfg`) and emit an entry-point node for each resolvable declaration. Scope decided (§10 C-1): packaging-declared entry points only. Directly invoked scripts that lack both a `__main__` block (FR-9) and a packaging declaration are not detected in v1; a bare-script detector is a recorded backlog item (backlog B-20).
- AC-10.1: When packaging metadata declares a console script pointing at a function in an analyzed file, then an entry-point node for that function appears in the index.
- AC-10.2 (failure): When a declared entry point references a function that cannot be resolved in the analyzed code, then the declaration is recorded in the skip/diagnostics output as unresolved rather than silently dropped.

**FR-11 — Web-framework route detection.** The system shall detect web-framework entry points for Flask, FastAPI, and Django (route-decorated functions for Flask/FastAPI; URLconf-referenced views for Django) and emit an entry-point node for each.
- AC-11.1: When a function carries a Flask or FastAPI route decorator, then an entry-point node for that function appears in the index.
- AC-11.2: When a Django URLconf references a view, then an entry-point node for that view appears in the index.
- AC-11.3 (failure): When route registration is too dynamic to resolve statically, then the affected construct is recorded in diagnostics as unresolved; the run does not fail and no incorrect entry point is fabricated.

### 4.4 Call-graph construction

**FR-12 — Static call graph.** The system shall construct a call graph over all analyzed files, in which nodes represent functions/methods (per the index schema, §4.6) and edges represent statically determined possible calls.
- AC-12.1: When function A contains an unambiguous direct call to function B and both are in analyzed files, then the index contains a call edge from A to B.
- AC-12.2 (failure): When a call target lies in a skipped or excluded file, then the call is represented as an edge to an external leaf node per FR-36, and the target file's status remains attributable via the skip/exclusion reports.

**FR-13 — No execution of target code.** The system shall not execute the target codebase's code at any point during analysis, nor import or otherwise load it by any mechanism that runs it (v1: Python `import`).
- AC-13.1: When the analysis machine lacks the target project's runtime environment (missing packages, missing C extensions), then analysis still runs to completion under FR-6 semantics.
- AC-13.2 (failure): When target code contains top-level side effects (e.g., network calls, file writes at import time), then running the analysis produces none of those side effects.

**FR-14 — Over-approximation of ambiguous calls.** When a call's target is ambiguous under static analysis (dynamic dispatch, duck typing, `getattr`, etc.), the system shall include a call edge to every statically possible target; the system shall never silently drop a possible edge. *(Clarified 2026-07-18, §10 C-11: "statically possible targets" means targets determinable by the configured resolution pipeline — the selected engine plus the syntactic mechanisms named in §6 item 10. Call sites the pipeline cannot resolve follow AC-14.2 and are recorded in the run's diagnostics. Known undeterminable classes at v1, measured on the reference benchmarks: bare-name value-flow dispatch (~46% of unresolved sites) and un-narrowed attribute dispatch; the documented manual workaround is §6 item 1's text-search-plus-backward-slice.)*
- AC-14.1: When a call site could dispatch to multiple implementations of a method, then the index contains an edge to each candidate implementation.
- AC-14.2 (failure): When a call target is entirely unresolvable (no candidates can be determined), then the call site is recorded in diagnostics as unresolved rather than omitted without trace.

**FR-40 — Ambiguity flag on over-approximated edges.** Every call edge whose target was resolved by over-approximation under FR-14 — i.e., the edge is one of multiple statically possible targets rather than an unambiguous resolution — shall carry an attribute, written at analysis time, marking it as ambiguous. (Promoted from SA-3, 2026-07-15. Rationale: the source designates ambiguity itself as a technical-debt signal that "may later become its own report" — backlog B-6; recording the flag at creation time makes that report a query over the existing index instead of a re-analysis. No v1 feature is required to read the flag.)
- AC-40.1: When FR-14 emits multiple candidate edges for one call site, then every one of those edges carries the ambiguity attribute.
- AC-40.2: When a call resolves to exactly one unambiguous target, then the resulting edge does not carry the ambiguity attribute.
- AC-40.3 (failure): When the ambiguity attribute is absent from an edge, then generic queries (slices, reachability) still function on that edge, consistent with AC-21.2.

**FR-36 — External call targets.** When analyzed code calls a target that is not itself analyzed (an external library, or code in an excluded or skipped file), the system shall represent the call as a call edge to a leaf node marked external via an attribute field and carrying the best statically resolvable qualified name; the internals of external targets shall not be analyzed, and external leaf nodes shall have no outgoing call edges. (Promoted from review, 2026-07-15.)
- AC-36.1: When analyzed code imports a library outside the root folder and calls one of its functions, then the index contains a leaf node for the imported symbol, marked external, with a call edge from the caller.
- AC-36.2: When analyzed code calls a function in an excluded file, then the call is represented per this requirement, and the target file's exclusion remains attributed in the exclusion report (FR-5).
- AC-36.3: When a slice (FR-15/FR-16) reaches an external leaf node, then the node appears in the slice as a terminus.
- AC-36.4 (failure): When the target's qualified name cannot be statically resolved at all, then the call site follows AC-14.2 (recorded in diagnostics); the system shall not fabricate an external node with a guessed name.
- AC-36.5: When the same external symbol is called from multiple call sites, then all resulting call edges target a single external leaf node for that symbol.

### 4.5 Slice queries (flagship), reachability, and dead code

**FR-15 — Forward call slice.** Given a selected node (entry point, function/method, or class), the system shall produce the forward slice: the subgraph of all nodes and call edges reachable from the selected node by following call edges in the forward direction.
- AC-15.1: When the selected node transitively calls N functions, then the forward slice contains exactly those nodes and the connecting edges — not the whole-program graph.
- AC-15.2 (failure): When the selected node has no outgoing call edges, then the query returns an empty slice result (presented as such), not an error.

**FR-16 — Backward call slice.** Given a selected node, the system shall produce the backward slice: the subgraph of all nodes and call edges from which the selected node is reachable by following call edges.
- AC-16.1: When function X is called (transitively) from M call sites, then the backward slice from X contains those callers and connecting edges.
- AC-16.2 (failure): When the selected node identifier does not exist in the index, then the query returns an error identifying the unknown identifier.

**FR-17 — Slice origin generality.** The system shall accept any function/method or class node in the index as a slice origin, not only detected entry points.
- AC-17.1: When the user selects an arbitrary (non-entry-point) function, then forward and backward slices are produced for it.
- AC-17.2 (failure): When the user selects a node type for which slicing is undefined (e.g., a file node, if the schema does not define call edges for it), then the system returns an error stating the node type is not sliceable, rather than an empty or misleading result.

**FR-18 — Reachability analysis.** The system shall compute, for every function/method node, whether it is reachable via call edges from at least one detected entry point, and record the result in the index.
- AC-18.1: When a function is transitively called from a detected entry point, then it is marked reachable.
- AC-18.2 (failure): When zero entry points are detected in the codebase, then reachability results are produced but the run output carries an explicit warning that no entry points were found and reachability is therefore uninformative (see §5, EC-9 — my addition).

**FR-19 — Dead-code report.** The system shall produce a report of code unreachable from any detected entry point, and every presentation of this report shall label the findings as approximate and subject to false positives arising from language dynamism (v1: Python dynamism); presentation details beyond this labeling are Open Question OQ-5.
- AC-19.1: When a function is unreachable from all entry points, then it appears in the dead-code report.
- AC-19.2: When the dead-code report is rendered (in any artifact or in the viewer), then the approximation/false-positive caveat is present in that rendering.
- AC-19.3 (failure): When no entry points were detected, then the dead-code report is emitted with the FR-18/AC-18.2 warning rather than asserting the entire codebase is dead code without qualification.

### 4.6 Index, schema, and extensibility (stakeholder-mandated architectural requirements)

The source summary mandates these as v1 architectural requirements. They are carried over verbatim in intent; they are not design choices introduced by this document.

**FR-20 — Persistent queryable index.** The analysis pipeline shall write its results to a persistent index on local disk that can be queried (including slice queries, FR-15/16) without re-running analysis.
- AC-20.1: When analysis completes and the pipeline process exits, then slice and reachability queries can be answered from the index alone.
- AC-20.2 (failure): When the index is absent or unreadable at query time, then the querying component reports an error identifying the index problem and instructing the user to (re)run analysis.

**FR-21 — Language-agnostic index schema.** The index schema shall define only generic node types (file, function/method, class, entry point) and generic edge types (calls, contains, imports); all language-specific detail (e.g., Python decorators, `__main__` semantics) shall be carried in attribute fields, never as first-class schema node/edge types.
- AC-21.1: When the schema definition is inspected, then no Python-specific concept appears as a node or edge *type*.
- AC-21.2 (failure): When a language-specific attribute is missing or unpopulated on a node, then generic queries (slices, reachability) still function on that node.

**FR-22 — Language-namespaced node identity.** Every node identifier in the index shall be namespaced by language (e.g., a Python function's ID carries a `python:` namespace component).
- AC-22.1: When any node is written to the index, then its ID includes the language namespace.
- AC-22.2 (failure): When a graph fragment containing a non-namespaced ID is submitted to the index, then it is rejected with an error, not stored.

**FR-23 — Per-language analyzer adapter boundary.** The pipeline shall define a single per-language analyzer interface — source files in, schema-conformant graph fragments out — and the chosen Python analysis engine shall be invoked only through this interface.
- AC-23.1: When the pipeline's components are inspected, then no component other than the Python adapter references the underlying engine's APIs or data formats.
- AC-23.2 (failure): When the adapter emits a fragment that does not conform to the schema, then the pipeline rejects it with a validation error rather than writing malformed data to the index.

**FR-24 — Incremental re-analysis.** The system shall key per-file analysis results by file content hash and, on re-analysis, re-process only files whose content hash changed plus their dependents; results for unchanged files shall be reused. "Dependents" means the transitive closure of reverse dependency (import) edges — every file whose analysis results could be affected by the change, however indirectly — as the correctness floor (decided, §10 C-9); the design may narrow re-processing within that floor only by means that provably preserve result equivalence with full transitive re-processing. (The v1 *surfaced* behavior may be limited to "re-run is fast when few files changed"; the keying and selective re-processing are required regardless.)
- AC-24.1: When re-analysis runs with zero changed files, then no file is re-parsed and the index is unchanged.
- AC-24.2: When exactly one file changed, then only that file and its dependents are re-resolved.
- AC-24.3 (failure): When cached per-file results are corrupt or missing, then the system falls back to full analysis of the affected files and reports that the fallback occurred; it does not serve corrupt results.

**FR-35 — Re-analysis report.** The system shall produce, on every incremental re-analysis run, a pipeline artifact listing each file that was re-processed, attributed to exactly one reason — content changed, dependent of a changed file, or cache fallback (AC-24.3) — and listing files removed since the prior run. This is a pipeline report only; no viewer presentation is required in v1. (Promoted from suggested-additions review, 2026-07-13; it is also the observability instrument that makes FR-24's acceptance criteria testable.)
- AC-35.1: When exactly one file's content changed, then the report lists that file with reason "content changed" and each of its dependents with reason "dependent of a changed file," and no other files.
- AC-35.2: When zero files changed, then the report is still produced and states that no files were re-processed.
- AC-35.3: When a file present in the prior run is absent from the current one, then the report lists it as removed.
- AC-35.4 (failure): When the AC-24.3 fallback occurs, then every file analyzed via the fallback path appears in the report with the fallback reason; the fallback is never silent.

**FR-37 — Source-location attributes.** Every non-external node in the index shall carry source-location attributes: the containing file's path relative to the analysis root, and the start and end lines of the element's definition. (Promoted from review, 2026-07-15: without a node-to-codebase-location mapping, slice results cannot be translated into the code they describe. File-plus-span is a property of text, not of any language, so this belongs in the generic attribute vocabulary without compromising FR-21.)
- AC-37.1: When a function/method, class, or entry-point node is written to the index, then its attributes include the relative file path and the definition's start and end lines.
- AC-37.2: When a node is marked external (FR-36), then source-location attributes are absent, and their absence does not impair generic queries (consistent with AC-21.2).
- AC-37.3 (failure): When the analyzer cannot determine a line span for an element it can otherwise identify, then the node carries the file path with the span omitted, and the omission is recorded in the run's diagnostics; no fabricated span is written.

**FR-38 — Post-run change detection.** At the completion of every analysis run (initial or incremental), the system shall compare the recorded content of discovered files against their current on-disk state and, if any differ or have been removed, include a warning in the run output naming the affected files, stating that results reflect pre-change contents, and recommending re-analysis. The comparison may use a cheap pre-check (e.g., size/mtime) provided a content-hash comparison confirms any reported difference. This check is best-effort: it narrows the window in which mid-run changes go unnoticed; it cannot close it, and the warning shall not be presented as a guarantee of freshness. (Promoted from review of EC-14, 2026-07-15; the motivating scenario is AI agents editing code concurrently with an analysis run.)
- AC-38.1: When a discovered file's content changes between being read for analysis and run completion, then the run output includes a warning naming that file and recommending re-analysis.
- AC-38.2: When no discovered file changed during the run, then no change warning is emitted.
- AC-38.3 (failure): When a discovered file has been deleted before run completion, then it is included in the warning as removed; when a file cannot be re-read during the check, then that is reported as a check failure for that file, not silently treated as unchanged.

**FR-39 — Index schema version identifier.** The index shall contain a schema-version identifier written by the pipeline on every run; any component reading an index whose schema version it does not support shall refuse to read it, reporting the incompatibility, rather than interpreting it as current. (Promoted from SA-2, 2026-07-15. This is the mechanism EC-13 and AC-25.2 presuppose, and the precondition for the two anticipated schema migrations — backlog B-1's variable/attribute nodes and B-10's language-two revision. The versioning scheme and the definition of a breaking change are design-stage decisions.)
- AC-39.1: When the pipeline writes an index, then the index contains the schema-version identifier of the schema it was written against.
- AC-39.2: When a component opens an index bearing a schema version it does not support, then it refuses with an error naming the found and supported versions; it does not read the data as current.
- AC-39.3 (failure): When the version identifier is missing or unreadable, then the index is treated as incompatible (per AC-39.2), not assumed current.

**FR-44 — Deterministic output.** Two analysis runs over identical input — identical file contents, identical configuration, identical tool version — shall produce equivalent indexes and reports, differing at most in explicitly designated volatile fields (e.g., timestamps, durations); the set of volatile fields shall be documented. (Promoted from SA-6, 2026-07-15. Rationale: determinism enables regression testing of the pipeline by diff, the cheapest verification available to agent-driven implementation.) *(Amended 2026-07-18: the selected engine exhibits rare seed-independent internal variance at large scale — measured 3 of 88,228 edges (0.003%) on the pandas benchmark, zero at ~131k-line scale. Runs shall be equivalent modulo the documented volatile fields **and** this documented variance class; comparison tooling detecting a within-class difference shall report it rather than silently ignore it, and any difference outside the class remains a defect.)*
- AC-44.1: When the same codebase is analyzed twice with no changes, then the two indexes are equivalent, and any differences are confined to the documented volatile fields.
- AC-44.2: When the same codebase is analyzed twice with no changes, then all structured reports (FR-42) are likewise equivalent modulo documented volatile fields.
- AC-44.3 (failure): When file processing order varies between runs (e.g., due to parallel execution or filesystem enumeration order), then stored results are still equivalent; ordering effects outside volatile fields are defects against this requirement.

### 4.7 Interactive viewer

**FR-25 — Local interactive viewer.** The system shall provide an interactive viewer that runs on the user's machine, presented in its own window or dedicated browser context, and that consumes analysis data exclusively through the index schema (FR-21) — never by invoking language tooling directly.
- AC-25.1: When the viewer runs, then it makes no calls to language analyzers or engines; all data comes from the index.
- AC-25.2 (failure): When the index is missing, unreadable, or schema-incompatible, then the viewer displays an error identifying the problem instead of a blank or partial UI.

**FR-26 — Entry-point browsing.** The viewer shall present a view listing all detected entry points from which the user can select one.
- AC-26.1: When the index contains entry points, then all of them are listed and selectable.
- AC-26.2 (failure): When the index contains zero entry points, then the view states this explicitly and offers slice-by-arbitrary-node (FR-17) as the alternative.

**FR-27 — Trace opening and navigation.** When the user selects a node, the viewer shall open a sliced trace view (forward or backward per the user's choice) and allow the user to follow call edges through the slice.
- AC-27.1: When the user selects an entry point and chooses forward, then the forward slice from that node is displayed and each displayed call edge can be followed to its target node.
- AC-27.2 (failure): When a slice query fails (FR-16/AC-16.2), then the viewer surfaces the query error to the user rather than showing an empty view without explanation.
- AC-27.3: When a node in a trace view is selected, then its source-location attributes (FR-37) are displayed; for external nodes (FR-36), the view states that the target is external instead.

**FR-28 — Slice-first presentation.** The viewer's presentation of graph data shall be sliced views; rendering the whole-program graph is not required in v1 and shall never be the default presentation.
- AC-28.1: When the user opens any graph view via the standard workflow, then what is rendered is a slice bounded to the selection, not the full graph.
- AC-28.2 (failure): When a slice is still too large to render responsively, then the viewer bounds or truncates it visibly (mechanism is Open Question OQ-4) rather than freezing or rendering an unusable hairball.

### 4.8 Performance, platform, and deployment

Reference machine for all performance bounds (resolves §10 C-3): a 4-core / 8-thread x86-64 CPU (reference: Intel Core i7-4700HQ), 24 GB RAM, SATA SSD, on the reference Linux environment. The reference Linux environment (resolves §10 C-7) is an enterprise-Linux-class distribution, reference: AlmaLinux 9. Both are stated as a class with a concrete reference so that equivalent-or-better hardware and compatible distributions satisfy them; upgrading the development machine does not trigger a requirements revision, and bounds met on this baseline hold a fortiori on newer hardware.

Benchmark codebases (resolves §10 C-8; hard-designated by the stakeholder 2026-07-16): **Django core** is the performance reference — approximately the ~100k-line target scale, real-world, and containing FR-11 entry-point patterns — against which the FR-29 bound is asserted. **pandas** is the dynamism/robustness benchmark — substantially larger than target scale and heavy in the dynamic patterns that stress call resolution — against which functional correctness and run-to-completion are asserted but the FR-29 time bound is not. Exact versions/commits of both are pinned at design stage for reproducibility.

**FR-29 — Initial analysis time bound.** Initial analysis of a ~100,000-line Python codebase shall complete within 10 minutes on the reference machine (source's "coffee break": ~2–10 min; sub-minute considered plausible but not required).
- AC-29.1: When run against the Django-core benchmark on the reference machine (§4.8 preamble), then wall-clock time from invocation to all artifacts written is ≤ 10 minutes.
- AC-29.2 (failure): When analysis exceeds the bound, then it still completes correctly (the bound is a performance requirement, not a timeout that aborts the run).
- AC-29.3: When run against the pandas benchmark on the reference machine, then analysis runs to completion and produces all artifacts per the functional requirements (FR-6/FR-7 semantics for whatever it cannot resolve); the 10-minute bound is not asserted.

**FR-30 — Re-analysis time bound.** Re-analysis after ≤ 5 changed files shall complete within 30 seconds on the reference machine (§4.8 preamble). Threshold confirmed by the stakeholder (§10 C-2) as the tolerated wait within a live debugging session; the OQ-1 engine prototype verifies feasibility on the reference machine, and infeasibility there is a revision trigger for this requirement (relax seconds, shrink the file count, or accept a degraded live-session experience — as a recorded decision). *(Verified 2026-07-18: the OQ-1 prototype confirmed feasibility on the reference machine for the selected engine — 13.2 s for the 5-changed-file high-fan-in set, 2.7 s for a leaf change; the two rejected per-site-query engines exceeded the bound, which drove engine selection (§8 OQ-1) rather than a requirements revision. The trigger is closed without amendment.)*
- AC-30.1: When ≤ 5 files changed since the last run, then re-analysis completes within 30 seconds on the reference machine.
- AC-30.2 (failure): When the incremental path is unavailable (AC-24.3 fallback), then the user is informed that a longer full analysis is running.

**FR-41 — Progress indication.** During an analysis run, the system shall emit progress output showing files processed out of total discovered, updated at least once every 5 seconds while file processing is under way. (Promoted from SA-1, 2026-07-15. Rationale: a run lasting up to the FR-29 bound with no output is indistinguishable from a hang.)
- AC-41.1: When file processing is under way, then progress output (processed / total) is emitted and updates at least every 5 seconds.
- AC-41.2 (failure): When the total is not yet known (e.g., during discovery) or progress cannot be computed, then the system emits an activity indication for the current phase rather than remaining silent.

**FR-43 — Distinct process exit codes.** The pipeline process shall exit with documented, mutually distinct exit codes for at least three outcomes: success (run completed, zero skipped files), partial success (run completed, one or more files skipped), and failure (run did not complete). Specific values are a design-stage decision; distinctness and documentation are required. (Promoted from SA-5, 2026-07-15. Rationale: scripted and agent-driven use must distinguish outcomes without parsing report text.)
- AC-43.1: When a run completes with every discovered, non-excluded file analyzed, then the process exits with the success code.
- AC-43.2: When a run completes with at least one skipped file, then the process exits with the partial-success code, distinct from both other codes.
- AC-43.3 (failure): When a run terminates without completing (e.g., unreadable root folder per AC-1.3, unwritable report location per AC-7.3), then the process exits with the failure code, distinct from both other codes.

**FR-31 — Platform support.** The system shall run on Linux and on Windows via WSL2, both as tested support targets. On WSL2, the performance requirements (FR-29, FR-30) apply only when the target codebase and the index reside on the Linux filesystem (e.g., under the WSL distro's home directory); this condition shall be stated in the user-facing documentation. Codebases on Windows-mounted filesystems (e.g., `/mnt/c/...`) shall be analyzable: all functional requirements apply, the performance bounds do not, and filesystem-semantics-dependent behavior (path case sensitivity, symlink handling) follows the semantics of the mounted filesystem. (macOS and native non-WSL Windows are out of scope, §6.)
- AC-31.1: When installed on the reference Linux environment, then all v1 workflows (US-1..US-5) function.
- AC-31.2: When installed on WSL2 with the target codebase and index on the Linux filesystem, then all v1 workflows function and the FR-29/FR-30 bounds apply.
- AC-31.3: When the target codebase resides on a Windows-mounted filesystem under WSL2, then analysis completes and produces correct artifacts per the functional requirements; FR-29/FR-30 are not asserted.
- AC-31.4 (failure): When run on an unsupported platform, then failures are ordinary errors; no requirement exists to detect or block unsupported platforms.

**FR-32 — Installation.** The system shall be installable by a single package-manager command appropriate to its implementation ecosystem (e.g., `pip install` if the tool is implemented in Python); the implementation language of the tool itself is Open Question OQ-7.
- AC-32.1: When a user runs the documented install command on a supported platform, then the tool is runnable with no further manual setup steps beyond documented configuration.
- AC-32.2 (failure): When installation fails (e.g., network unavailable during install), then the failure is the package manager's ordinary error; the tool does not require partial-install repair steps.

**FR-33 — No post-install network communication.** After installation and configuration, the system shall perform no external network communication during operation (no telemetry, no phone-home, no online lookups).
- AC-33.1: When analysis and viewing are performed on a machine with all external network access blocked, then all v1 workflows function.
- AC-33.2 (failure): When any component attempts external network access at runtime, then that is a defect against this requirement (corporate-network-friendliness is a stated constraint, §8).

**FR-34 — No elevated privileges.** Basic operation (analysis and viewing) shall not require administrator/root rights.
- AC-34.1: When run as an unprivileged user with read access to the target codebase and write access to the output location, then all v1 workflows function.
- AC-34.2 (failure): When the output location is not writable by the unprivileged user, then the system reports a permissions error identifying the path; it does not request elevation.

## 5. Edge cases and error handling

Cases EC-1 through EC-8 are drawn from the source summary; EC-9 onward are **my additions**, marked as such.

| # | Situation | Required behavior |
|---|---|---|
| EC-1 | File with Python syntax errors (including never-imported dead files) | Skip the file; list it in the skip report with a parse-failure reason; continue (FR-6). |
| EC-2 | Python 2 remnants that fail Python 3 parsing | Same as EC-1; the reason should identify a parse failure (distinguishing Py2 syntax specifically is not required). |
| EC-3 | Vendored third-party code / generated code (protobufs, migrations) inflating analysis and polluting graphs | Excluded by defaults where conventions match (FR-2); everything excluded is attributed in the exclusion report (FR-5); user overrides available (FR-4). Calls from analyzed code into such code are acknowledged as edges to external leaf nodes (FR-36), not silently truncated. |
| EC-4 | Analysis machine lacks the target's runtime environment (missing packages, C extensions) | Analysis must not execute or import target code (FR-13); unresolvable references are skipped and reported (FR-6/FR-7). |
| EC-5 | Dynamic dispatch, duck typing, `getattr` ambiguity | Over-approximate: include every possible edge; never silently drop (FR-14). Missing the real buggy path is the fatal failure mode for a debugging tool. |
| EC-6 | Whole-program graph of a spaghetti codebase is an unusable hairball | Slicing is the primary and default presentation; full-graph rendering not required (FR-28). |
| EC-7 | Code edited mid-debugging-session makes analysis stale | Incremental re-analysis (FR-24, FR-30). |
| EC-8 | Overly aggressive default exclusions silently hide relevant code | Every-run exclusion report, prominent on first run, doubles as the audit trail (FR-5); user re-inclusion overrides (FR-4). |
| EC-9 *(my addition)* | Zero entry points detected in the codebase | Run completes; entry-point view states the fact (AC-26.2); reachability/dead-code outputs carry an explicit "no entry points found" warning instead of declaring all code dead without qualification (AC-18.2, AC-19.3). Note: this is the *expected* outcome for a library codebase, whose entry points are its public API — a category no v1 detector recognizes; the warning is normal there, and FR-17 (slice from any selected function) is the v1 workaround. A public-API entry-point detector is a recorded backlog item (backlog B-16). |
| EC-10 *(my addition)* | Root folder is empty or contains zero files recognized by any configured analyzer (v1: Python, FR-1 rules (a)/(b)) | Run completes with an index reflecting zero files; coverage report and run output state explicitly that no recognized sources were discovered (v1: no Python sources). |
| EC-11 *(my addition)* | Symbolic links forming cycles, or links pointing outside the root folder | Discovery must terminate (no infinite recursion). *Decided 2026-07-15:* links targeting paths outside the root folder are not followed, and skipped link targets are recorded in diagnostics (AC-1.6). |
| EC-12 *(my addition)* | Source files with non-UTF-8 or undeclared encodings | Treat as a per-file failure per FR-6: skip, report with an encoding-related reason, continue. |
| EC-13 *(my addition)* | Index file corrupted, truncated, or produced by an incompatible schema version | Pipeline: fall back per AC-24.3 and report. Viewer: refuse to load with an error identifying the incompatibility (AC-25.2); never render silently wrong data. Version incompatibility is detected via the FR-39 schema-version identifier. |
| EC-14 *(my addition)* | Files change on disk while an analysis run is in progress | The run's artifacts must be internally consistent with the file contents each file had when it was read during that run; the staleness remedy is re-analysis (FR-24), not mid-run reconciliation. A best-effort post-run check warns the user when mid-run changes are detected (FR-38). |
| EC-15 *(my addition)* | User selects a node in the viewer that no longer exists after re-analysis | Viewer surfaces the FR-16/AC-16.2 unknown-identifier error and returns the user to a valid view; it does not crash or show a stale slice as current. |

## 6. Non-goals / out of scope (v1)

Every exclusion in this section is scoped to v1: "out of scope" means *not in this release*, not *never*. Where follow-on intent has been recorded, the item says so (and references a backlog entry); where an exclusion is intended as permanent or has no recorded follow-on intent, the item states that explicitly. An implementing agent must treat all items identically regardless of long-term status: build nothing toward them in v1.

Carried over from the source summary:

1. **Full data-flow / taint analysis** (backlog B-2). Acknowledged as the highest-value capability; research-grade effort; deferred. The nearest mitigation — statically resolved "find all readers/writers of an attribute or module-level variable" (backlog B-1) — is **not in v1** (decided; §10 C-6) and is the top candidate for the first follow-on. The v1 manual workaround when a call chain goes cold at shared mutable state: identify candidate writers by text search, then backward-slice each (FR-16/FR-17) to see how they are reached.
2. **UML class diagrams** (backlog B-3) — deferred to an early follow-on, not indefinitely.
3. **Standalone data/control-flow graph artifacts** (user-ranked highest-value non-v1 feature; backlog B-4) and **configuration analysis** (backlog B-5) — early follow-on candidates.
4. **Test mapping and test-coverage information.**
5. **Vector generation / AI-consumable artifacts** (backlog B-9). Out of scope for v1 and not planned for the next several release stages; the tool's purpose is to serve human judgment. Converting the analysis/code breakdown into vector embeddings is acknowledged as a possible long-term direction, but no v1 requirement anticipates or prepares for it.
6. **Languages other than Python 3 as implemented features.** Java and JavaScript/Angular are the proven-pain future targets but are not implemented in v1 (backlog B-10). Cross-language boundary analysis — tracing calls across seams where languages meet through a protocol rather than a language-level call, e.g., an Angular HTTP request resolving to the Java controller route that serves it — is deferred, not rejected: FR-22's namespaced node IDs are its v1 insurance, and backlog B-11 holds the feature, gated on two languages existing. Python 2 support is excluded with no planned follow-on. Note: language-agnosticism of the schema and analyzer boundary *is in scope* (§4.6); only additional language implementations are excluded.
7. **Plugin framework infrastructure** — no plugin discovery, registration, versioning, or plugin-configuration machinery, even though extensibility is a goal. With one language, a correct multi-language abstraction cannot be designed; the generic schema and thin adapter interface are the required insurance, and framework machinery beyond them is explicitly prohibited over-engineering.
8. **macOS support** (no test environment available; backlog B-13).
9. **IDE/editor integration.** (The recorded first candidate exception is backlog B-12, editor jump-to-source from the viewer; v1 builds nothing toward it — the v1 boundary is displaying source locations, AC-27.3.)
10. **Writing novel static-analysis algorithms of any kind** — in particular type inference, data-flow, or value-flow analysis. The pipeline orchestrates existing engines for all semantic analysis. *(Amended 2026-07-18: this exclusion does not extend to bounded syntactic mechanisms built on the standard library's parser — specifically, import/symbol-table resolution used to attribute calls to external or unanalyzed targets (FR-36). Basis: engine evaluation, prototype sessions 1–5; no maintained existing engine covers these mechanisms, and their cost and behavior are measured in FINDINGS-namematch.md and FINDINGS-session5.md.)*

Explicit exclusions a reader might otherwise assume (not mentioned in the source; stated here to prevent invented scope):

11. **User accounts, authentication, or authorization** — none. Single local user.
12. **Multi-user or collaboration features** — none.
13. **Hosted/cloud/SaaS deployment** — none; strictly local operation (FR-33 forbids runtime network use).
14. **Mobile or tablet support** — none.
15. **Notifications** (email, desktop, or otherwise) — none.
16. **CI/CD integration** or automated gating of code changes — none.
17. **Code modification of any kind** (refactoring execution, auto-fixes, formatting) — the tool is read-only with respect to the target codebase.
18. **Security vulnerability scanning** — not a goal; over-approximation serves debugging comprehension, not security audit.
19. **Telemetry or usage analytics** — none (FR-33).
20. **Native Windows (non-WSL) support** — not targeted. (Windows via WSL2 *is* a tested support target per FR-31, with a documented Linux-filesystem condition on the performance bounds.)
21. **Whole-program graph rendering** — not required in v1 (FR-28) and never the default presentation; deferred as a tech-debt-evidence artifact (backlog B-15).

## 7. Constraints

Any design produced from this document must respect the following (from the source summary):

- **Team:** solo developer. The majority of implementation code will be written by AI agents (Claude, Pro tier, token-bounded weekly usage). Human review time is abundant (several hours/day); token-gated generation is the scarce resource. **Sequencing implication (stakeholder-mandated):** the batch pipeline — token-cheap to review — must be built and usable before the iteration-heavy viewer.
- **Time:** ≥ 20 hours/week available; no external deadline; self-paced. When scope pressure arises, the portfolio goal (a finishable, demonstrable v1) takes precedence over product ambitions.
- **Soundness:** Python's dynamism makes call resolution and dead-code detection inherently unsound. All outputs must be framed as approximations; over-approximation is the chosen posture (FR-14, FR-19).
- **Dependency risk:** engine choice may smuggle in dependencies (e.g., Pyright implies Node.js; mitigable via `nodejs-wheel`-style packaging, or avoidable via Jedi at some capability cost). Whatever the choice, FR-32..FR-34 (pip install, offline, no admin) still bind.
- **Licensing:** CodeQL's license prohibits commercial use — acceptable for a personal/portfolio tool, a constraint if product ambition grows. Engine selection (OQ-1) must record this trade-off.
- **Deployment environment:** offline-capable after install; no runtime phone-home; no post-install admin rights; corporate-network-friendly (FR-33, FR-34).
- **Target scale:** ~100,000 lines of Python 3 (FR-29).

## 8. Open questions and working assumptions

Carried over from the source. Each is stated with the working assumption under which this document's requirements are written, so the document is usable now and revisable later.

- **OQ-1 — Which analysis engine(s) to orchestrate** (Pyright, Jedi, PyCG, CodeQL, others). To be resolved by a timeboxed prototype run on the reference machine (§4.8 preamble) against the two designated benchmarks: Django core for the FR-29/FR-30 performance criteria, pandas for call-resolution quality and robustness under heavy dynamism. An error-recovering parser is a recorded tiebreaker (backlog B-8), and FR-44 (deterministic output) is a fourth criterion — an engine with nondeterministic resolution ordering requires result normalization in the FR-23 adapter to comply, which counts against it as adapter complexity. **Working assumption:** requirements are engine-agnostic; any selected engine must operate behind the FR-23 adapter interface, satisfy FR-13 (no execution of target code), and permit FR-32..FR-34 deployment properties. If no acceptable engine meets FR-30 on the reference machine, FR-30 reopens as a recorded trade-off (its revision-trigger clause). Additional evaluation resources (recorded 2026-07-16): the PyCG paper's published benchmark suite covers the call-resolution *correctness* axis with small, ground-truth cases, complementing the two designated benchmarks (which cover performance and robustness at scale); mypy_primer is the ecosystem's corpus-regression pattern should broader multi-project coverage ever be wanted. **Resolved 2026-07-18.** Engine selected: **mypy (pinned exactly; evaluation version 2.3.0), driven through its programmatic build API behind the FR-23 adapter.** Evidence (prototype sessions 1–5, reference machine): Jedi and Pyright rejected — their per-call-site query interfaces structurally fail FR-30 (125 s and 220 s respectively on the 5-file benchmark set); PyCG is unmaintained and cannot parse modern Python. mypy: FR-29 satisfied at 11.3 s on Django core; FR-30 satisfied at 13.2 s (core change) / 2.7 s (leaf change); AC-29.3 pandas robustness verified (664k LOC to completion, zero file casualties); FR-13 verified; FR-44 satisfied modulo the variance class its 2026-07-18 amendment documents. The engine's semi-public API entails a pin-exactly-and-revalidate upgrade policy, recorded at design stage. Decision rationale and full evidence: `specs/design.md` §2 (decision log) and the engine-evaluation FINDINGS files (sessions 1–5).
- **OQ-2 — Viewer technology** (locally launched web app vs. Tauri/Electron shell; graph library). Deferred until the pipeline and index exist, since the schema constrains the viewer more than vice versa. **Working assumption:** the viewer is a locally launched interactive application in its own window or dedicated browser context (FR-25); nothing in this document depends on which shell or library is chosen.
- **OQ-3 — Exact default exclusion list.** Low-risk detail; the hybrid model (defaults + prominent report + overrides) bounds the damage of a wrong default. **Working assumption:** the defaults include at minimum virtual-environment directories, `.git/`, `build/`, `dist/`, and `.gitignore` patterns (FR-2, FR-3); the final list is settled during design without changing FR-2's structure. The design should also decide whether language-independent entries (e.g., `.git/`) live in a common convention set alongside the per-language sets or are duplicated into each per-language set, with exclusion-report attribution and multi-language override semantics as the deciding criteria.
- **OQ-4 — Slice-bounding rules** (N-hop limit vs. stop-at-external-boundary vs. user-expandable). Needs experimentation with real graphs. **Working assumption:** v1 must bound oversized slices *somehow* and do so visibly (AC-28.2); the specific mechanism is a viewer-design decision that does not alter the slice-query requirements (FR-15..FR-17). Note: FR-36's external leaf nodes supply the graph representation that the stop-at-external-boundary option presupposes, so that option is now implementable.
- **OQ-5 — Responsible presentation of dead-code findings** given Python false-positive rates. **Working assumption:** reachability data is computed and stored in v1 regardless (FR-18); every rendering of dead-code findings carries an approximation caveat (FR-19); presentation refinement happens after real-world output is observable. Additional consideration (2026-07-15): test functions are not entry points under the v1 detector set (consistent with the §6 exclusion of test mapping), so every test function in an analyzed codebase will appear unreachable — the presentation decision must account for this noise, or test-conventional paths must join the OQ-3 default-exclusion discussion.
- **OQ-6 — Competitive evaluation** (Sourcetrail, Understand, Sourcegraph, code2flow/pydeps, CodeQL/Semgrep). **Working assumption:** no competitive finding changes v1 requirements; a timeboxed evaluation before feature design is recommended to sharpen differentiation and borrow proven UX ideas, but this document does not depend on it.
- **OQ-7 — Implementation language of the tool itself.** Not addressed in the source summary, which specified distribution "via `pip install` (or equivalent)"; surfaced during review (2026-07-15). Interacts strongly with OQ-1 — the selected engine's host ecosystem pulls the pipeline toward it (e.g., Pyright implies Node.js, Jedi implies Python) — with OQ-2 (viewer stack), and with the deployment properties FR-32–FR-34, which any chosen ecosystem must be able to satisfy (single-command install, offline operation, no elevated privileges). **Working assumption (confirmed by stakeholder 2026-07-15):** the functional requirements in this document are implementation-language-neutral; FR-32 requires a single-command install in whichever ecosystem is chosen, with `pip install` as the expected form if the implementation is Python. Resolution point: alongside OQ-1 — these are one coupled decision, since the foreign-runtime packaging burden is a function of the engine × implementation-language pair (it vanishes when the tool is implemented in the engine's own ecosystem, e.g., npm+Pyright or pip+Jedi, and must be solved otherwise, with pip+`nodejs-wheel` the one pre-packaged crossing). Early design artifacts (schema definition, adapter interface) must be written language-neutrally until this resolves. **Resolved 2026-07-18** (coupled with OQ-1, as anticipated): implementation language is **Python (3.13)** — the selected engine's own ecosystem; `pip install` distribution per FR-32; no foreign-runtime packaging burden; mypy runs in-process behind the FR-23 adapter. Recorded in `specs/design.md` §2.

## 9. Suggested additions for review

Not in the source summary; **not** part of the numbered requirements unless a human approves promoting them.

- **SA-1 — *Promoted 2026-07-15*** as FR-41 (progress indication).
- **SA-2 — *Promoted 2026-07-15*** as FR-39 (index schema version identifier).
- **SA-3 — *Promoted 2026-07-15*** as FR-40 (ambiguity flag on over-approximated edges).
- **SA-4 — *Promoted 2026-07-15*** as FR-42 (machine-readable report formats).
- **SA-5 — *Promoted 2026-07-15*** as FR-43 (distinct process exit codes).
- **SA-6 — *Promoted 2026-07-15*** as FR-44 (deterministic output).
- **SA-7 — *Deferred 2026-07-15*** to backlog B-7 (changed-code review surface), which is now its permanent home. The trigger-decision concern raised during disposition (knowing files changed is the input to the re-analysis decision) was recognized as a distinct capability — a pipeline-side staleness check — recorded as backlog B-21 with B-19 as its eventual viewer presentation.
- **SA-8 — *Deferred 2026-07-15*** to backlog B-8 (partial analysis of files with syntax errors), which is now its permanent home. The one v1 accommodation it needs — a format-version field in structured reports so a future "partially analyzed" status is a versioned format change rather than a silent one — was folded into FR-42 (AC-42.4).

## 10. Clarifications needed

Requirements affected by each item are written to the most conservative reading and flagged "(pending clarification)" where applicable.

- **C-1 — *Resolved 2026-07-15.*** "CLI entry points" means interpretation (a): packaging-declared console scripts only. FR-10's pending flag removed. The blind spot — bare scripts with neither a `__main__` block nor packaging metadata (common in legacy and AI-generated code) — is accepted for v1 and recorded as backlog B-20 (a detector for interpretation (b)/(c)); FR-9 catches most directly invoked scripts that do use a `__main__` guard.
- **C-2 — *Resolved 2026-07-15.*** Threshold confirmed: re-analysis after ≤ 5 changed files completes in ≤ 30 seconds on the reference machine. Set as the stakeholder's tolerated live-session wait, independent of engine feasibility; the OQ-1 prototype verifies feasibility, and failure there reopens FR-30 as a recorded trade-off. Folded into FR-30.
- **C-3 — *Resolved 2026-07-15.*** Reference machine defined in the §4.8 preamble: 4-core/8-thread x86-64 (reference: Intel Core i7-4700HQ), 24 GB RAM, SATA SSD, reference Linux environment per FR-31; stated as class-plus-reference so hardware upgrades don't trigger revision.
- **C-4 — *Resolved 2026-07-15.*** WSL2 is a tested support target with a documented Linux-filesystem condition on the performance bounds; folded into FR-31 and §6 item 20.
- **C-5 — *Resolved 2026-07-15.*** The Python analyzer recognizes `.py` files and extensionless files with a Python shebang first line; folded into FR-1 (AC-1.4, AC-1.5).
- **C-6 — *Resolved 2026-07-15.*** The readers/writers pivot stays out of scope for v1, with the recorded expectation that it is among the first backlog items implemented (backlog B-1). Rationale: done honestly, attribute resolution under Python dynamism is a feature the size of the call graph, not a patch; an unreliable version is worse than none given the FR-14 posture; and the portfolio tiebreaker governs. §6 item 1 documents the v1 manual workaround.

Step-2 gate check (2026-07-16) — residual ambiguities found on the final pass and resolved:

- **C-7 — *Resolved 2026-07-16.*** Reference Linux environment defined: enterprise-Linux-class distribution, reference AlmaLinux 9; folded into the §4.8 preamble.
- **C-8 — *Resolved 2026-07-16.*** Initially resolved as design-stage designation against recorded criteria; superseded the same day by stakeholder hard-designation: **Django core** is the performance reference (FR-29 bound asserted, AC-29.1) and **pandas** is the dynamism/robustness benchmark (run-to-completion asserted, time bound not, AC-29.3). Folded into the §4.8 preamble and FR-29; exact versions/commits pinned at design stage.
- **C-9 — *Resolved 2026-07-16.*** FR-24's "dependents" defined as the transitive closure of reverse dependency edges, as the correctness floor; design may narrow only by equivalence-preserving means; folded into FR-24.
- **C-10 — *Resolved 2026-07-16.*** "The run's diagnostics" defined as a per-run structured artifact under FR-42's rules; folded into the §4 conventions block.

Engine-evaluation pass (2026-07-18):

- **C-11 — *Resolved 2026-07-18.*** FR-14's "statically possible targets" admitted two readings once the engine evaluation showed the selected precise engine soundly declines sites for which candidates *are* syntactically determinable: (a) determinable by the configured resolution pipeline, or (b) determinable by any static means. Resolved as interpretation (a), with the pipeline's composition fixed by the amended §6 item 10 and the known undeterminable classes documented (measured on the reference benchmarks: bare-name value-flow dispatch ≈ 46% of unresolved sites; un-narrowed attribute dispatch). A narrowed method-dispatch over-approximation layer was evaluated (FINDINGS-namematch.md, FINDINGS-session5.md Part 3) and deferred to the backlog by the portfolio tiebreaker — the same discipline as C-6. Folded into FR-14.

## 11. Pre-approval review checklist

The 3–5 items most in need of human review before this document is approved:

1. ~~Confirm or replace the FR-30 re-analysis placeholder (C-2)~~ — *resolved 2026-07-15: ≤ 5 changed files in ≤ 30 s on the reference machine, confirmed as the stakeholder's live-session tolerance.*
2. ~~Resolve the meaning of "CLI entry points" (C-1)~~ — *resolved 2026-07-15: interpretation (a), packaging-declared only; the bare-script detector for (b)/(c) is backlog B-20.*
3. ~~Decide the v1 status of the readers/writers pivot (C-6)~~ — *resolved 2026-07-15: out of scope for v1, first backlog item expected to be implemented (B-1); §6 item 1 documents the manual workaround.*
4. ~~Disposition the final two Suggested Additions (§9)~~ — *completed 2026-07-15: SA-1 through SA-6 promoted (FR-39–FR-44); SA-7 deferred to backlog B-7 (with the staleness-check split recorded as B-21); SA-8 deferred to backlog B-8 (with AC-42.4 as its v1 insurance).*
5. ~~Confirm OQ-7's working assumption and resolution path (implementation language of the tool)~~ — *confirmed 2026-07-15: requirements remain implementation-language-neutral; the language and engine choices are one coupled decision, resolved together at OQ-1.*
