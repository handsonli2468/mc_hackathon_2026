"""Pytest isolation for the robot-brain project.

The production defaults intentionally point at /mlsteam/workspace.  Unit tests may be
run from an extracted release anywhere, so establish a repository-local PROJECT_ROOT
and an isolated temporary RUNTIME_ROOT *before* application modules are imported.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="robot-bt-agent-tests-"))

os.environ["PROJECT_ROOT"] = str(_REPO_ROOT)
os.environ["RUNTIME_ROOT"] = str(_TEST_RUNTIME)
os.environ["RAG_KNOWLEDGE_ROOT"] = str(_TEST_RUNTIME / "rag-knowledge")


def pytest_sessionfinish(session, exitstatus):  # type: ignore[no-untyped-def]
    # Best-effort cleanup; test failures should never be masked by cleanup errors.
    try:
        shutil.rmtree(_TEST_RUNTIME, ignore_errors=True)
    except Exception:
        pass
