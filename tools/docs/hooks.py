"""MkDocs hooks for the Horae docs site (``mkdocs.yml``).

Three jobs, all at build time, nothing written to the tree:

1. Include files that live outside ``docs/`` as pages (the README, ``schemas/README.md``, the
   SDK README), so the existing files stay where they are and their links keep working.
2. Generate the CLI reference from the Typer app (``horae.cli.app``): it can never be stale.
3. Rewrite relative links that leave ``docs/`` (``../../deploy/...``, ``../../schemas/README.md``)
   to the site page that includes the target, or else to the file on GitHub.

Needs the project installed next to MkDocs: ``uv sync --no-dev --group docs``.
"""

from __future__ import annotations

import html
import inspect
import posixpath
import re
from pathlib import Path
from typing import Any

import typer.main
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page

REPO_URL = "https://github.com/horae-dev/horae"
ROOT = Path(__file__).resolve().parents[2]

# Page of the site -> the repository file it is a copy of.
INCLUDED = {
    "reference/runtime.md": "README.md",
    "reference/schemas.md": "schemas/README.md",
    "reference/sdk.md": "sdk/README.md",
}
SOURCE_TO_PAGE = {source: page for page, source in INCLUDED.items()}
CLI_PAGE = "reference/cli.md"

# Links in the README that only mean something on GitHub's own pages.
LITERAL = {"../../issues": f"{REPO_URL}/issues"}

# Anchors that are wrong in a source file this story may not edit; remove an entry when the
# file is fixed (security.md: the heading is "9. Reporting a vulnerability", so its id has the number).
ANCHOR_FIXES = {("security.md", "#reporting-a-vulnerability"): "#9-reporting-a-vulnerability"}

_LINK = re.compile(r"(\]\()([^)\s]+)(\s+\"[^\"]*\")?(\))")
_REFDEF = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s+)(\S+)", re.MULTILINE)
_FENCE = re.compile(r"^\s*(```+|~~~+)[\w+-]*\s*$")


def on_files(files: Files, config: MkDocsConfig) -> Files:
    for page, source in INCLUDED.items():
        text = (ROOT / source).read_text(encoding="utf-8")
        files.append(File.generated(config, page, content=text))
    files.append(File.generated(config, CLI_PAGE, content=_cli_reference()))
    return files


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    src_uri = page.file.src_uri
    origin = INCLUDED.get(src_uri)
    origin = origin if origin is not None else f"docs/{src_uri}"
    here = posixpath.dirname(origin)

    def rewrite(target: str) -> str:
        if (src_uri, target) in ANCHOR_FIXES:
            return ANCHOR_FIXES[(src_uri, target)]
        if target in LITERAL:
            return LITERAL[target]
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith(("#", "/")):
            return target
        path, hash_mark, anchor = target.partition("#")
        suffix = f"#{anchor}" if hash_mark else ""
        resolved = posixpath.normpath(posixpath.join(here, path))
        if resolved.startswith(".."):
            return target
        page_uri = SOURCE_TO_PAGE.get(resolved)
        if page_uri is None and resolved.startswith("docs/") and (ROOT / resolved).is_file():
            if origin.startswith("docs/"):
                return target  # docs to docs: untouched, MkDocs validates it
            page_uri = resolved.removeprefix("docs/")
        if page_uri is not None:
            relative = posixpath.relpath(page_uri, posixpath.dirname(src_uri) or ".")
            return relative + suffix
        disk = ROOT / resolved
        if disk.is_dir():
            return f"{REPO_URL}/tree/main/{resolved.rstrip('/')}{suffix}"
        if disk.is_file():
            return f"{REPO_URL}/blob/main/{resolved}{suffix}"
        return target  # unknown: left for MkDocs to report

    out: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            line = _LINK.sub(lambda m: f"{m[1]}{rewrite(m[2])}{m[3] or ''}{m[4]}", line)
            line = _REFDEF.sub(lambda m: f"{m[1]}{rewrite(m[2])}", line)
        out.append(line)
    return "\n".join(out)


# --- the CLI reference ------------------------------------------------------------------------


def _cli_reference() -> str:
    from horae.cli import app  # imported late: the hook module loads before the project is needed

    root = typer.main.get_command(app)
    lines = [
        "# CLI reference",
        "",
        "Generated at build time from the Typer app (`horae.cli:app`), so it matches the code of",
        "this build. `horae <command> --help` prints the same. Exit codes, the same for every",
        "command, are in [the runtime reference](runtime.md) (see Exit codes).",
        "",
    ]
    _render(root, "horae", 2, lines)
    return "\n".join(lines).rstrip() + "\n"


# Typer bundles its own copy of click (`typer._click`), so `click`'s classes cannot be used for
# isinstance: parameters are told apart by `param_type_name`, groups by `list_commands`.
def _render(command: Any, path: str, level: int, lines: list[str]) -> None:
    ctx = command.make_context(path.split()[-1], [], resilient_parsing=True)
    usage = " ".join([path, *command.collect_usage_pieces(ctx)])
    lines += [f"{'#' * level} `{path}`", ""]
    if command.help:
        # The docstring as written (it holds <placeholders> and {json}, which Markdown would eat).
        lines += ["```text", inspect.cleandoc(command.help), "```", ""]
    lines += ["Usage:", "", "```text", usage, "```", ""]
    arguments = [p for p in command.params if p.param_type_name == "argument"]
    options = [p for p in command.params if p.param_type_name == "option" and not _hidden(p)]
    if arguments:
        lines += ["| Argument | Description |", "|---|---|"]
        for arg in arguments:
            lines.append(f"| `{arg.human_readable_name}` | {_cell(_describe(arg))} |")
        lines.append("")
    if options:
        lines += ["| Option | Description |", "|---|---|"]
        for opt in options:
            names = ", ".join(f"`{n}`" for n in [*opt.opts, *opt.secondary_opts])
            lines.append(f"| {names} | {_cell(_describe(opt))} |")
        lines.append("")
    if hasattr(command, "list_commands"):
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is not None and not sub.hidden:
                _render(sub, f"{path} {name}", min(level + 1, 6), lines)


def _hidden(param: Any) -> bool:
    return bool(param.hidden) or param.name == "help"


def _describe(param: Any) -> str:
    help_text: Any = getattr(param, "help", None)
    parts = [html.escape(" ".join(str(help_text).split()), quote=False)] if help_text else []
    if param.required:
        parts.append("Required.")
    default = param.default
    if default not in (None, "", ()) and not callable(default) and param.show_default:
        if default is not False:
            parts.append(f"Default: `{default}`.")
    if param.envvar:
        envs = [param.envvar] if isinstance(param.envvar, str) else list(param.envvar)
        parts.append("Environment: " + ", ".join(f"`{e}`" for e in envs) + ".")
    return " ".join(parts)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
