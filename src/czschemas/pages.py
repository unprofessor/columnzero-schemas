"""The browsable face of the tree: one HTML page per mutable directory.

These pages are derived exactly as the JSON indexes are -- regenerated whole from the
releases on every run, never read back as input.  A page is a *second projection of the
tree*, not a rendering of the first projection: nothing here parses an `index.json`, so
the published JSON stays free to change shape without a page silently depending on it.

No page is written inside a release directory.  Everything below one is canonical and
append-only, so a page written there would be frozen at whatever markup shipped first,
and `write_canonical` would fail the build on the first restyle.  Release contents are
reachable from the line page instead, which links each version's schemas directly.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Iterable

from .model import CompatLine, ReleaseKey, Version

PAGE_NAME = "index.html"

# Inlined because the pages must stand alone: a stylesheet at a shared path would be one
# more derived resource to purge, and a release URL that 404s its own styling is worse
# than no styling.  It is small enough that the duplication costs less than the coupling.
STYLE = """
:root { --fg:#16181d; --dim:#61666e; --bg:#fff; --line:#e3e6ea; --accent:#0b5fff; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6e8ec; --dim:#9aa1ab; --bg:#14161a; --line:#2a2e35; --accent:#7aa2ff; }
}
* { box-sizing: border-box; }
body { margin:0 auto; padding:2.5rem 1.25rem 5rem; max-width:52rem; background:var(--bg);
  color:var(--fg); line-height:1.55;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
code, .u { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.875em; }
nav { font-size:.875rem; color:var(--dim); margin-bottom:1.5rem; }
nav a { color:var(--dim); }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em; }
h2 { font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
  margin:2.5rem 0 .75rem; font-weight:600; }
.lede { color:var(--dim); margin:0 0 1rem; }
ul { list-style:none; padding:0; margin:0; }
li { padding:.7rem 0; border-top:1px solid var(--line); }
li:last-child { border-bottom:1px solid var(--line); }
.name { font-weight:600; }
.meta { color:var(--dim); font-size:.8125rem; margin-top:.15rem; }
.u { display:block; color:var(--dim); font-size:.8125rem; margin-top:.2rem;
  overflow-wrap:anywhere; }
footer { margin-top:3.5rem; padding-top:1rem; border-top:1px solid var(--line);
  color:var(--dim); font-size:.8125rem; }
