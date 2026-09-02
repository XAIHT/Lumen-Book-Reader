"""Headless Lumen MCP CLI. STDIO mode never writes non-protocol text to stdout."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from ..passage_builder import PassageBuilder
from ..retrieval.contracts import RetrievalError
from ..retrieval.service import RetrievalService
from ..runtime_paths import RuntimePaths
from ..version import get_version
from .config_export import ConfigError, canonical_bytes, emit, resolve_model, validate_file
from .diagnostics import doctor
from .server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumen-mcp", description="Lumen Book Reader MCP sidecar")
    parser.add_argument("--version", action="version", version=get_version())
    parser.add_argument("--data-dir")
    parser.add_argument("--index")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="Run the MCP server")
    transport = serve.add_mutually_exclusive_group()
    transport.add_argument("--stdio", action="store_true", help="Use MCP STDIO (default)")
    transport.add_argument("--http", action="store_true", help="Use loopback Streamable HTTP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    status = subcommands.add_parser("status", help="Print retrieval status")
    status.add_argument("--json", action="store_true")
    diagnostics = subcommands.add_parser("doctor", help="Run non-mutating diagnostics")
    diagnostics.add_argument("--json", action="store_true")

    index = subcommands.add_parser("index", help="Manage the complete passage index")
    index_commands = index.add_subparsers(dest="index_command", required=True)
    build = index_commands.add_parser("build", help="Build complete passage coverage")
    build.add_argument("--root", action="append", default=[])
    build.add_argument("--book-id", action="append", type=int, default=[])
    build.add_argument("--limit", type=int, default=0)
    build.add_argument("--force", action="store_true")
    build.add_argument("--json", action="store_true")

    config = subcommands.add_parser("config", help="Emit or validate portable MCP config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    export = config_commands.add_parser("emit", help="Emit LumenBookReader.json")
    export.add_argument("--target", choices=["portable-json"], default="portable-json")
    export.add_argument("--mode", choices=["installed", "development"], default="installed")
    export.add_argument("--executable")
    export.add_argument("--checkout")
    output = export.add_mutually_exclusive_group(required=True)
    output.add_argument("--output")
    output.add_argument("--stdout", action="store_true")
    export.add_argument("--force", action="store_true")
    export.add_argument("--backup", action="store_true")
    export.add_argument("--report", choices=["text", "json"], default="text")
    validate = config_commands.add_parser("validate", help="Validate LumenBookReader.json")
    validate.add_argument("--target", choices=["portable-json"], default="portable-json")
    validate.add_argument("--input", required=True)
    validate.add_argument("--mode", choices=["installed", "development"])
    validate.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    paths = RuntimePaths.discover(data_dir=arguments.data_dir, index_file=arguments.index)
    try:
        if arguments.command == "serve":
            if arguments.http and arguments.host not in {"127.0.0.1", "localhost", "::1"}:
                parser.error("unauthenticated Streamable HTTP is restricted to loopback")
            logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
            server = create_server(paths, host=arguments.host, port=arguments.port)
            try:
                if arguments.http and getattr(server, "_lumen_sdk_generation", 1) >= 2:
                    server.run(
                        transport="streamable-http",
                        host=arguments.host,
                        port=arguments.port,
                        streamable_http_path="/mcp",
                        stateless_http=True,
                        json_response=True,
                    )
                else:
                    server.run(transport="streamable-http" if arguments.http else "stdio")
            except KeyboardInterrupt:
                return 130
            return 0
        if arguments.command == "doctor":
            return _print_report(doctor(paths), arguments.json)
        if arguments.command == "status":
            return _print_report(RetrievalService(paths).status(), arguments.json)
        if arguments.command == "index":
            summary = PassageBuilder(paths.index_file).build(
                roots=arguments.root,
                book_ids=arguments.book_id,
                force=arguments.force,
                limit=arguments.limit,
                progress=None if arguments.json else lambda value: print(value, file=sys.stderr),
            )
            return _print_report({key: getattr(summary, key) for key in summary.__slots__}, arguments.json)
        if arguments.command == "config" and arguments.config_command == "emit":
            model = resolve_model(
                arguments.mode,
                executable=arguments.executable,
                checkout=arguments.checkout,
            )
            if arguments.stdout:
                sys.stdout.buffer.write(canonical_bytes(model))
                return 0
            report = emit(arguments.output, model, force=arguments.force, backup=arguments.backup)
            return _print_report(report.as_dict(), arguments.report == "json")
        if arguments.command == "config" and arguments.config_command == "validate":
            report = validate_file(arguments.input, expected_mode=arguments.mode)
            return _print_report(report.as_dict(), arguments.json)
    except ConfigError as exception:
        print(json.dumps({"error": {"code": exception.code, "message": exception.message}}, ensure_ascii=False), file=sys.stderr)
        return 3 if "JSON" in exception.code or "SCHEMA" in exception.code else 4
    except RetrievalError as exception:
        print(json.dumps(exception.as_dict("cli"), ensure_ascii=False), file=sys.stderr)
        return 4
    except (OSError, sqlite3.Error) as exception:
        print(f"{type(exception).__name__}: {exception}", file=sys.stderr)
        return 5
    return 2


def _print_report(report: dict[str, object], as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
