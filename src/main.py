"""
Agentic Message Board — CLI entry point.

Subcommands:
  serve   Start the FastAPI server (default port from PORT env, default 8000).
  demo    Boot API in-process, run the swarm, print sample results, then exit.
  swarm   Run only the swarm runner against a running API.
  bench   Run the load benchmark and report results.
"""
from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Super-Lightweight Agentic Message Board",
    )
    sub = parser.add_subparsers(dest="command")

    # -- serve --
    sub.add_parser("serve", help="Start the FastAPI server")

    # -- demo --
    sub.add_parser("demo", help="Run full demo (API + swarm + sample queries), then exit")

    # -- swarm --
    sub.add_parser("swarm", help="Run the swarm runner against a running API")

    # -- bench --
    sub.add_parser("bench", help="Run load benchmark and report results")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "serve":
        from board.api import run_server
        run_server()
        return 0

    if args.command == "demo":
        from board.demo import run_demo
        return run_demo()

    if args.command == "swarm":
        from swarm.runner import run_swarm_cli
        run_swarm_cli()
        return 0

    if args.command == "bench":
        from board.bench import run_bench
        return run_bench()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
