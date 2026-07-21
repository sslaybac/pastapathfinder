"""User configuration loading and validation.

design.md §3.2 (interface), §5.5 (the file format); requirements FR-4 (AC-4.1/4.2 data,
AC-4.3 failure path).
"""

from pathlib import Path

import pytest

from pastapathfinder.config import CONFIG_FILENAME, Config, ConfigError, load_config
from pastapathfinder.exclusions import SOURCE_USER_EXCLUDE, build_ruleset

# design.md §5.5's example file, verbatim.
DESIGN_EXAMPLE = """\
[exclude]
add = ["generated/", "*.pb2.py"]       # gitwildmatch patterns
reinclude = ["vendor/keep_this/"]      # negates any default/gitignore match
[output]
dir = "/absolute/path"                 # optional; overrides the XDG default
"""


def write_config(root: Path, text: str, name: str = CONFIG_FILENAME) -> Path:
    path = root / name
    path.write_text(text)
    return path


def test_absent_default_file_yields_the_empty_config(tmp_path):
    config = load_config(tmp_path)

    assert config == Config()
    assert config.exclude == []
    assert config.reinclude == []
    assert config.out_dir is None


def test_the_design_example_file_parses(tmp_path):
    write_config(tmp_path, DESIGN_EXAMPLE)

    config = load_config(tmp_path)

    assert config.exclude == ["generated/", "*.pb2.py"]
    assert config.reinclude == ["vendor/keep_this/"]
    assert config.out_dir == "/absolute/path"


def test_an_explicit_path_overrides_the_default_file(tmp_path):
    write_config(tmp_path, '[exclude]\nadd = ["from_default/"]\n')
    explicit = write_config(tmp_path, '[exclude]\nadd = ["from_explicit/"]\n', "other.toml")

    assert load_config(tmp_path, explicit).exclude == ["from_explicit/"]


def test_a_missing_explicit_path_is_an_error(tmp_path):
    """The user named the file, so silently falling back would analyze the wrong tree."""
    missing = tmp_path / "nope.toml"

    with pytest.raises(ConfigError, match="nope.toml"):
        load_config(tmp_path, missing)


def test_empty_tables_are_accepted(tmp_path):
    write_config(tmp_path, "[exclude]\n[output]\n")

    assert load_config(tmp_path) == Config()


# ---------------------------------------------------------------------------
# AC-4.3 — nothing is silently ignored
# ---------------------------------------------------------------------------


def test_malformed_toml_terminates_the_run_naming_the_file(tmp_path):
    path = write_config(tmp_path, '[exclude\nadd = ["x/"]\n')

    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)

    assert str(path) in str(caught.value)
    assert "invalid TOML" in str(caught.value)


def test_invalid_pattern_terminates_the_run_naming_the_pattern(tmp_path):
    write_config(tmp_path, '[exclude]\nadd = ["ok/", "!"]\n')

    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)

    message = str(caught.value)
    assert "'!'" in message
    assert "[exclude] add[1]" in message


def test_invalid_reinclude_pattern_terminates_the_run(tmp_path):
    write_config(tmp_path, '[exclude]\nreinclude = ["!"]\n')

    with pytest.raises(ConfigError, match=r"\[exclude\] reinclude\[0\]"):
        load_config(tmp_path)


def test_inert_pattern_terminates_the_run(tmp_path):
    write_config(tmp_path, '[exclude]\nadd = ["# generated"]\n')

    with pytest.raises(ConfigError, match="matches nothing"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[excludes]\nadd = ["x/"]\n', "unknown key 'excludes'"),
        ('[exclude]\nadds = ["x/"]\n', "unknown key 'adds'"),
        ('[output]\ndirectory = "/tmp"\n', "unknown key 'directory'"),
        ('exclude = "generated/"\n', "[exclude] must be a table"),
        ('[exclude]\nadd = "generated/"\n', "[exclude] add must be a list"),
        ("[exclude]\nadd = [1]\n", "[exclude] add[0] must be a string"),
        ("[output]\ndir = 7\n", "[output] dir must be a non-empty string"),
        ('[output]\ndir = "  "\n', "[output] dir must be a non-empty string"),
    ],
)
def test_unusable_configuration_is_rejected_by_name(tmp_path, text, expected):
    write_config(tmp_path, text)

    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)

    assert expected in str(caught.value)


# ---------------------------------------------------------------------------
# The two halves together (FR-4)
# ---------------------------------------------------------------------------


def test_configured_patterns_reach_the_rule_set(tmp_path):
    """AC-4.1/4.2 end to end from the file the user actually edits."""
    write_config(
        tmp_path,
        '[exclude]\nadd = ["gen/"]\nreinclude = ["build/"]\n',
    )
    (tmp_path / "gen").mkdir()
    (tmp_path / "build").mkdir()
    config = load_config(tmp_path)

    ruleset = build_ruleset(
        tmp_path,
        exclude=config.exclude,
        reinclude=config.reinclude,
        warn=lambda message: pytest.fail(f"unexpected warning: {message}"),
    )

    excluded = ruleset.match("gen", is_dir=True)
    assert excluded is not None
    assert (excluded.pattern, excluded.source) == ("gen/", SOURCE_USER_EXCLUDE)
    assert ruleset.match("build", is_dir=True) is None
