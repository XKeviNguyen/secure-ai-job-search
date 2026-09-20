#!/usr/bin/env python3
"""Plan and prepare deterministic per-application document outputs.

This tool handles paths, routing, byte-for-byte source copies, and source-hash
verification. It deliberately does not tailor document content: `/apply` owns
that work and uses the appropriate document/template workflow after `prepare`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1
# Generic public default. /setup (or the caller) supplies the real slug through
# --candidate-slug or the AJS_CANDIDATE_SLUG environment variable; no candidate
# name is hard-coded in this framework.
DEFAULT_CANDIDATE_SLUG = os.environ.get("AJS_CANDIDATE_SLUG", "candidate")


@dataclass(frozen=True)
class DocumentSpec:
    kind: str
    folder: str
    suffix: str
    required_extension: str | None


@dataclass(frozen=True)
class RouteSpec:
    route: str
    aliases: tuple[str, ...]
    documents: tuple[DocumentSpec, ...]


ROUTES = (
    RouteSpec(
        route="japanese",
        aliases=("japan", "japanese", "jp"),
        documents=(
            DocumentSpec("rirekisho", "japanese", "rirekisho", ".docx"),
            DocumentSpec(
                "shokumukeirekisho",
                "japanese",
                "shokumukeirekisho",
                ".docx",
            ),
        ),
    ),
    RouteSpec(
        route="english_international",
        aliases=("english", "international", "english_international"),
        documents=(
            DocumentSpec("english_resume", "english", "resume", None),
        ),
    ),
)


class ApplicationOutputError(ValueError):
    """Raised for invalid routes, unsafe inputs, or conflicting outputs."""


def slugify(value: str) -> str:
    """Match documents/README.md's application-folder normalization rule."""
    normalized = value.strip().lower().replace(" ", "_")
    normalized = re.sub(r"[^\w]", "", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def application_id(company: str, role: str) -> str:
    result = slugify(f"{company}_{role}")
    if not result:
        raise ApplicationOutputError(
            "company and role must contain at least one letter or digit"
        )
    return result


def _is_occupied(path: Path) -> bool:
    """Return True for any lexical filesystem entry at ``path``.

    ``Path.exists()`` follows symlinks and returns False for a dangling one, so a
    broken symlink would let the allocator reuse an application ID.  The later
    write would then resolve through that symlink, placing drafts and the
    manifest outside ``documents/applications``.  ``os.path.lexists`` treats the
    symlink itself as an existing entry, which is the correct collision signal.
    """
    return os.path.lexists(path)


def allocate_application_id(*, repo_root: Path, company: str, role: str) -> tuple[str, int]:
    """Return the lowest available attempt ID without reusing an existing folder.

    The first application keeps the canonical company/role slug.  Once that path
    exists, every later attempt receives a numeric suffix.  Treat any existing
    filesystem entry (including a dangling symlink) as occupied so incomplete or
    legacy archives are preserved and no write escapes the applications folder.
    """
    base_id = application_id(company, role)
    applications_dir = repo_root / "documents" / "applications"
    if not _is_occupied(applications_dir / base_id):
        return base_id, 1

    attempt = 2
    while _is_occupied(applications_dir / f"{base_id}_{attempt}"):
        attempt += 1
    return f"{base_id}_{attempt}", attempt


def resolve_route(market: str) -> RouteSpec:
    needle = market.strip().lower().replace("-", "_").replace(" ", "_")
    for route in ROUTES:
        if needle == route.route or needle in route.aliases:
            return route
    supported = sorted({alias for route in ROUTES for alias in route.aliases})
    raise ApplicationOutputError(
        f"unknown market {market!r}; choose one of: {', '.join(supported)}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def output_plan(
    *,
    repo_root: Path,
    market: str,
    company: str,
    role: str,
    candidate_slug: str = DEFAULT_CANDIDATE_SLUG,
    source_extensions: dict[str, str] | None = None,
) -> dict:
    route = resolve_route(market)
    base_id = application_id(company, role)
    app_id, attempt = allocate_application_id(
        repo_root=repo_root, company=company, role=role
    )
    candidate = slugify(candidate_slug)
    if not candidate:
        raise ApplicationOutputError("candidate slug cannot be empty")
    application_dir = repo_root / "documents" / "applications" / app_id
    source_extensions = source_extensions or {}
    documents = []
    for spec in route.documents:
        extension = spec.required_extension or source_extensions.get(spec.kind, ".tex")
        if not extension.startswith(".") or "/" in extension or "\\" in extension:
            raise ApplicationOutputError(
                f"invalid source extension for {spec.kind}: {extension!r}"
            )
        stem = f"{candidate}_{app_id}_{spec.suffix}"
        working_path = application_dir / "drafts" / spec.folder / f"{stem}{extension}"
        pdf_path = working_path.with_suffix(".pdf")
        documents.append(
            {
                "kind": spec.kind,
                "working_path": _relative_to_repo(working_path, repo_root),
                "pdf_path": _relative_to_repo(pdf_path, repo_root),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "base_application_id": base_id,
        "application_id": app_id,
        "attempt": attempt,
        "company": company,
        "role": role,
        "route": route.route,
        "state": "drafted_for_review",
        "application_dir": _relative_to_repo(application_dir, repo_root),
        "manifest_path": _relative_to_repo(
            application_dir / "application-output.json", repo_root
        ),
        "documents": documents,
        "submitted_documents": [],
    }


def parse_source_args(values: Iterable[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ApplicationOutputError(
                f"source must use KIND=PATH syntax, got {value!r}"
            )
        kind, raw_path = value.split("=", 1)
        kind = kind.strip()
        if not kind or not raw_path.strip():
            raise ApplicationOutputError(
                f"source must use non-empty KIND=PATH syntax, got {value!r}"
            )
        if kind in sources:
            raise ApplicationOutputError(f"duplicate source kind: {kind}")
        sources[kind] = Path(raw_path).expanduser().resolve()
    return sources


def prepare_outputs(
    *,
    repo_root: Path,
    market: str,
    company: str,
    role: str,
    sources: dict[str, Path],
    candidate_slug: str = DEFAULT_CANDIDATE_SLUG,
) -> dict:
    repo_root = repo_root.resolve()
    route = resolve_route(market)
    expected = {spec.kind for spec in route.documents}
    supplied = set(sources)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        raise ApplicationOutputError("invalid source set (" + "; ".join(details) + ")")

    extensions: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    for spec in route.documents:
        source = sources[spec.kind]
        if not source.is_file():
            raise ApplicationOutputError(f"source does not exist: {source}")
        if spec.required_extension and source.suffix.lower() != spec.required_extension:
            raise ApplicationOutputError(
                f"{spec.kind} source must be {spec.required_extension}, got {source.suffix}"
            )
        extensions[spec.kind] = source.suffix
        source_hashes[spec.kind] = sha256_file(source)

    manifest = output_plan(
        repo_root=repo_root,
        market=route.route,
        company=company,
        role=role,
        candidate_slug=candidate_slug,
        source_extensions=extensions,
    )
    manifest_path = repo_root / manifest["manifest_path"]

    destinations = {
        item["kind"]: repo_root / item["working_path"] for item in manifest["documents"]
    }
    for destination in destinations.values():
        if _is_occupied(destination):
            raise ApplicationOutputError(f"refusing to overwrite: {destination}")
    for spec in route.documents:
        source = sources[spec.kind]
        destination = destinations[spec.kind]
        if source == destination.resolve():
            raise ApplicationOutputError("source and destination must be different")

    applications_dir = repo_root / "documents" / "applications"
    applications_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest_path.parent.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise ApplicationOutputError(
            "application output collision while reserving "
            f"{manifest_path.parent}; run prepare again to allocate the next attempt"
        ) from error
    for destination in destinations.values():
        destination.parent.mkdir(parents=True, exist_ok=True)

    for spec in route.documents:
        source = sources[spec.kind]
        destination = destinations[spec.kind]
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)

    for item in manifest["documents"]:
        kind = item["kind"]
        item["source_path"] = _relative_to_repo(sources[kind], repo_root)
        item["source_sha256"] = source_hashes[kind]
        item["prepared_sha256"] = sha256_file(destinations[kind])
        if item["prepared_sha256"] != item["source_sha256"]:
            raise ApplicationOutputError(f"copy hash mismatch for {kind}")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".application-output-", suffix=".json", dir=manifest_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, manifest_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return manifest


def verify_sources(*, repo_root: Path, manifest_path: Path) -> dict:
    """Fail closed: a missing or incomplete record is never a success.

    The Japanese route protects two private masters and the English route one.
    Validation therefore checks the manifest route, requires the complete
    expected document set for that route, and requires each expected record to
    carry ``source_path`` and ``source_sha256``.  An empty or truncated manifest
    reports ``all_sources_unchanged: false`` rather than a vacuous success.
    """
    repo_root = repo_root.resolve()
    errors: list[str] = []
    results: list[dict] = []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "all_sources_unchanged": False,
            "route": None,
            "expected_kinds": [],
            "sources": [],
            "errors": [f"manifest could not be read as JSON: {error}"],
        }
    if not isinstance(manifest, dict):
        return {
            "all_sources_unchanged": False,
            "route": None,
            "expected_kinds": [],
            "sources": [],
            "errors": ["manifest must be a JSON object"],
        }

    route_value = manifest.get("route")
    expected_kinds: list[str] = []
    if not isinstance(route_value, str) or not route_value.strip():
        errors.append("manifest is missing the 'route' field")
    else:
        try:
            route = resolve_route(route_value)
            expected_kinds = [spec.kind for spec in route.documents]
        except ApplicationOutputError as error:
            errors.append(f"invalid manifest route: {error}")

    documents = manifest.get("documents")
    if not isinstance(documents, list):
        errors.append("manifest 'documents' must be a list")
        documents = []

    by_kind: dict[str, dict] = {}
    for entry in documents:
        if not isinstance(entry, dict):
            errors.append("manifest contains a non-object document entry")
            continue
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            errors.append("manifest contains a document entry with no 'kind'")
            continue
        if kind in by_kind:
            errors.append(f"duplicate document record for kind {kind!r}")
            continue
        by_kind[kind] = entry

    if expected_kinds:
        missing = [kind for kind in expected_kinds if kind not in by_kind]
        unexpected = [kind for kind in by_kind if kind not in expected_kinds]
        if missing:
            errors.append(f"missing routed document record(s): {', '.join(missing)}")
        if unexpected:
            errors.append(f"unexpected routed document record(s): {', '.join(unexpected)}")

    for kind in expected_kinds:
        entry = by_kind.get(kind)
        if entry is None:
            results.append(
                {
                    "kind": kind,
                    "source_path": None,
                    "expected_sha256": None,
                    "actual_sha256": None,
                    "unchanged": False,
                    "error": "no document record in manifest",
                }
            )
            continue

        source_value = entry.get("source_path")
        expected_hash = entry.get("source_sha256")
        if not source_value:
            results.append(
                {
                    "kind": kind,
                    "source_path": None,
                    "expected_sha256": expected_hash,
                    "actual_sha256": None,
                    "unchanged": False,
                    "error": "missing source_path",
                }
            )
            continue
        if not expected_hash:
            results.append(
                {
                    "kind": kind,
                    "source_path": source_value,
                    "expected_sha256": None,
                    "actual_sha256": None,
                    "unchanged": False,
                    "error": "missing source_sha256",
                }
            )
            continue

        source_path = Path(source_value)
        if not source_path.is_absolute():
            source_path = repo_root / source_path
        exists = source_path.is_file()
        actual_hash = sha256_file(source_path) if exists else None
        unchanged = exists and actual_hash == expected_hash
        results.append(
            {
                "kind": kind,
                "source_path": source_value,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "unchanged": unchanged,
                "error": None if unchanged else "source missing or hash mismatch",
            }
        )

    all_unchanged = (
        not errors
        and bool(expected_kinds)
        and all(result["unchanged"] for result in results)
        and len(results) == len(expected_kinds)
    )
    return {
        "all_sources_unchanged": all_unchanged,
        "route": route_value if isinstance(route_value, str) else None,
        "expected_kinds": expected_kinds,
        "sources": results,
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "prepare"):
        command = subparsers.add_parser(name)
        command.add_argument("--market", required=True)
        command.add_argument("--company", required=True)
        command.add_argument("--role", required=True)
        command.add_argument("--candidate-slug", default=DEFAULT_CANDIDATE_SLUG)
        if name == "prepare":
            command.add_argument(
                "--source",
                action="append",
                default=[],
                metavar="KIND=PATH",
                help="repeat once for each routed document",
            )

    verify = subparsers.add_parser("verify-sources")
    verify.add_argument("manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = output_plan(
                repo_root=args.repo_root,
                market=args.market,
                company=args.company,
                role=args.role,
                candidate_slug=args.candidate_slug,
            )
        elif args.command == "prepare":
            result = prepare_outputs(
                repo_root=args.repo_root,
                market=args.market,
                company=args.company,
                role=args.role,
                candidate_slug=args.candidate_slug,
                sources=parse_source_args(args.source),
            )
        else:
            manifest_path = args.manifest
            if not manifest_path.is_absolute():
                manifest_path = args.repo_root / manifest_path
            result = verify_sources(
                repo_root=args.repo_root, manifest_path=manifest_path.resolve()
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ApplicationOutputError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
