"""Build the exact test-only OpenCLI artifact closure."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

_OPENCLI_ENTRYPOINT_SUFFIX = "/node_modules/@jackwener/opencli/dist/src/main.js"


@dataclass(frozen=True, slots=True)
class OpenCliArtifactClosure:
    node: Path
    root: Path
    cli: Path
    session_home: Path

    def paths(self) -> tuple[Path, Path, Path, Path]:
        return self.node, self.root, self.cli, self.session_home


def python_node_stub(expected_argv: tuple[str, ...], body: str) -> bytes:
    """Return a Python-backed Node stub with the shared entrypoint/argv checks."""

    source = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"if not sys.argv[1].endswith({_OPENCLI_ENTRYPOINT_SUFFIX!r}):\n"
        "    raise SystemExit(6)\n"
        f"if tuple(sys.argv[2:]) != {expected_argv!r}:\n"
        "    raise SystemExit(7)\n"
        f"{body}"
    )
    return source.encode("utf-8")


def build_opencli_artifact_closure(
    base: Path,
    *,
    node_name: str = "node",
    root_name: str = "opencli",
    session_name: str = "session",
) -> OpenCliArtifactClosure:
    """Create the package tree, permissions, and session home used by tests."""

    node = base / node_name
    node.write_bytes(b"#!/bin/sh\nexit 0\n")
    node.chmod(0o700)
    root = base / root_name
    package_root = root / "node_modules" / "@jackwener" / "opencli"
    cli = package_root / "dist" / "src" / "main.js"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"export {};\n")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@jackwener/opencli",
                "version": "1.8.6-hermes.1",
                "bin": {"opencli": "dist/src/main.js"},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    session_home = base / session_name
    session_home.mkdir(mode=0o700)
    return OpenCliArtifactClosure(node, root, cli, session_home)
