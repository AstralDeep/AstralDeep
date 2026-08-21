"""030 — knowledge index never surfaces retired/merged agents (US6 / T037)."""
import sys
from contextlib import contextmanager
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from astralplane import create_repository_catalog  # noqa: E402
from orchestrator.knowledge_synthesis import (  # noqa: E402
    RETIRED_KNOWLEDGE_STEMS,
    KnowledgeSynthesizer,
)


class _PlaneRuntime:
    def __init__(self) -> None:
        self.repositories = create_repository_catalog()

    @contextmanager
    def transaction(self):
        yield object()


def _write(p: Path, name: str):
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{name}.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")


def test_retired_stems_cover_029_agents():
    for stem in ("grants", "grant_budgets", "nefarious", "classify", "forecaster", "llm_factory"):
        assert stem in RETIRED_KNOWLEDGE_STEMS


def test_update_index_skips_retired_files(tmp_path):
    caps = tmp_path / "capabilities"
    tech = tmp_path / "techniques"
    # Retired/merged agents that must NOT be indexed even if files exist on disk.
    for name in ("grants", "classify", "forecaster", "llm_factory"):
        _write(caps, name)
        _write(tech, name)
    # A live agent that MUST still be indexed.
    _write(caps, "weather")
    _write(tech, "weather")

    runtime = _PlaneRuntime()
    synth = KnowledgeSynthesizer(
        knowledge_dir=str(tmp_path),
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        # _update_index is a pure filesystem projection; this focused test
        # does not create or claim durable maintenance units.
        maintenance_repository=object(),
    )
    synth._update_index()

    index = (tmp_path / "_index.md").read_text(encoding="utf-8")
    for retired in ("grants", "classify", "forecaster", "llm_factory"):
        assert retired not in index, f"retired agent {retired} leaked into the index"
    assert "weather" in index
