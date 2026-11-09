#!/usr/bin/env python3
"""Check public source layouts and package contracts without executing plugins."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
import yaml

ROOT = Path(__file__).resolve().parents[1]
DRAFT = "https://json-schema.org/draft/2020-12/schema"
RELEASE_VERSION = "0.1.0"
# This source repository deliberately does not commit compiled install payloads.
GENERATED_PAYLOADS = {
    "ollama.cli": {"bin/ollama.cli": ("go.mod", "cmd/plugin/main.go")},
}


class StructureError(ValueError):
    """A static contract violation reported using paths rather than file contents."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently accepting the last value."""


def unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    """Construct one YAML mapping, rejecting duplicate keys without echoing values."""
    mapping = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node)
        if key in mapping:
            raise StructureError("duplicate YAML mapping key")
        mapping[key] = loader.construct_object(value_node)
    return yaml.SafeLoader.construct_mapping(loader, node)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def unique_json(pairs: list[tuple[str, object]]) -> dict:
    """Construct one JSON object, rejecting duplicate keys without echoing values."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise StructureError("duplicate JSON object key")
        result[key] = value
    return result


def reject_json_constant(value: str) -> None:
    """Reject nonstandard JSON constants such as NaN and Infinity."""
    raise StructureError("nonstandard JSON constant")


def source_files(root: Path) -> set[str]:
    """Enumerate tracked and nonignored new source files using read-only Git."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {path.decode("utf-8") for path in result.stdout.split(b"\0") if path}


def regular_file(base: Path, relative: str) -> Path:
    """Require a contained regular file without unsafe paths or symlink ancestors."""
    if not isinstance(relative, str) or not relative:
        raise StructureError("file path must be a nonempty string")
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
        or any(character in relative for character in "\\*?[]!")
    ):
        raise StructureError(
            "file path must be a literal canonical package-relative path"
        )
    target = base / relative
    if any(
        (base / Path(*path.parts[:index])).is_symlink()
        for index in range(1, len(path.parts) + 1)
    ):
        raise StructureError(f"{relative}: symlinks are not allowed")
    if not target.is_file() or not target.resolve().is_relative_to(base.resolve()):
        raise StructureError(f"{relative}: missing or non-regular source file")
    return target


def read_document(filename: Path) -> object:
    """Parse YAML or JSON with duplicate-key checks and content-free diagnostics."""
    try:
        text = filename.read_text(encoding="utf-8")
        if filename.suffix == ".json":
            return json.loads(
                text, object_pairs_hook=unique_json, parse_constant=reject_json_constant
            )
        return yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, json.JSONDecodeError, UnicodeError, TypeError) as error:
        raise StructureError("malformed YAML/JSON document") from error


def require(condition: bool, message: str) -> None:
    """Raise a contract diagnostic when a static assertion is not satisfied."""
    if not condition:
        raise StructureError(message)


