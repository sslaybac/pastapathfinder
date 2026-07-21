"""User configuration: loading and validating `.pastapathfinder.toml`.

design.md §3.2 (`config`'s responsibility and interface), §5.5 (the file format), D11;
requirements FR-4 (AC-4.1/4.2 data, AC-4.3 failure path).

The file is optional: a codebase with no `.pastapathfinder.toml` and no `--config`
yields the empty `Config`, and the run proceeds on defaults and `.gitignore`s alone.
Everything this module rejects, it rejects loudly and by name — AC-4.3's requirement is
that a configuration the user believes is in force never quietly is not, so an unknown
key, a mistyped value, and an unusable pattern are all run-terminating errors rather
than best-effort interpretations.

Pattern *syntax* is validated here (via `exclusions.compile_pattern`, so there is one
definition of "valid pattern" for the whole tool); pattern *composition* is
`exclusions.build_ruleset`'s job.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from pastapathfinder.exclusions import InvalidPatternError, compile_pattern

#: design.md §3.2: the default configuration file, looked for at the analyzed root.
CONFIG_FILENAME = ".pastapathfinder.toml"

#: The §5.5 surface, exhaustively. Anything else in the file is a typo, and a typo that
#: is ignored is an exclusion rule the user thinks they have and does not.
_TABLES: dict[str, tuple[str, ...]] = {
    "exclude": ("add", "reinclude"),
    "output": ("dir",),
}


class ConfigError(Exception):
    """The configuration cannot be used; the run terminates (AC-4.3).

    `cli.main()` maps this to exit 2 with the message on stderr (D10), so every message
    here names the file and the offending key, pattern, or value.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """The user's configuration, or the empty default when there is no file.

    design.md §3.2's interface: `exclude` and `reinclude` are gitwildmatch patterns for
    `exclusions.build_ruleset`; `out_dir` overrides §5.1's derived output directory
    (whose derivation and resolution belong to the runner, task 1.5).
    """

    exclude: list[str] = field(default_factory=list)
    reinclude: list[str] = field(default_factory=list)
    out_dir: str | None = None


def load_config(root: Path, explicit: Path | None = None) -> Config:
    """Load `explicit`, or `<root>/.pastapathfinder.toml` when it exists (design.md §3.2).

    An explicitly requested file that is missing is an error — the user named it, so
    falling back to defaults would analyze the wrong thing silently. A missing default
    file is not an error.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"configuration file not found: {path}")
    else:
        path = Path(root) / CONFIG_FILENAME
        if not path.is_file():
            return Config()

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in configuration file {path}: {exc}") from exc

    return _parse(data, path)


def _fail(path: Path, problem: str) -> NoReturn:
    raise ConfigError(f"{path}: {problem}")


def _reject_unknown(
    keys: Mapping[str, Any], allowed: tuple[str, ...], path: Path, where: str
) -> None:
    location = f"[{where}] " if where else ""
    for key in keys:
        if key not in allowed:
            _fail(
                path,
                f"{location}unknown key {key!r} (expected one of: {', '.join(allowed)})",
            )


def _table(value: object, name: str, path: Path) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail(path, f"[{name}] must be a table")
    return value


def _patterns(value: object, name: str, path: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(path, f"{name} must be a list of patterns")
    result: list[str] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, str):
            _fail(path, f"{name}[{position}] must be a string, got {entry!r}")
        try:
            compile_pattern(entry)
        except InvalidPatternError as exc:
            # AC-4.3: name the pattern, terminate the run, never silently ignore it.
            _fail(path, f"{name}[{position}]: {exc}")
        result.append(entry)
    return result


def _parse(data: Mapping[str, Any], path: Path) -> Config:
    _reject_unknown(data, tuple(_TABLES), path, "")
    exclude_table = _table(data.get("exclude"), "exclude", path)
    output_table = _table(data.get("output"), "output", path)
    _reject_unknown(exclude_table, _TABLES["exclude"], path, "exclude")
    _reject_unknown(output_table, _TABLES["output"], path, "output")

    out_dir = output_table.get("dir")
    if out_dir is not None and (not isinstance(out_dir, str) or not out_dir.strip()):
        _fail(path, f"[output] dir must be a non-empty string, got {out_dir!r}")

    return Config(
        exclude=_patterns(exclude_table.get("add"), "[exclude] add", path),
        reinclude=_patterns(exclude_table.get("reinclude"), "[exclude] reinclude", path),
        out_dir=out_dir,
    )
