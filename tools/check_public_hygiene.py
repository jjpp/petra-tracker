# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail if tracked files look like they contain real secrets or personal data.

This repository is public. Test data must be synthetic. This is a heuristic
safety net, not a guarantee; see CLAUDE.md / README "Contributing".

Usage: python tools/check_public_hygiene.py [files...]   (default: git ls-files)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

CHECKS: list[tuple[str, re.Pattern[str]]] = [
    # A literal JWT (header.payload...). Tests build tokens at runtime instead.
    ("JWT token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}")),
    # Operator API keys look like 32 hex chars. Fetch them at runtime instead.
    ("32-hex API key", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")),
    # Authorization header with a real-looking value.
    ("Authorization header", re.compile(r"Authorization:\s*(JWT|Bearer)\s+[A-Za-z0-9_-]{20,}")),
    # Swedish personal identity number (personnummer / samordningsnummer).
    ("personal identity number", re.compile(r"\b(19|20)?\d{6}[-+]\d{4}\b")),
    # Phone numbers in international or Swedish/Norwegian mobile format.
    ("phone number", re.compile(r"(?<![\w.])(\+46|\+47|0046|0047)[\s-]?\d{2,3}[\s-]?\d{3}[\s-]?\d{2,4}\b|(?<![\w.])07\d[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b")),
    # GPS coordinates with high precision far from the synthetic Null Island area
    # (|lat| or |lon| >= 1 with 5+ decimals), e.g. "48.12345".
    ("precise real-world coordinate", re.compile(r"(?<![\d.])-?(?:[1-9]\d?|1[0-7]\d)\.\d{5,}(?!\d)")),
    # Scaled (1e7) coordinates in the real-world range, e.g. 481234567.
    ("scaled coordinate", re.compile(r"(?<!\d)[1-9]\d{8}(?!\d)")),
]

SKIP_FILES = {"LICENSE", "tools/check_public_hygiene.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".jar", ".keystore"}
# Lines explicitly marked as reviewed false positives.
ALLOW_MARK = "hygiene: allow"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f]


def main(argv: list[str]) -> int:
    files = argv or tracked_files()
    problems = 0
    for name in files:
        path = Path(name)
        if name in SKIP_FILES or path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for label, pattern in CHECKS:
                if pattern.search(line):
                    problems += 1
                    print(f"{name}:{lineno}: possible {label}: {line.strip()[:120]}")
    if problems:
        print(f"\n{problems} potential problem(s). Use synthetic data; if a hit is a false "
              f"positive, add the comment '{ALLOW_MARK}' on that line.")
        return 1
    print(f"OK: {len(files)} files checked, nothing that looks like real data or secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
