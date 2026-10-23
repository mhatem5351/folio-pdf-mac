"""Template for engine extension modules (files starting with _ are skipped).

Copy to Engine/ext/<feature>.py. The loader calls register(subparsers, api);
api offers respond(ok, **payload), ensure_parent(path), parse_page_spec(spec,
total_pages) -> 0-based indices, safe_stem(path), version.
Commands print one JSON object via api.respond and return its exit code.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def register(subparsers: Any, api: Any) -> None:
    def cmd_example(args: argparse.Namespace) -> int:
        import fitz  # lazy: keep extension loading cheap

        output = Path(args.output)
        api.ensure_parent(output)
        with fitz.open(args.input) as doc:
            doc.save(str(output), garbage=3, deflate=True)
        return api.respond(True, output=str(output))

    example = subparsers.add_parser("example-copy")
    example.add_argument("--input", required=True)
    example.add_argument("--output", required=True)
    example.set_defaults(func=cmd_example)
