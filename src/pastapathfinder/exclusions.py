"""The layered exclusion rule engine: conventions, `.gitignore` files, user overrides.

design.md §3.3 (`exclusions`, normative), §5.5, D11, §8-O1 (directory pruning);
requirements FR-2 (AC-2.1/2.2), FR-3 (AC-3.1/3.2), FR-4 (AC-4.1/4.2/4.3), FR-5's data,
OQ-3's settled list.

One `RuleSet` answers one question — "is this path excluded, and by which rule?" — for
every consumer: `discovery` prunes with it (task 1.4), `reports` renders its records
(task 1.5). Every answer carries `(pattern, source)`, so no exclusion is ever
unattributed (FR-5, EC-8).

**Precedence (design.md §3.3), highest first:**

1. user `reinclude` — the highest-precedence negation (FR-4, AC-4.1)
2. user `exclude`                                       → `user:exclude`
3. every `.gitignore` in the tree, deepest first        → `gitignore:<relpath>`
4. the v1 Python convention set                         → `default:python`
5. the common (language-independent) convention set     → `default:common`

The first layer that decides wins, and within a layer the last matching pattern wins —
gitignore's own rule, so a `!` line inside a `.gitignore` (or a deeper `.gitignore`)
overrides a broader one above it. D11 puts every source through one matcher, `pathspec`
with gitignore/gitwildmatch semantics, so the three rule sources cannot drift apart.
(`pathspec` 1.x renamed the `gitwildmatch` factory to `gitignore` and deprecated the old
name; the semantics D11 names are unchanged, so this module spells it the new way.)

Unmatched rules are not errors (AC-2.2): a `RuleSet` is a matcher, not a checklist.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec, Pattern
from pathspec.patterns.gitignore import GitIgnorePatternError
from pathspec.util import lookup_pattern

from pastapathfinder.schema import Diag

# ---------------------------------------------------------------------------
# The rule sources (design.md §3.3's attribution vocabulary)
# ---------------------------------------------------------------------------

#: Language-independent conventions. D11 keeps these in their own set rather than
#: duplicating them into every per-language set, so attribution stays truthful when a
#: second language arrives.
SOURCE_COMMON = "default:common"

#: The per-language convention set; v1 ships Python's (FR-2).
SOURCE_PYTHON = "default:python"

#: A pattern from the user's `[exclude] add` list (AC-4.2).
SOURCE_USER_EXCLUDE = "user:exclude"

#: The user's `[exclude] reinclude` list. Never appears in an `ExclusionRecord` — a
#: reinclude match means the path was *not* excluded — but the layer is labelled for
#: symmetry and for error messages.
SOURCE_USER_REINCLUDE = "user:reinclude"

#: `.gitignore` attribution is per file, so the source names the file (AC-3.1).
GITIGNORE_SOURCE_PREFIX = "gitignore:"

GITIGNORE_FILENAME = ".gitignore"


def gitignore_source(relpath: str) -> str:
    """The `source` attribution for a `.gitignore` at root-relative `relpath`."""
    return f"{GITIGNORE_SOURCE_PREFIX}{relpath}"


# ---------------------------------------------------------------------------
# The convention sets (design.md §3.3, normative; OQ-3's settled list)
# ---------------------------------------------------------------------------

#: Normative, design.md §3.3. Language-independent.
COMMON_CONVENTIONS: tuple[str, ...] = (".git/",)

#: Normative, design.md §3.3 — the v1 Python convention set settling OQ-3, reproduced in
#: the design's order. Changing this list is a design change (CLAUDE.md rule 4), not a
#: convenience: it decides what every run silently declines to analyze.
PYTHON_CONVENTIONS: tuple[str, ...] = (
    "venv/",
    ".venv/",
    "env/",
    ".env/",
    "virtualenv/",
    "build/",
    "dist/",
    "__pycache__/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    ".mypy_cache/",
    ".pytest_cache/",
    "node_modules/",
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_PATTERN_FACTORY = lookup_pattern("gitignore")


class InvalidPatternError(ValueError):
    """A pattern is not valid gitignore syntax, or can never match anything.

    AC-4.3: a user pattern that cannot work terminates the run with an error naming the
    pattern — never a silent ignore. That is also why an *inert* pattern (a comment, a
    blank line, a bare separator) is rejected when it arrives from user configuration:
    accepting it would silently drop an exclusion the user asked for.
    """


def compile_pattern(text: str, *, allow_inert: bool = False) -> Pattern:
    """Compile one gitignore pattern.

    `allow_inert=True` is for `.gitignore` files, where comments and blank lines are
    ordinary content; user configuration uses the strict form (AC-4.3).
    """
    if not isinstance(text, str):
        raise InvalidPatternError(f"exclusion pattern must be a string, got {text!r}")
    try:
        pattern = _PATTERN_FACTORY(text)
    except GitIgnorePatternError as exc:
        raise InvalidPatternError(f"invalid exclusion pattern {text!r}: {exc}") from exc
    if pattern.include is None and not allow_inert:
        raise InvalidPatternError(
            f"exclusion pattern {text!r} matches nothing (a comment, a blank line, or a "
            f"bare separator); remove it or write a pattern that can match"
        )
    return pattern


# ---------------------------------------------------------------------------
# Records and layers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    """One excluded path with the rule that excluded it (FR-5; design.md §5.3's shape).

    `path` is root-relative POSIX. Per D17 and §8-O1 an excluded *directory* is one
    record whose contents are never enumerated, so `is_dir` is what tells a reader
    whether this record stands for one file or for a pruned subtree.
    """

    path: str
    is_dir: bool
    pattern: str
    source: str


@dataclass(frozen=True, slots=True)
class _Layer:
    """One rule source: its patterns, the directory they are relative to, its label.

    `base` is the root-relative POSIX directory the patterns are anchored at — `""` for
    every layer except a nested `.gitignore`, whose patterns are interpreted relative to
    its own directory (AC-3.1). `patterns` is parallel to the compiled spec, so a match
    can name the pattern text that produced it.
    """

    source: str
    base: str
    spec: GitIgnoreSpec
    patterns: tuple[str, ...]
    negates: bool = False

    def decide(self, candidate: str) -> tuple[bool, str] | None:
        """`(excluded, pattern)` when this layer decides `candidate`, else None."""
        relative = _strip_base(candidate, self.base)
        if relative is None:
            return None
        result = self.spec.check_file(relative)
        if result.include is None or result.index is None:
            return None
        pattern = self.patterns[result.index]
        if self.negates:
            # A reinclude layer only ever keeps things; a `!` line inside it is not a
            # request to exclude, so it simply does not decide.
            return (False, pattern) if result.include else None
        return bool(result.include), pattern


def _strip_base(candidate: str, base: str) -> str | None:
    """`candidate` expressed relative to `base`, or None when it is not under `base`."""
    if not base:
        return candidate
    prefix = f"{base}/"
    if candidate.startswith(prefix) and len(candidate) > len(prefix):
        return candidate[len(prefix) :]
    return None


def _compile_layer(
    source: str,
    patterns: Iterable[str],
    *,
    base: str = "",
    negates: bool = False,
) -> _Layer:
    """Compile a layer whose patterns must all be valid and non-inert (AC-4.3)."""
    texts = tuple(patterns)
    compiled = [compile_pattern(text) for text in texts]
    return _Layer(
        source=source,
        base=base,
        spec=GitIgnoreSpec(compiled),
        patterns=texts,
        negates=negates,
    )


# ---------------------------------------------------------------------------
# The rule set
# ---------------------------------------------------------------------------


class RuleSet:
    """The composed exclusion rules. Build one with `build_ruleset()`.

    `match()` is the whole interface: given a root-relative POSIX path and whether it is
    a directory, it returns the `ExclusionRecord` that excludes it, or None. Directory
    patterns (`venv/`) match directories only, which is why `is_dir` is not optional
    guesswork — the caller knows, and gitignore semantics need it.
    """

    __slots__ = ("_diagnostics", "_layers")

    def __init__(self, layers: Sequence[_Layer], diagnostics: Sequence[Diag] = ()) -> None:
        self._layers = tuple(layers)
        self._diagnostics = tuple(diagnostics)

    @property
    def diagnostics(self) -> tuple[Diag, ...]:
        """`gitignore_problem` entries collected while building (AC-3.2)."""
        return self._diagnostics

    def match(self, path: str, *, is_dir: bool) -> ExclusionRecord | None:
        """The record excluding `path`, or None when no rule excludes it."""
        if not path or path == ".":
            # The root itself is never a candidate for exclusion: it is the run's input.
            return None
        candidate = f"{path}/" if is_dir else path
        for layer in self._layers:
            decision = layer.decide(candidate)
            if decision is None:
                continue
            excluded, pattern = decision
            if not excluded:
                return None
            return ExclusionRecord(path=path, is_dir=is_dir, pattern=pattern, source=layer.source)
        return None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _warn_stderr(message: str) -> None:
    print(f"pastapathfinder: warning: {message}", file=sys.stderr)


def _record(diagnostics: list[Diag], warn: Callable[[str], None], diagnostic: Diag) -> None:
    """AC-3.2 wants both halves: a warning the user sees and a diagnostics entry."""
    diagnostics.append(diagnostic)
    warn(diagnostic.message)


def _read_gitignore(
    root: Path,
    rel_dir: str,
    diagnostics: list[Diag],
    warn: Callable[[str], None],
) -> _Layer | None:
    """Compile the `.gitignore` in `rel_dir`, tolerating its failures (AC-3.2).

    An unreadable file drops out entirely; an unparseable line drops out on its own,
    leaving that file's other patterns in force. Both are warned about naming file (and
    line), recorded as `gitignore_problem`, and the run continues on the remaining rules.
    """
    relpath = f"{rel_dir}/{GITIGNORE_FILENAME}" if rel_dir else GITIGNORE_FILENAME
    try:
        text = (root / relpath).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _record(
            diagnostics,
            warn,
            Diag(
                kind="gitignore_problem",
                path=relpath,
                message=f"{relpath}: cannot be read ({exc}); its patterns are not applied",
            ),
        )
        return None

    compiled: list[Pattern] = []
    texts: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            pattern = compile_pattern(line, allow_inert=True)
        except InvalidPatternError as exc:
            _record(
                diagnostics,
                warn,
                Diag(
                    kind="gitignore_problem",
                    path=relpath,
                    line=number,
                    message=(
                        f"{relpath}:{number}: {exc}; the line is ignored and the run "
                        f"continues on the remaining rules"
                    ),
                ),
            )
            continue
        compiled.append(pattern)
        texts.append(line)

    return _Layer(
        source=gitignore_source(relpath),
        base=rel_dir,
        spec=GitIgnoreSpec(compiled),
        patterns=tuple(texts),
    )


def _collect_gitignores(
    root: Path,
    above: Sequence[_Layer],
    below: Sequence[_Layer],
    diagnostics: list[Diag],
    warn: Callable[[str], None],
) -> list[_Layer]:
    """Every `.gitignore` under `root`, in precedence order (deepest first).

    The walk prunes exactly as discovery will (§3.3, §8-O1): a directory the rules
    already exclude is not descended into, so `venv/` and `node_modules/` cost one
    decision rather than a subtree traversal, and the `.gitignore` files inside them —
    which govern code that is not being analyzed — are never read. `above`/`below` are
    the layers that outrank and underrank the `.gitignore`s, so every pruning decision
    is made with the same precedence the finished rule set will use.
    """
    found: list[_Layer] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        if GITIGNORE_FILENAME in filenames:
            layer = _read_gitignore(root, rel_dir, diagnostics, warn)
            if layer is not None:
                # Later finds are deeper (or siblings, whose bases cannot overlap), and
                # a deeper `.gitignore` outranks its ancestors.
                found.insert(0, layer)
        dirnames.sort()  # D12: the walk order fixes diagnostic order, so pin it.
        ruleset = RuleSet([*above, *found, *below])
        dirnames[:] = [
            name
            for name in dirnames
            if ruleset.match(f"{rel_dir}/{name}" if rel_dir else name, is_dir=True) is None
        ]
    return found


def build_ruleset(
    root: Path | str,
    *,
    exclude: Iterable[str] = (),
    reinclude: Iterable[str] = (),
    warn: Callable[[str], None] = _warn_stderr,
) -> RuleSet:
    """Compose the design.md §3.3 rule layers for the tree at `root`.

    `exclude` and `reinclude` are the user's patterns (`config.Config`'s fields). An
    invalid one raises `InvalidPatternError` and terminates the run (AC-4.3); a broken
    `.gitignore` does not (AC-3.2) — it is warned about, recorded in the returned rule
    set's `diagnostics`, and skipped.
    """
    root = Path(root)
    above = [
        _compile_layer(SOURCE_USER_REINCLUDE, reinclude, negates=True),
        _compile_layer(SOURCE_USER_EXCLUDE, exclude),
    ]
    below = [
        _compile_layer(SOURCE_PYTHON, PYTHON_CONVENTIONS),
        _compile_layer(SOURCE_COMMON, COMMON_CONVENTIONS),
    ]
    diagnostics: list[Diag] = []
    gitignores = _collect_gitignores(root, above, below, diagnostics, warn)
    return RuleSet([*above, *gitignores, *below], diagnostics)
