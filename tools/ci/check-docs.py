############################################################################
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
############################################################################

"""Check that Documentation/test-cases.rst matches the real test suite.

Collects every ``test_*`` function/method defined under
``ntfc/tests/**/test_*.py`` and every manifest session name defined in
``ntfc/manifest-*.yaml``, and compares them against the double-backtick
literals in ``Documentation/test-cases.rst``. Exits non-zero (printing
every mismatch) if a test or session is undocumented, or if the docs
name a test or session that does not exist.
"""

import ast
import sys
from pathlib import Path
from typing import Set

import yaml

#: Repository root (two levels up from this file, tools/ci/check-docs.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Glob (relative to REPO_ROOT) matching every NTFC test module.
TEST_GLOB = "ntfc/tests/**/test_*.py"

#: Glob (relative to REPO_ROOT) matching every NTFC manifest.
MANIFEST_GLOB = "ntfc/manifest-*.yaml"

#: Glob (relative to REPO_ROOT) matching each target's NTFC config
#: directory (``ntfc/configs/<target>``).
TARGET_GLOB = "ntfc/configs/*"

#: Path (relative to REPO_ROOT) of the test-cases documentation page.
DOC_PATH = "Documentation/test-cases.rst"

#: A literal that names a test function, e.g. ``test_uname``.
TEST_NAME_PREFIX = "test_"

#: Characters a manifest session name is made of (``<target>-<scenario>``).
SESSION_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _is_session_like(literal: str, targets: Set[str]) -> bool:
    """Decide whether a literal could be a manifest session name.

    A session name is made only of lowercase letters, digits and
    hyphens, and contains at least one hyphen (every real session name
    is ``<target>-<scenario>``). Target names themselves (e.g.
    ``qemu-armv8a``, ``rv-virt``) have the same shape but are not
    session names, so they are excluded explicitly. This keeps
    unrelated literals (file paths, commands, verdict lines, bare
    target names) out of the comparison.

    :param literal: the literal's text, without the surrounding
        double backticks
    :param targets: known target directory names to exclude
    :return: ``True`` if the literal is shaped like a session name
    """
    return (
        "-" in literal
        and literal[0] != "-"
        and literal[-1] != "-"
        and set(literal) <= SESSION_CHARS
        and literal not in targets
    )


def collect_test_names(root: Path) -> Set[str]:
    """Collect every ``test_*`` function/method name under ntfc/tests.

    :param root: repository root
    :return: set of test function/method names found by AST parsing
    """
    names: Set[str] = set()
    for path in sorted(root.glob(TEST_GLOB)):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_def and node.name.startswith(TEST_NAME_PREFIX):
                names.add(node.name)

    return names


def collect_session_names(root: Path) -> Set[str]:
    """Collect every manifest session name under ntfc/manifest-*.yaml.

    :param root: repository root
    :return: set of session names declared across every manifest
    """
    names: Set[str] = set()
    for path in sorted(root.glob(MANIFEST_GLOB)):
        data = yaml.safe_load(path.read_text()) or {}
        for session in data.get("sessions", []):
            names.add(session["name"])

    return names


def collect_target_names(root: Path) -> Set[str]:
    """Collect every target name from its ntfc/configs/<target> directory.

    :param root: repository root
    :return: set of target directory names (e.g. ``sim``,
        ``qemu-armv8a``)
    """
    return {path.name for path in root.glob(TARGET_GLOB) if path.is_dir()}


def collect_doc_literals(doc: Path) -> Set[str]:
    """Collect every double-backtick ``literal`` in a RST file.

    :param doc: path to the RST file
    :return: set of literal contents, without the double backticks
    """
    text = doc.read_text()
    literals: Set[str] = set()
    start = 0
    while True:
        begin = text.find("``", start)
        if begin < 0:
            break

        end = text.find("``", begin + 2)
        if end < 0:
            break

        literals.add(text[begin + 2 : end])
        start = end + 2

    return literals


def main() -> int:
    """Check Documentation/test-cases.rst against the real test suite.

    :return: ``0`` if the docs match the test suite, ``1`` otherwise
    """
    doc = REPO_ROOT / DOC_PATH

    tests = collect_test_names(REPO_ROOT)
    sessions = collect_session_names(REPO_ROOT)
    targets = collect_target_names(REPO_ROOT)
    literals = collect_doc_literals(doc)

    documented_tests = {
        lit for lit in literals if lit.startswith(TEST_NAME_PREFIX)
    }
    documented_sessions = {
        lit for lit in literals if _is_session_like(lit, targets)
    }

    problems = []

    for name in sorted(tests - documented_tests):
        problems.append(f"undocumented test function: {name}")

    for name in sorted(sessions - documented_sessions):
        problems.append(f"undocumented manifest session: {name}")

    for name in sorted(documented_tests - tests):
        problems.append(f"documented test function does not exist: {name}")

    for name in sorted(documented_sessions - sessions):
        problems.append(f"documented manifest session does not exist: {name}")

    if problems:
        print(f"{DOC_PATH}: out of date with the test suite:")
        for problem in problems:
            print(f"  {problem}")

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
