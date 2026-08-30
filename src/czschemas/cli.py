"""Command line: plan, apply, verify.

`plan` is the review artifact.  A lock diff tells a reviewer a project, a version, and a
tag; it cannot tell them whether the digest is right, whether the schemas lint, or --
the part that actually reaches users -- which aliases are about to move.  Planning
fetches and checks all of that and writes nothing, so it is safe to run on a pull
request from a branch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gate, reconcile, registry
from .config import Config
from .model import ReleaseKey, ValidationError


def _projected(
    published: dict[ReleaseKey, registry.PublishedRelease], admissions: list[reconcile.Admission]
) -> dict[ReleaseKey, registry.PublishedRelease]:
    """The tree as it would be once the plan is applied."""
    projected = dict(published)
    for admission in admissions:
        projected[admission.key] = registry.PublishedRelease(
            key=admission.key,
            schemas=tuple(
                registry.PublishedSchema(doc.basename, doc.body, doc.dialect)
                for doc in sorted(admission.documents, key=lambda doc: doc.basename)
            ),
            lock=b"",
        )
    return projected


def _alias_moves(before, after) -> list[str]:
    """Which alias URLs change release, in the form a reviewer cares about."""
    groups = (
        ("line", lambda key, schema: (key.project, str(key.compat), schema.name),
         lambda bucket: f"/{bucket[0]}/v{bucket[1]}/{bucket[2]}"),
        ("latest", lambda key, schema: (key.project, schema.name),
         lambda bucket: f"/{bucket[0]}/latest/{bucket[1]}"),
    )
    moves: list[str] = []
    for _label, group, render in groups:
        old = {b: key.version for b, (key, _s) in registry.newest_by(before, group).items()}
        new = {b: key.version for b, (key, _s) in registry.newest_by(after, group).items()}
        for bucket in sorted(new, key=str):
            was, now = old.get(bucket), new[bucket]
            if was != now:
                moves.append(f"{render(bucket):<52} {was or '-'} -> {now}")
    return moves


def render_plan(proposal, admissions, published, projected) -> str:
    lines: list[str] = []
    for admission in admissions:
        label = "backfill" if admission.is_backfill else "admit"
        lines.append(f"{label:9} {admission.key}  -> line {admission.key.compat.segment}")
        lines.append(f"{'':9} artifact  {admission.tag}")
        for document in sorted(admission.documents, key=lambda doc: doc.basename):
            lines.append(f"{'':9} schema    {document.basename}  {document.dialect}")
        if admission.unlinted:
            lines.append(f"{'':9} unlinted  {', '.join(admission.unlinted)}")
    moves = _alias_moves(published, projected)
    if moves:
        lines.append("")
        lines.append("aliases")
        lines.extend(f"  {move}" for move in moves)
    if proposal.halt:
        lines.append("")
        lines.append("halt")
        lines.extend(f"  {reason}" for reason in proposal.halt)
    if not lines:
        lines.append("trees converge; nothing to do.")
    return "\n".join(lines)


def command_plan(args) -> int:
    config = Config.load(args.config)
    declarations = reconcile.declared(args.manifest_root)
    published = registry.read_published(args.site) if args.site.is_dir() else {}
    proposal = reconcile.plan(
        declarations, published, enforce_undeclared=not args.allow_undeclared
    )
    admissions: list[reconcile.Admission] = []
    if not proposal.halt:
        # Only fetch once the plan is clean: a halt should cost no network.
        admissions = reconcile.admit(
            proposal.admit, declarations, config, proposal.backfill, lint_root=args.manifest_root
        )
    print(render_plan(proposal, admissions, published, _projected(published, admissions)))
    if proposal.halt:
        return 1
    return 1 if args.strict and admissions else 0


def command_apply(args) -> int:
    config = Config.load(args.config)
    result = reconcile.apply(
        args.manifest_root,
        args.site,
        config,
        enforce_undeclared=not args.allow_undeclared,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def command_verify(args) -> int:
    found = gate.violations(sys.stdin.read(), args.tree)
    if found:
        print(f"refusing to publish: {args.tree} resources would change", file=sys.stderr)
        for violation in found:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print(json.dumps({"violations": 0}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="czschemas", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("plan", command_plan, "report what reconciling would do; writes nothing"),
        ("apply", command_apply, "reconcile the manifest tree into the publication tree"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--manifest-root", type=Path, default=Path("."))
        child.add_argument("--config", type=Path, default=Path("manifest.toml"))
        child.add_argument("--site", type=Path, default=Path("site"))
        child.add_argument(
            "--allow-undeclared",
            action="store_true",
            help="tolerate published releases with no lock in the manifest tree "
                 "(migration only; remove once every release is declared)",
        )
        child.set_defaults(handler=handler)
    sub.choices["plan"].add_argument(
        "--strict", action="store_true", help="exit non-zero when anything would change"
    )

    verify = sub.add_parser("verify", help="read a git diff --name-status and refuse non-additive changes")
    verify.add_argument("--tree", choices=sorted(gate.TREES), default="site")
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