def check_schema(
    base: Path,
    relative: str,
    payload: set[str],
    visited: set[str],
    action_schema: bool = False,
) -> None:
    """Validate schemas and contained references offline with JSON Schema resources."""
    require(
        relative in payload,
        f"{relative}: referenced schema must be selected in package.files",
    )
    schema = read_document(regular_file(base, relative))
    if action_schema:
        require(
            isinstance(schema, dict) and schema.get("$schema") == DRAFT,
            "action schemas must declare JSON Schema Draft 2020-12",
        )
    if relative in visited:
        return
    visited.add(relative)
    resources = {}

    def load(uri: str) -> Resource:
        """Load selected regular schema files without retrieving network resources."""
        if uri not in resources:
            parsed = urlsplit(uri)
            require(
                parsed.scheme == "file" and not parsed.netloc and not parsed.query,
                "remote schema retrieval is not allowed",
            )
            target = Path(unquote(parsed.path)).relative_to(base.resolve()).as_posix()
            require(
                target in payload,
                f"{target}: referenced schema must be selected in package.files",
            )
            contents = read_document(regular_file(base, target))
            try:
                Draft202012Validator.check_schema(contents)
            except SchemaError as error:
                raise StructureError(
                    f"{target}: invalid Draft 2020-12 schema"
                ) from error
            resources[uri] = Resource(contents=contents, specification=DRAFT202012)
        return resources[uri]

    # Pre-register selected JSON resources so embedded $id/$anchor references work.
    registry = (
        Registry(retrieve=load)
        .with_resources(
            ((base.resolve() / path).as_uri(), load((base.resolve() / path).as_uri()))
            for path in sorted(payload)
            if path.endswith(".schema.json")
        )
        .crawl()
    )
    uri = (base.resolve() / relative).as_uri()
    resource = load(uri)
    seen = set()

    def visit(current: Resource, resolver: Any) -> None:
        """Validate schema-bearing positions and references, excluding instance data."""
        if id(current.contents) in seen:
            return
        seen.add(id(current.contents))
        try:
            Draft202012Validator.check_schema(current.contents)
        except SchemaError as error:
            raise StructureError(f"{relative}: invalid Draft 2020-12 schema") from error
        if isinstance(current.contents, dict):
            for keyword in ("$ref", "$dynamicRef"):
                if keyword not in current.contents:
                    continue
                reference = current.contents[keyword]
                parsed = urlsplit(reference)
                path = unquote(parsed.path)
                require(
                    not parsed.scheme and not parsed.netloc and not parsed.query,
                    f"{relative}: remote or qualified schema references "
                    "are not allowed",
                )
                require(
                    not PurePosixPath(path).is_absolute()
                    and ".." not in PurePosixPath(path).parts,
                    f"{relative}: schema references must stay inside their package",
                )
                try:
                    resolved = resolver.lookup(reference)
                except Unresolvable as error:
                    raise StructureError(
                        f"{relative}: unresolved {keyword} target "
                        "(missing JSON pointer, anchor or selected file)"
                    ) from error
                visit(
                    Resource(contents=resolved.contents, specification=DRAFT202012),
                    resolved.resolver,
                )
        for child in current.subresources():
            visit(child, resolver.in_subresource(child))

    visit(resource, registry.resolver(uri).in_subresource(resource))