"""


def _document(title: str, crumbs: list[tuple[str, str | None]], body: str) -> bytes:
    trail = " / ".join(
        f'<a href="{html.escape(href)}">{html.escape(label)}</a>' if href else html.escape(label)
        for label, href in crumbs
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f"<nav>{trail}</nav>\n{body}\n"
        "<footer>Immutable schema registry. Canonical URLs never change or disappear; "
        "alias URLs move forward with each release.</footer>\n"
        "</body>\n</html>\n"
    ).encode()


def _entry(name: str, href: str, meta: str = "", url: str = "") -> str:
    parts = [f'<div class="name"><a href="{html.escape(href)}">{html.escape(name)}</a></div>']
    if meta:
        parts.append(f'<div class="meta">{meta}</div>')
    if url:
        parts.append(f'<code class="u">{html.escape(url)}</code>')
    return "<li>" + "".join(parts) + "</li>"


def _list(entries: Iterable[str]) -> str:
    rendered = "\n".join(entries)
    return f"<ul>\n{rendered}\n</ul>" if rendered else '<p class="lede">Nothing published yet.</p>'


def _alias_entries(schemas: list[tuple[ReleaseKey, Any]], prefix: str) -> list[str]:
    """One row per alias: where it points now, and the version currently serving it."""
    entries = []
    for key, schema in sorted(schemas, key=lambda pair: pair[1].name):
        dialect = f" &middot; {html.escape(schema.dialect)}" if schema.dialect else ""
        entries.append(
            _entry(
                schema.name,
                schema.name,
                f"serving {html.escape(str(key.version))}{dialect}",
                f"{prefix}/{schema.name}",
            )
        )
    return entries


def write_all(
    site: Path,
    root: str,
    releases: dict[ReleaseKey, Any],
    by_line: dict[Any, tuple[ReleaseKey, Any]],
    by_project: dict[Any, tuple[ReleaseKey, Any]],
) -> int:
    """Regenerate every page from the tree.  Returns the number of pages written."""
    projects = sorted({key.project for key in releases})
    lines: dict[str, set[CompatLine]] = {}
    versions: dict[tuple[str, str], set[Version]] = {}
    for key in releases:
        lines.setdefault(key.project, set()).add(key.compat)
        versions.setdefault((key.project, str(key.compat)), set()).add(key.version)

    written = 0

    def emit(path: Path, body: bytes) -> None:
        nonlocal written
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        written += 1

    # Root: the projects, and the one fetch that mirrors everything.
    emit(site / PAGE_NAME, _document(
        "Schema registry",
        [("schemas", None)],
        "<h1>Schema registry</h1>\n"
        '<p class="lede">Immutable, digest-pinned JSON Schema releases.</p>\n'
        "<h2>Projects</h2>\n"
        + _list(
            _entry(project, f"{project}/",
                   f"{len(lines[project])} compatibility line"
                   f"{'' if len(lines[project]) == 1 else 's'}")
            for project in projects
        )
        + '\n<h2>Whole registry</h2>\n<ul><li><div class="name">'
        f'<a href="catalog.json">catalog.json</a></div>'
        '<div class="meta">Every published schema, one flat entry each.</div>'
        f'<code class="u">{html.escape(root)}/catalog.json</code></li></ul>',
    ))

    for project in projects:
        has_latest = any(bucket[0] == project for bucket in by_project)
        ordered = sorted(lines[project], key=lambda line: line.sort_key, reverse=True)
        entries = []
        if has_latest:
            entries.append(_entry(
                "latest", "latest/",
                "Newest stable release across every line. Moves on each release.",
                f"{root}/{project}/latest/",
            ))
        for line in ordered:
            count = len(versions[(project, str(line))])
            entries.append(_entry(
                line.segment, f"{line.segment}/",
                f"{count} release{'' if count == 1 else 's'}",
                f"{root}/{project}/{line.segment}/",
            ))
        emit(site / project / PAGE_NAME, _document(
            project,
            [("schemas", "../"), (project, None)],
            f"<h1>{html.escape(project)}</h1>\n"
            '<p class="lede">Pick a compatibility line. Within a line, schemas stay '
            "backward compatible; across lines they need not.</p>\n"
            "<h2>Lines</h2>\n" + _list(entries),
        ))

        if has_latest:
            members = [alias for bucket, alias in by_project.items() if bucket[0] == project]
            emit(site / project / "latest" / PAGE_NAME, _document(
                f"{project} latest",
                [("schemas", "../../"), (project, "../"), ("latest", None)],
                f"<h1>{html.escape(project)} <span class=\"meta\">latest</span></h1>\n"
                '<p class="lede">These URLs follow the newest stable release, including '
                "across a breaking-change boundary. Pin a version instead if that matters."
                "</p>\n<h2>Schemas</h2>\n"
                + _list(_alias_entries(members, f"{root}/{project}/latest")),
            ))

        for line in ordered:
            segment = line.segment
            members = [
                alias for bucket, alias in by_line.items()
                if bucket[0] == project and bucket[1] == str(line)
            ]
            release_entries = []
            for version in sorted(versions[(project, segment[1:])], key=lambda v: v.sort_key,
                                  reverse=True):
                key = ReleaseKey(project, version)
                release = releases[key]
                files = ", ".join(
                    f'<a href="{html.escape(str(version))}/{html.escape(schema.name)}">'
                    f"{html.escape(schema.name)}</a>"
                    for schema in sorted(release.schemas, key=lambda s: s.name)
                )
                tag = " &middot; prerelease" if version.is_prerelease else ""
                release_entries.append(
                    "<li>"
                    f'<div class="name">{html.escape(str(version))}{tag}</div>'
                    f'<div class="meta">{files or "no schemas"}</div>'
                    f'<div class="meta"><a href="{html.escape(str(version))}/index.json">'
                    "index.json</a> &middot; "
                    f'<a href="{html.escape(str(version))}/artifact.lock">artifact.lock</a>'
                    "</div></li>"
                )
            emit(site / project / segment / PAGE_NAME, _document(
                f"{project} {segment}",
                [("schemas", "../../"), (project, "../"), (segment, None)],
                f"<h1>{html.escape(project)} <span class=\"meta\">{html.escape(segment)}</span>"
                "</h1>\n"
                '<p class="lede">Reference the line URL to track compatible updates, or a '
                "version URL to pin bytes that can never change.</p>\n"
                "<h2>Line URLs</h2>\n"
                + _list(_alias_entries(members, f"{root}/{project}/{segment}"))
                + "\n<h2>Releases</h2>\n"
                + _list(release_entries),
            ))
    return written
