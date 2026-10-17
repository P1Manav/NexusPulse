"""
run.py — Automated runner that parses and executes run.yaml directly.

Emulates the exact behavior of the AIntropy Grader Bot:
  1. Reads run.yaml from repo root
  2. Runs setup_commands (pip install)
  3. Runs run_commands (python src/main.py demo)
  4. Runs test_commands (python -m pytest src/tests -q)
  5. Stops on any failure and exits with appropriate exit code

Usage:
  python run.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
RUN_YAML = ROOT_DIR / "run.yaml"


def parse_run_yaml(file_path: Path) -> dict[str, list[str]]:
    """Parse commands from run.yaml using standard library (works before pyyaml is installed)."""
    text = file_path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {
        "setup_commands": [],
        "run_commands": [],
        "test_commands": [],
    }

    current_section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Detect section header
        if line.startswith("setup_commands:"):
            current_section = "setup_commands"
            continue
        elif line.startswith("run_commands:"):
            current_section = "run_commands"
            continue
        elif line.startswith("test_commands:"):
            current_section = "test_commands"
            continue
        elif line.startswith("notes:"):
            current_section = None
            continue

        # Extract list item: - "command"
        if current_section and line.startswith("-"):
            cmd = line[1:].strip()
            # Strip quotes if present
            if (cmd.startswith('"') and cmd.endswith('"')) or (cmd.startswith("'") and cmd.endswith("'")):
                cmd = cmd[1:-1]
            if cmd:
                sections[current_section].append(cmd)

    return sections


def run_command_section(section_name: str, commands: list[str]) -> bool:
    print(f"\n{'=' * 72}")
    print(f"  EXECUTING: {section_name.upper()} ({len(commands)} command(s))")
    print(f"{'=' * 72}")

    for cmd in commands:
        print(f"\n>> {cmd}\n")
        # Ensure python commands in child processes use the same python interpreter if inside a venv
        adjusted_cmd = cmd
        if cmd.startswith("python "):
            adjusted_cmd = f'"{sys.executable}" ' + cmd[7:]
        elif cmd.startswith("pip "):
            # Use python -m pip to ensure it installs into the current environment
            adjusted_cmd = f'"{sys.executable}" -m pip ' + cmd[4:]

        res = subprocess.run(adjusted_cmd, shell=True, cwd=ROOT_DIR)
        if res.returncode != 0:
            print(f"\n[FAIL] Command failed with exit code {res.returncode}: {cmd}")
            return False

    return True


def main() -> int:
    if not RUN_YAML.exists():
        print(f"Error: {RUN_YAML} not found")
        return 1

    print("=" * 72)
    print("  AIntropy Grader Bot Simulator -- Running run.yaml")
    print("=" * 72)

    config = parse_run_yaml(RUN_YAML)

    for section in ["setup_commands", "run_commands", "test_commands"]:
        cmds = config.get(section, [])
        if cmds:
            success = run_command_section(section, cmds)
            if not success:
                print(f"\n[ABORTED] Evaluation failed at section: {section}")
                return 1

    print("\n" + "=" * 72)
    print("  ALL run.yaml COMMANDS COMPLETED SUCCESSFULLY (EXIT CODE 0)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