def check_package(root: Path, package: str, files: set[str]) -> None:
    """Check source layout, finite payload, actions and adapter-specific bindings."""
    base = root / "plugins" / package
    require(not (root / "plugins").is_symlink(), "plugins/ must not be a symlink")
    require(not base.is_symlink(), "package directory must not be a symlink")
    prefix = f"plugins/{package}/"
    local = {path.removeprefix(prefix) for path in files if path.startswith(prefix)}
    for relative in ("plugin.yaml", "README.md", "workflow.yaml"):
        require(relative in local, f"{relative}: required source file is missing")
        regular_file(base, relative)
    tests = [
        path
        for path in sorted(local)
        if path.startswith("tests/")
        and (
            Path(path).name.startswith("test_")
            or Path(path).name in {"smoke.py", "e2e.py"}
        )
        and path.endswith(".py")
    ]
    # Presence must refer to actual contained files, not stale Git paths or links.
    require(
        bool(tests),
        "tests/: require at least one package-local test or smoke runner",
    )
    for path in tests:
        regular_file(base, path)
    manifest = read_document(base / "plugin.yaml")
    require(isinstance(manifest, dict), "manifest must be a YAML mapping")
    require(
        manifest.get("schema") == "transist.plugin.v1",
        "manifest schema must be transist.plugin.v1",
    )
    require(
        manifest.get("id") == package, "manifest id must match its canonical directory"
    )
    require(
        manifest.get("version") == RELEASE_VERSION,
        f"manifest version must be exactly {RELEASE_VERSION}",
    )
    workflow = read_document(base / "workflow.yaml")
    require(isinstance(workflow, dict), "workflow must be a YAML mapping")
    require(
        workflow.get("schema") == "transist.flow.v1",
        "workflow schema must be transist.flow.v1",
    )
    require(
        workflow.get("version") == RELEASE_VERSION,
        f"workflow version must be exactly {RELEASE_VERSION}",
    )
    pins = workflow.get("plugins")
    require(
        isinstance(pins, dict) and bool(pins),
        "workflow plugins must be a nonempty mapping",
    )
    require(
        all(pin == RELEASE_VERSION for pin in pins.values()),
        f"workflow plugin pins must be exactly {RELEASE_VERSION}",
    )
    adapter = manifest.get("adapter")
    require(adapter in ("process", "http", "mcp"), "unsupported adapter")
    packaging = manifest.get("package")
    require(isinstance(packaging, dict), "package must be a mapping")
    selected = packaging.get("files")
    require(
        isinstance(selected, list)
        and bool(selected)
        and all(isinstance(path, str) for path in selected),
        "package.files must be a nonempty list of file paths",
    )
    payload = set(selected)
    require(len(payload) == len(selected), "package.files contains duplicate entries")
    require(
        "plugin.yaml" not in payload, "plugin.yaml is implicit and must not be selected"
    )
    generated = GENERATED_PAYLOADS.get(package, {})
    for relative in selected:
        if relative in generated:
            require(relative not in local, "generated binary must not be committed")
            for source in generated[relative]:
                require(
                    source in local, f"{source}: generated payload source is missing"
                )
                regular_file(base, source)
        else:
            require(
                relative in local, "selected payload file is absent from public sources"
            )
            regular_file(base, relative)
    entrypoint = manifest.get("entrypoint")
    require(
        isinstance(entrypoint, dict) and entrypoint.get("type") == adapter,
        "entrypoint.type must match adapter",
    )
    if adapter in ("process", "mcp"):
        command = entrypoint.get("command")
        require(
            isinstance(command, str) and command in payload,
            "entrypoint.command must be a selected local file",
        )
        if command not in generated:
            require(
                bool(regular_file(base, command).stat().st_mode & 0o111),
                "entrypoint.command must be executable",
            )
    else:
        url = entrypoint.get("base_url")
        require(
            isinstance(url, str)
            and urlsplit(url).scheme in {"http", "https"}
            and bool(urlsplit(url).netloc),
            "HTTP entrypoint requires an HTTP(S) base_url",
        )
    actions = manifest.get("actions")
    require(
        isinstance(actions, list) and bool(actions), "actions must be a nonempty list"
    )
    names = set()
    visited = set()
    for action in actions:
        require(isinstance(action, dict), "action must be a mapping")
        name = action.get("name")
        require(
            isinstance(name, str) and bool(name) and name not in names,
            "action names must be nonempty and unique",
        )
        names.add(name)
        provider = action.get(
            "provider_kind", manifest.get("default_provider_kind", "external")
        )
        require(provider in ("local", "external"), "unsupported provider_kind")
        if adapter == "mcp":
            bindings = action.get("bindings")
            require(
                isinstance(bindings, dict)
                and isinstance(bindings.get("tool_name"), str)
                and bool(bindings["tool_name"]),
                "MCP action requires bindings.tool_name",
            )
        for field in ("input_schema", "output_schema"):
            relative = action.get(field)
            require(
                isinstance(relative, str)
                and relative.startswith("schemas/")
                and relative.endswith(".schema.json"),
                f"{field} must reference a JSON schema under schemas/",
            )
            check_schema(base, relative, payload, visited, action_schema=True)


def check_repository(root: Path) -> list[str]:
    """Return all package and YAML/JSON diagnostics without modifying source files."""
    files = source_files(root)
    errors = []
    packages = set()
    for relative in sorted(files):
        path = PurePosixPath(relative)
        if path.name == "plugin.yaml" and (
            len(path.parts) != 3 or path.parts[0] != "plugins"
        ):
            errors.append(
                f"{relative}: plugin manifests belong at plugins/<id>/plugin.yaml"
            )
        if path.parts[0] == "plugins":
            if len(path.parts) < 3:
                errors.append(f"{relative}: plugin source requires a package directory")
            else:
                packages.add(path.parts[1])
        if path.suffix in {".yaml", ".yml", ".json"}:
            try:
                read_document(regular_file(root, relative))
            except (ValueError, OSError) as error:
                errors.append(f"{relative}: {error}")
    if not packages:
        errors.append("plugins/: no public source packages found")
    for package in sorted(packages):
        try:
            check_package(root, package, files)
        except (ValueError, OSError) as error:
            errors.append(f"plugins/{package}: {error}")
    return errors


def main() -> int:
    """Run the read-only static gate and return nonzero for invalid source contracts."""
    try:
        errors = check_repository(ROOT)
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        print("Cannot enumerate public source files", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Plugin source structure and YAML/JSON contracts passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
