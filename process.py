#!/usr/bin/env python3
"""
Post-processing agent runner.
Reads a meeting transcript and an agent prompt, calls Claude CLI, saves output.
Requires the Claude CLI to be installed and authenticated (claude -p).
"""

import sys
import subprocess
from pathlib import Path


def run_agent(transcript_path: str, agent_path: str, output_path: str):
    transcript = Path(transcript_path).read_text(encoding="utf-8")
    agent_prompt = Path(agent_path).read_text(encoding="utf-8")

    full_prompt = f"{agent_prompt}\n\nHere is the meeting transcript:\n\n{transcript}"

    result = subprocess.run(
        ["claude", "-p", full_prompt],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error running Claude CLI:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    output = result.stdout
    Path(output_path).write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: process.py <transcript.md> <agent.md> <output.md>")
        sys.exit(1)
    run_agent(sys.argv[1], sys.argv[2], sys.argv[3])
