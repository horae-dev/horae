# SPDX-License-Identifier: Apache-2.0
"""Parameter templating and the canonical pipeline digest (#17)."""

import datetime
from typing import Any

import pytest

from horae.contracts import Pipeline
from horae.engine import (
    TemplateError,
    canonical_json,
    canonical_pipeline,
    make_context,
    pipeline_digest,
    render,
    render_string,
    variables,
)

NOW = datetime.datetime(2026, 10, 27, 23, 30, tzinfo=datetime.UTC)


def test_context_date_is_local_to_the_time_zone() -> None:
    assert make_context(NOW, "UTC").vars == {"date": "2026-10-27"}
    paris = make_context(NOW, "Europe/Paris", {"vault": "Main"})
    assert paris.vars == {"date": "2026-10-28", "vault": "Main"}
    assert variables(paris) == {
        "now": "2026-10-27T23:30:00Z",
        "timezone": "Europe/Paris",
        "date": "2026-10-28",
        "vault": "Main",
    }


@pytest.mark.parametrize(
    ("now", "timezone", "extra"),
    [
        (NOW.replace(tzinfo=None), "UTC", None),
        (NOW, "Mars/Olympus", None),
        (NOW, "UTC", {"date": "2000-01-01"}),
        (NOW, "UTC", {"now": "x"}),
    ],
)
def test_context_rejects(
    now: datetime.datetime, timezone: str, extra: dict[str, str] | None
) -> None:
    with pytest.raises(ValueError, match=r"aware|time zone|built in"):
        make_context(now, timezone, extra)


def test_render_substitutes_known_variables() -> None:
    names = {"date": "2026-10-27", "x": "1"}
    assert render_string("Daily/{{date}}.assistant.md", names) == "Daily/2026-10-27.assistant.md"
    assert render_string("{{ date }}-{{x}}{{x}}", names) == "2026-10-27-11"
    assert render_string("no template", names) == "no template"
    assert render_string("{{{date}}}", names) == "{2026-10-27}"  # single braces are literal


def test_render_walks_params_but_not_keys_or_scalars() -> None:
    params: dict[str, Any] = {"{{date}}": [{"p": "{{date}}"}, 2, True, None], "n": 1.5}
    assert render(params, {"date": "D"}) == {"{{date}}": [{"p": "D"}, 2, True, None], "n": 1.5}


@pytest.mark.parametrize(
    "text",
    [
        "{{unknown}}",
        "{{date",
        "date}}",
        "{{ date | upper }}",
        "{{date.year}}",
        "{{}}",
        "{{{{date}}}}",
        "{{ __import__('os') }}",
    ],
)
def test_render_is_strict(text: str) -> None:
    with pytest.raises(TemplateError):
        render_string(text, {"date": "D"})


def test_render_is_single_pass() -> None:
    """A value containing a placeholder is inserted literally, never expanded."""
    assert render_string("{{a}}", {"a": "{{b}}", "b": "secret"}) == "{{b}}"


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": ["é", 1.5]}) == '{"a":["é",1.5],"b":1}'.encode()
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json(float("nan"))


def _pipeline(text: dict[str, Any]) -> Pipeline:
    return Pipeline.model_validate(text)


def test_digest_ignores_spelling_but_not_meaning() -> None:
    bare = _pipeline({"name": "p", "steps": [{"collect": ["a.b"]}]})
    mapped = _pipeline({"name": "p", "steps": [{"collect": [{"a.b": {}}]}]})
    assert pipeline_digest(bare) == pipeline_digest(mapped)
    assert canonical_pipeline(bare) == {"name": "p", "steps": [{"collect": [{"a.b": {}}]}]}
    null_param = _pipeline({"name": "p", "steps": [{"collect": [{"a.b": {"x": None}}]}]})
    assert pipeline_digest(null_param) != pipeline_digest(bare)  # `null` params are kept

    llm = {"skill": "s/k", "model": "m", "budget": {"usd": 0.1, "tokens_out": 5}}
    implicit = _pipeline({"name": "p", "steps": [{"llm": llm}]})
    explicit = _pipeline({"name": "p", "steps": [{"llm": {**llm, "on_unchanged_input": "skip"}}]})
    assert pipeline_digest(implicit) == pipeline_digest(explicit)

    other = _pipeline({"name": "p", "steps": [{"collect": [{"a.b": {"days": 2}}]}]})
    assert pipeline_digest(other) != pipeline_digest(bare)
    assert len(pipeline_digest(bare)) == 64
