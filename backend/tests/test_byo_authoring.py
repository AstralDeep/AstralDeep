"""Feature 058 — BYO authoring orchestration: the Analyze gate is structurally
pre-generation (a violating draft produces NO code) and a passing draft generates
+ delivers a REAL self-contained bundle to the host (never Popen'd).

The LLM call is stubbed (codegen needs a configured system LLM) but everything
downstream of it is the real generator + lifecycle: the earlier all-mocked draft
hid a defect where the delivered bundle was always ``{}``."""
from __future__ import annotations

import asyncio
import ast
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from astralplane import (
    BundlePublicationKey,
    GENERATED_AGENT_BUNDLE_CONTRACT,
    ImmutableBundleStore,
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import agent_lifecycle as lifecycle  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.agent_constitution import (  # noqa: E402
    AGENT_CONSTITUTION_VERSION,
    USER_AGENT_POLICY_REVISION,
)
from orchestrator.agent_generator import (  # noqa: E402
    BYO_BUNDLE_FILENAMES,
    BYO_RUNTIME_CONTRACT_VERSION,
)
from orchestrator.agent_lifecycle import AgentLifecycleManager, BYO_ORIGIN  # noqa: E402
from orchestrator.generated_agent_publication import (  # noqa: E402
    GeneratedAgentPublicationRecoveryPendingError,
    GeneratedAgentPublicationResult,
    generated_agent_publication_identity,
)
from tests.helpers.draft_store_double import InMemoryDraftStore  # noqa: E402
from tests.helpers.user_agent_registry import make_user_agent_registry  # noqa: E402

# A plausible LLM output: self-contained, astralprims-only, correct return shape.
CANNED_TOOLS = '''"""Greeter tools."""
from astralprims import Card, Text

REQUIRED_CREDENTIALS = []


def greet(name="world", **kwargs):
    card = Card(title="Greeting", content=[Text(content=f"Hello, {name}!")])
    return {"_ui_components": [card.to_dict()], "_data": {"greeted": name}}


TOOL_REGISTRY = {
    "greet": {
        "function": greet,
        "description": "Greet someone by name",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"}}},
        "scope": "tools:read",
    },
}
'''


class _LifecyclePublicationService:
    """Narrow typed publisher double; Plane owns its real state-machine tests."""

    def __init__(self, draft_store, bundle_store):
        self.draft_store = draft_store
        self.bundle_store = bundle_store
        self.results = {}

    async def load_published(
        self,
        *,
        owner_id,
        draft_uuid,
        source_state_revision,
    ):
        return self.results.get((owner_id, draft_uuid, source_state_revision))

    async def publish(self, request):
        identity = generated_agent_publication_identity(
            owner_id=request.owner_id,
            draft_uuid=request.draft_uuid,
            source_state_revision=request.source_state_revision,
            generation_claim_id=request.generation_claim_id,
            target_agent_id=request.target_agent_id,
        )
        key = BundlePublicationKey(
            scope_id=request.target_agent_id,
            staging_id=request.draft_uuid,
            source_revision=request.source_state_revision,
            publication_id=str(identity.publication_id),
            revision_id=str(identity.target_revision_id),
        )
        published = await asyncio.to_thread(
            self.bundle_store.publish,
            request.bundle,
            key=key,
        )
        finished = await asyncio.to_thread(
            self.draft_store.finish_draft_generation,
            draft_id=request.draft_uuid,
            owner_user_id=request.owner_id,
            expected_revision=request.source_state_revision,
            claim_id=request.generation_claim_id,
            status="generated",
            error_message=request.generation_result.error_message,
            security_report=request.generation_result.security_report,
            validation_report=request.generation_result.validation_report,
            required_credentials=request.generation_result.required_credentials,
        )
        if finished is None:
            raise RuntimeError("test publication claim fence is stale")
        self.draft_store.update_draft_agent(
            request.draft_uuid,
            published_revision_id=str(identity.target_revision_id),
        )
        result = GeneratedAgentPublicationResult(
            publication=MagicMock(
                publication_id=str(identity.publication_id),
                generation_claim_id=request.generation_claim_id,
            ),
            revision=MagicMock(
                revision_id=str(identity.target_revision_id),
                agent_id=request.target_agent_id,
                state="prepared",
                failure_code=None,
                runtime_contract_version=request.runtime_contract_version,
                release_lock_digest=request.release_lock_digest,
            ),
            published=published,
            generation_result=request.generation_result,
        )
        self.results[
            (request.owner_id, request.draft_uuid, request.source_state_revision)
        ] = result
        return result


def _fake_orch():
    o = MagicMock()
    o.user_agent_registry = make_user_agent_registry()
    o.lifecycle_manager = MagicMock()
    draft_store = InMemoryDraftStore()
    o.lifecycle_manager.draft_store = draft_store

    async def _create_draft(**kwargs):
        draft_id = str(uuid.uuid4())
        revises_agent_id = kwargs.get("revises_agent_id") or None
        agent_name = str(kwargs["agent_name"])
        draft_store.create_draft_agent(
            draft_id=draft_id,
            user_id=str(kwargs["user_id"]),
            agent_name=agent_name,
            agent_slug=(
                f"{agent_name.lower().replace(' ', '_')}_"
                f"{draft_id.replace('-', '')[:12]}"
            ),
            description=str(kwargs["description"]),
            tools_spec=(
                json.dumps(kwargs["tools_spec"])
                if kwargs.get("tools_spec")
                else None
            ),
            origin=str(kwargs.get("origin") or BYO_ORIGIN),
            revises_agent_id=revises_agent_id,
            target_agent_id=kwargs.get("target_agent_id"),
            plan_json=kwargs.get("plan_json"),
            constitution_version=kwargs.get("constitution_version"),
        )
        row = draft_store.get_draft_agent(draft_id)
        assert row is not None
        return row

    o.lifecycle_manager.create_draft = AsyncMock(side_effect=_create_draft)
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "ok", "files": {"greeter_agent.py": "print('hi')"}})
    o.deliver_agent_bundle = AsyncMock(return_value=1)
    return o


def _seed_fake_draft(
    orch,
    *,
    user_id: str,
    agent_id: str,
    draft_id: str,
    agent_name: str,
    tool_names: list[str],
    declared_scopes: list[str],
    declared_egress: list[str] | None = None,
    revises_agent_id: str | None = None,
) -> dict:
    """Persist the owner/target/Analyze facts required by direct spine tests."""

    store = orch.lifecycle_manager.draft_store
    tools_spec = [
        {"name": name, "description": "", "scope": "tools:read"}
        for name in tool_names
    ]
    plan = {
        "tools": tools_spec,
        "tools_used": tool_names,
        "tool_scopes": {name: "tools:read" for name in tool_names},
        "declared_scopes": declared_scopes,
        "declared_egress": declared_egress,
    }
    store.create_draft_agent(
        draft_id=draft_id,
        user_id=user_id,
        agent_name=agent_name,
        agent_slug=f"seeded_{draft_id.replace('-', '')[:12]}",
        description="seeded direct generation and delivery test draft",
        tools_spec=json.dumps(tools_spec),
        origin=BYO_ORIGIN,
        revises_agent_id=revises_agent_id,
        target_agent_id=agent_id,
        plan_json=json.dumps(plan, sort_keys=True, separators=(",", ":")),
        constitution_version=AGENT_CONSTITUTION_VERSION,
    )
    row = store.get_draft_agent(draft_id)
    assert row is not None
    spec = {
        "display_name": agent_name,
        "description": row["description"],
        "agent_id": agent_id,
        "owner_user_id": user_id,
        "declared_tools": tool_names,
        "declared_scopes": declared_scopes,
        "declared_egress": declared_egress,
        "plan": plan,
    }
    analyze_result = {
        "passed": True,
        "constitution_version": AGENT_CONSTITUTION_VERSION,
        "policy_revision": USER_AGENT_POLICY_REVISION,
        "violations": [],
        "spec_fingerprint": aa.spec_fingerprint(spec),
    }
    assert store.update_draft_agent(
        draft_id,
        analyze_result=json.dumps(analyze_result, sort_keys=True),
    )
    stored = store.get_draft_agent(draft_id)
    assert stored is not None
    assert stored["target_agent_id"] == agent_id
    return stored


@pytest.fixture()
def real_lifecycle(tmp_path):
    """A real AgentLifecycleManager whose only stub is the LLM tools call."""
    draft_store = InMemoryDraftStore()
    bundle_store = ImmutableBundleStore(
        tmp_path / "artifacts",
        contract=GENERATED_AGENT_BUNDLE_CONTRACT,
    )
    publication_service = _LifecyclePublicationService(
        draft_store,
        bundle_store,
    )
    lm = AgentLifecycleManager(
        draft_store=draft_store,
        orchestrator=None,
        generated_agent_publication_service=publication_service,
    )
    lm.user_agent_registry = make_user_agent_registry()
    lm.generator.generate_tools_file = AsyncMock(return_value=CANNED_TOOLS)
    lm.generator.refine_tools_file = AsyncMock(return_value=CANNED_TOOLS)
    created = []
    _create = lm.create_draft

    async def _tracked(*a, **kw):
        d = await _create(*a, **kw)
        created.append(d)
        return d

    lm.create_draft = _tracked
    yield lm
    for d in created:
        shutil.rmtree(os.path.join(lm._agents_dir, d["agent_slug"]), ignore_errors=True)
        draft_store.delete_draft_agent(d["id"])


async def test_analyze_violation_blocks_generation():
    o = _fake_orch()
    res = await aa.author_and_deliver(
        o, user_id="u-block", agent_name="Sharer",
        description="publishes and shares the agent with another user",
        declared_tools=["share_agent"], declared_scopes=["tools:read"])
    assert res["status"] == "analyze_failed"
    principles = {v["principle"] for v in res["violations"]}
    assert principles & {"K", "D"}                       # share/cross-user caught
    o.lifecycle_manager.create_draft.assert_awaited_once()
    draft = o.lifecycle_manager.draft_store.get_owned_draft_agent(
        "u-block",
        res["draft_id"],
    )
    assert draft is not None
    assert draft["target_agent_id"] == res["agent_id"]
    assert str(uuid.UUID(draft["target_agent_id"])) == draft["target_agent_id"]
    assert draft["state_revision"] == 1
    assert draft["constitution_version"] is None
    persisted_analyze = json.loads(draft["analyze_result"])
    assert persisted_analyze["passed"] is False
    assert {item["principle"] for item in persisted_analyze["violations"]} & {
        "K",
        "D",
    }
    o.lifecycle_manager.generate_code.assert_not_awaited() # NO code (FR-003)
    o.deliver_agent_bundle.assert_not_awaited()


async def test_malformed_egress_is_durably_rejected_before_generation():
    o = _fake_orch()

    res = await aa.author_and_deliver(
        o,
        user_id="u-egress",
        agent_name="Egress Agent",
        description="queries one explicitly declared external service",
        declared_tools=["greet"],
        declared_scopes=["tools:read"],
        declared_egress=["not a url", "everything"],
        plan={
            "tools_used": ["greet"],
            "tool_scopes": {"greet": "tools:read"},
        },
    )

    assert res["status"] == "analyze_failed"
    assert {item["principle"] for item in res["violations"]} == {"J"}
    draft = o.lifecycle_manager.draft_store.get_owned_draft_agent(
        "u-egress",
        res["draft_id"],
    )
    assert draft is not None
    persisted_analyze = json.loads(draft["analyze_result"])
    assert persisted_analyze["passed"] is False
    assert {item["principle"] for item in persisted_analyze["violations"]} == {
        "J"
    }
    o.lifecycle_manager.generate_code.assert_not_awaited()
    o.deliver_agent_bundle.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("declared_tools", "greet"),
        ("declared_tools", {"name": "greet"}),
        ("declared_scopes", "tools:read"),
        ("declared_scopes", {"scope": "tools:read"}),
        ("declared_egress", "https://api.example.test"),
        ("declared_egress", {"host": "api.example.test"}),
    ],
)
async def test_malformed_outer_declarations_are_rejected_before_draft_creation(
    field,
    malformed,
):
    """Strings/mappings must never be coerced into declaration sequences."""

    o = _fake_orch()
    kwargs = {
        "declared_tools": ["greet"],
        "declared_scopes": ["tools:read"],
        "declared_egress": ["https://api.example.test"],
    }
    kwargs[field] = malformed

    with pytest.raises(TypeError, match=rf"^{field} must be a list or tuple$"):
        await aa.author_and_deliver(
            o,
            user_id="u-malformed-declaration",
            agent_name="Malformed Declaration",
            description="must fail before acquiring a durable target identity",
            plan={
                "tools_used": ["greet"],
                "tool_scopes": {"greet": "tools:read"},
            },
            **kwargs,
        )

    o.lifecycle_manager.create_draft.assert_not_awaited()
    assert o.lifecycle_manager.draft_store.rows == {}
    o.lifecycle_manager.generate_code.assert_not_awaited()
    o.deliver_agent_bundle.assert_not_awaited()


async def test_analyze_pass_generates_validates_delivers():
    o = _fake_orch()
    res = await aa.author_and_deliver(
        o, user_id="u-ok", agent_name="Greeter",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    assert res["status"] == "delivered" and res["delivered_to"] == 1
    o.lifecycle_manager.generate_code.assert_awaited_once()
    o.deliver_agent_bundle.assert_awaited_once()
    draft = o.lifecycle_manager.draft_store.get_owned_draft_agent(
        "u-ok",
        res["draft_id"],
    )
    assert draft is not None
    assert draft["target_agent_id"] == res["agent_id"]
    assert draft["state_revision"] == 1
    assert draft["constitution_version"] == AGENT_CONSTITUTION_VERSION
    assert json.loads(draft["analyze_result"])["passed"] is True
    row = ua.get_user_agent(o.user_agent_registry, res["agent_id"])
    assert row["status"] == "validated" and row["constitution_version"]


async def test_generation_failure_reported_no_delivery():
    o = _fake_orch()
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "error", "error_message": "codegen boom"})
    res = await aa.author_and_deliver(
        o, user_id="u-gen", agent_name="Greeter2",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    assert res["status"] == "generation_failed" and "boom" in (res["error"] or "")
    o.deliver_agent_bundle.assert_not_awaited()


async def test_runtime_activation_failure_is_not_reported_as_no_host():
    o = _fake_orch()
    agent_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    ua.create_user_agent(
        o.user_agent_registry,
        agent_id=agent_id,
        owner_user_id="u-delivery-failed",
        display_name="Delivery Failed",
    )
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={
            "status": "generated",
            "files": {
                "agent_main.py": "print('ready')\n",
                "astralprims_ui.py": "def render(value):\n    return value\n",
                "protected_executor.py": "def execute(call):\n    return call()\n",
                "mcp_tools.py": CANNED_TOOLS,
                "manifest.json": "{}",
            },
            "runtime_manifest": {
                "agent_id": agent_id,
                "revision_id": revision_id,
                "bundle_sha256": "a" * 64,
                "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
                "required_runtime_lock_sha256": "b" * 64,
            },
            "bundle_sha256": "a" * 64,
            "revision_id": revision_id,
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "b" * 64,
            "artifact_relative_path": f"revisions/{agent_id}/{revision_id}",
        }
    )
    o.deliver_agent_bundle = AsyncMock(
        side_effect=lifecycle.RevisionActivationError("child_start_failed")
    )
    draft_id = str(uuid.uuid4())
    _seed_fake_draft(
        o,
        user_id="u-delivery-failed",
        agent_id=agent_id,
        draft_id=draft_id,
        agent_name="Delivery Failed",
        tool_names=["greet"],
        declared_scopes=["tools:read"],
        revises_agent_id=agent_id,
    )

    result = await aa._generate_and_deliver(
        o,
        user_id="u-delivery-failed",
        agent_id=agent_id,
        draft_id=draft_id,
        tool_names=["greet"],
        declared_scopes=["tools:read"],
        tool_scopes={"greet": "tools:read"},
    )

    assert result["status"] == "delivery_failed", result
    assert result["failure_code"] == "child_start_failed"


async def test_ambiguous_promotion_is_reported_as_delivery_pending():
    o = _fake_orch()
    agent_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    ua.create_user_agent(
        o.user_agent_registry,
        agent_id=agent_id,
        owner_user_id="u-delivery-pending",
        display_name="Delivery Pending",
    )
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={
            "status": "generated",
            "files": {
                "agent_main.py": "print('ready')\n",
                "astralprims_ui.py": "def render(value):\n    return value\n",
                "protected_executor.py": "def execute(call):\n    return call()\n",
                "mcp_tools.py": CANNED_TOOLS,
                "manifest.json": "{}",
            },
            "runtime_manifest": {
                "agent_id": agent_id,
                "revision_id": revision_id,
                "bundle_sha256": "a" * 64,
                "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
                "required_runtime_lock_sha256": "b" * 64,
            },
            "bundle_sha256": "a" * 64,
            "revision_id": revision_id,
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "b" * 64,
            "artifact_relative_path": f"revisions/{agent_id}/{revision_id}",
        }
    )
    o.deliver_agent_bundle = AsyncMock(
        side_effect=lifecycle.RevisionActivationRecoveryPendingError(
            "revision_promotion_recovery_pending"
        )
    )
    draft_id = str(uuid.uuid4())
    _seed_fake_draft(
        o,
        user_id="u-delivery-pending",
        agent_id=agent_id,
        draft_id=draft_id,
        agent_name="Delivery Pending",
        tool_names=["greet"],
        declared_scopes=["tools:read"],
        revises_agent_id=agent_id,
    )

    result = await aa._generate_and_deliver(
        o,
        user_id="u-delivery-pending",
        agent_id=agent_id,
        draft_id=draft_id,
        tool_names=["greet"],
        declared_scopes=["tools:read"],
        tool_scopes={"greet": "tools:read"},
    )

    assert result["status"] == "delivery_pending", result
    assert result["failure_code"] == "revision_promotion_recovery_pending"


async def test_same_owner_same_name_drafts_keep_distinct_immutable_targets(
    real_lifecycle,
):
    first = await real_lifecycle.create_draft(
        user_id="owner-with-shared-prefix",
        agent_name="Same Name",
        description="first independent draft",
        origin=BYO_ORIGIN,
    )
    second = await real_lifecycle.create_draft(
        user_id="owner-with-shared-prefix",
        agent_name="Same Name",
        description="second independent draft",
        origin=BYO_ORIGIN,
    )

    assert first["target_agent_id"] != second["target_agent_id"]
    assert str(uuid.UUID(first["target_agent_id"])) == first["target_agent_id"]
    assert str(uuid.UUID(second["target_agent_id"])) == second["target_agent_id"]

    await asyncio.to_thread(
        real_lifecycle.db.update_draft_agent,
        first["id"],
        agent_name="Renamed Display Only",
    )
    replay = await asyncio.to_thread(
        real_lifecycle.db.get_draft_agent,
        first["id"],
    )
    assert replay is not None
    assert aa.session_agent_id(replay) == first["target_agent_id"]


# ── The REAL generate_code → _bundle_files path (the empty-bundle defect) ──────

async def _gen_byo(lm, *, name="Byo Greeter", target_agent_id=None):
    create_kwargs = {}
    if target_agent_id is not None:
        create_kwargs["target_agent_id"] = target_agent_id
    draft = await lm.create_draft(
        user_id="u-byo", agent_name=name,
        description="greets the owner by their name",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
        **create_kwargs)
    return await lm.generate_code(
        draft["id"],
        target="byo",
        agent_id=draft["target_agent_id"],
    )


async def test_generate_code_refuses_identity_other_than_the_persisted_target(
    real_lifecycle,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Persisted Target",
        description="binds generation to one immutable draft target",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )

    with pytest.raises(ValueError, match="does not match the draft target"):
        await real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=str(uuid.uuid4()),
        )

    real_lifecycle.generator.generate_tools_file.assert_not_awaited()


def _write_bundle(tmp_path, files):
    for fname, content in files.items():
        (tmp_path / fname).write_text(content, encoding="utf-8")
    return tmp_path


def _run_bundle(bundle_dir, requests):
    """Run the bundle exactly as the desktop host does: a child process speaking
    JSON lines over stdio. Returns the frames it emitted."""
    stdin = "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in requests)
    child_env = os.environ.copy()
    lets_source = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "LETS", "src")
    )
    inherited_paths = [
        path
        for path in child_env.get("PYTHONPATH", "").split(os.pathsep)
        if path
    ]
    child_env["PYTHONPATH"] = os.pathsep.join(
        [lets_source]
        + [
            path if os.path.isabs(path) else os.path.abspath(path)
            for path in inherited_paths
        ]
    )
    proc = subprocess.run(
        [sys.executable, "agent_main.py"], cwd=str(bundle_dir), input=stdin,
        capture_output=True, text=True, timeout=90, env=child_env)
    assert proc.returncode == 0, f"worker exited {proc.returncode}: {proc.stderr[-800:]}"
    return [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]


async def test_real_generate_code_returns_finalized_v3_bundle(real_lifecycle):
    # The delivered bundle was ALWAYS EMPTY: generate_code returned a draft_agents
    # row, which has no files/agent_code column, so _bundle_files fell through to {}.
    gen = await _gen_byo(real_lifecycle)
    assert gen["status"] == "generated", gen.get("error_message")
    files = aa._bundle_files(gen)
    # The v3 artifact digest covers the executable files; manifest.json is
    # the metadata envelope retained by the legacy flat-file delivery seam.
    assert set(files) == {*BYO_BUNDLE_FILENAMES, "manifest.json"}
    assert all(files.values())                      # no empty file
    assert "TOOL_REGISTRY" in files["mcp_tools.py"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["agent_id"] == gen["target_agent_id"]
    assert manifest["revision_id"] == gen["revision_id"]
    assert manifest["bundle_sha256"] == gen["bundle_sha256"]
    assert manifest["runtime_contract_version"] == BYO_RUNTIME_CONTRACT_VERSION
    assert gen["artifact_relative_path"] == (
        f"revisions/{gen['target_agent_id']}/{gen['revision_id']}"
    )
    loaded = real_lifecycle.generated_agent_publication_service.bundle_store.load(
        gen["artifact_relative_path"],
        expected_digest=gen["bundle_sha256"],
        expected_manifest_digest=gen["manifest_sha256"],
    )
    assert loaded.files["mcp_tools.py"] == files["mcp_tools.py"]


async def test_byo_scaffold_filesystem_read_runs_off_the_event_loop(
    real_lifecycle,
    monkeypatch,
):
    loop_thread = threading.get_ident()
    real_scaffold = real_lifecycle.generator.generate_byo_scaffold
    observed_threads: list[int] = []

    def guarded_scaffold(**kwargs):
        observed_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return real_scaffold(**kwargs)

    monkeypatch.setattr(
        real_lifecycle.generator,
        "generate_byo_scaffold",
        guarded_scaffold,
    )

    generated = await _gen_byo(
        real_lifecycle,
        name="Off-loop Scaffold",
    )

    assert generated["status"] == "generated"
    assert len(observed_threads) == 1


async def test_generation_progress_uses_only_the_exact_claim_fence(
    real_lifecycle,
    monkeypatch,
):
    real_append = real_lifecycle.db.append_generation_log
    recorded: list[dict[str, object]] = []

    def recording_append(draft_id, message, **kwargs):
        recorded.append({"draft_id": draft_id, "message": message, **kwargs})
        return real_append(draft_id, message, **kwargs)

    monkeypatch.setattr(
        real_lifecycle.db,
        "append_generation_log",
        recording_append,
    )
    claim_id = str(uuid.uuid4())
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Claim-fenced Progress",
        description="records generation progress under one exact claim",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )

    result = await real_lifecycle.generate_code(
        draft["id"],
        target="byo",
        agent_id=draft["target_agent_id"],
        expected_state_revision=0,
        generation_claim_id=claim_id,
    )

    assert result["status"] == "generated"
    assert recorded
    assert all(entry["owner_user_id"] == "u-byo" for entry in recorded)
    assert all(entry["expected_revision"] == 1 for entry in recorded)
    assert all(entry["claim_id"] == claim_id for entry in recorded)


async def test_published_retry_reopens_exact_bundle_without_llm_or_new_claim(
    real_lifecycle,
):
    claim_id = str(uuid.uuid4())
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Replay Published Generation",
        description="replays one exact durable publication",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    first = await real_lifecycle.generate_code(
        draft["id"],
        target="byo",
        agent_id=draft["target_agent_id"],
        expected_state_revision=0,
        generation_claim_id=claim_id,
    )
    assert first["status"] == "generated"

    real_lifecycle.generator.generate_tools_file.reset_mock()
    replay = await real_lifecycle.generate_code(
        draft["id"],
        target="byo",
        agent_id=draft["target_agent_id"],
        expected_state_revision=0,
        generation_claim_id=claim_id,
    )

    assert replay["generation_outcome"] == "replayed"
    assert replay["publication_id"] == first["publication_id"]
    assert replay["revision_id"] == first["revision_id"]
    assert replay["bundle_sha256"] == first["bundle_sha256"]
    real_lifecycle.generator.generate_tools_file.assert_not_awaited()


async def test_authoring_no_host_retry_redelivers_exact_publication_without_regeneration(
    real_lifecycle,
    monkeypatch: pytest.MonkeyPatch,
):
    """A fresh rendered Generate action must reopen the durable publication.

    The first request can finish publication while no desktop host is connected.
    The next render carries a new transition UUID, so replay authority comes from
    the terminal Plane journal/draft pointer rather than from process memory or
    the first UI mutation id.
    """

    owner_id = "u-byo-authoring-replay"
    monkeypatch.setattr(aa, "byo_enabled", lambda: True)
    orch = MagicMock()
    orch.lifecycle_manager = real_lifecycle
    orch.user_agent_registry = real_lifecycle.user_agent_registry
    retained_delivery_operations = {}

    async def _record_no_host_delivery(*_args, **_kwargs):
        operation_id = str(uuid.uuid4())
        retained_delivery_operations[operation_id] = "completed:no_host"
        return 0

    orch.deliver_agent_bundle = AsyncMock(side_effect=_record_no_host_delivery)

    session = await aa.start_session(
        orch,
        user_id=owner_id,
        agent_name="Replayable Greeter",
        description="greets the owner by their name each morning",
    )
    draft_id = session["id"]
    ok, phase, message = await asyncio.to_thread(
        aa.advance,
        orch,
        owner_id,
        draft_id,
        {
            "agent_name": "Replayable Greeter",
            "specification": "greets the owner by their name each morning",
        },
    )
    assert ok and phase == "clarify", message
    await asyncio.to_thread(
        real_lifecycle.db.update_draft_agent,
        draft_id,
        clarify_answers=json.dumps(
            [{"question": "Whom should it greet?", "answer": "only me"}]
        ),
    )
    ok, phase, message = await asyncio.to_thread(
        aa.advance,
        orch,
        owner_id,
        draft_id,
        {},
    )
    assert ok and phase == "plan", message
    ok, phase, message = await asyncio.to_thread(
        aa.advance,
        orch,
        owner_id,
        draft_id,
        {
            "tools": "greet | tools:read | greets the owner",
            "scopes": "tools:read",
            "egress": "",
        },
    )
    assert ok and phase == "tasks", message
    ok, phase, message = await asyncio.to_thread(
        aa.advance,
        orch,
        owner_id,
        draft_id,
        {"tasks": "greet the owner"},
    )
    assert ok and phase == "analyze", message
    analyzed = await asyncio.to_thread(
        aa.run_analyze,
        orch,
        owner_id,
        draft_id,
    )
    assert analyzed["status"] == "passed"

    before_generate = await asyncio.to_thread(
        real_lifecycle.db.get_draft_agent,
        draft_id,
    )
    assert before_generate is not None
    initial_revision = int(before_generate["state_revision"])
    first_transition = str(uuid.uuid4())
    publication_service = real_lifecycle.generated_agent_publication_service
    publication_service.publish = AsyncMock(wraps=publication_service.publish)

    first = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=initial_revision,
        transition_id=first_transition,
    )
    assert first["status"] == "no_host"
    first_publication_id = next(iter(publication_service.results.values())).publication
    first_publication_id = str(first_publication_id.publication_id)
    after_first = await asyncio.to_thread(
        real_lifecycle.db.get_draft_agent,
        draft_id,
    )
    assert after_first is not None
    terminal_revision = int(after_first["state_revision"])
    assert terminal_revision > initial_revision
    assert real_lifecycle.generator.generate_tools_file.await_count == 1
    assert publication_service.publish.await_count == 1

    # Lost-response retry: the original render resubmits its stale revision and
    # stable transition id.  The exact terminal journal publication wins.
    lost_response_retry = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=initial_revision,
        transition_id=first_transition,
    )
    assert lost_response_retry["status"] == "no_host"

    # A different UUID cannot borrow the original stale transition's authority.
    # It must conflict before the terminal publication is reopened.
    stale_fresh_transition = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=initial_revision,
        transition_id=str(uuid.uuid4()),
    )
    assert stale_fresh_transition["status"] == "conflict"
    assert stale_fresh_transition["current_revision"] == terminal_revision

    # No-host retry: a newly rendered action has a fresh transition id and the
    # current revision, but must still redeliver the same immutable bytes.
    rerender_retry = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=terminal_revision,
        transition_id=str(uuid.uuid4()),
    )
    assert rerender_retry["status"] == "no_host"
    assert real_lifecycle.generator.generate_tools_file.await_count == 1
    assert publication_service.publish.await_count == 1
    assert len(publication_service.results) == 1
    replayed_publication = next(iter(publication_service.results.values()))
    assert str(replayed_publication.publication.publication_id) == first_publication_id
    assert orch.deliver_agent_bundle.await_count == 3

    replayed_publication.revision.state = "failed"
    replayed_publication.revision.failure_code = "child_start_failed"
    terminal_failure = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=terminal_revision,
        transition_id=str(uuid.uuid4()),
    )
    assert terminal_failure["status"] == "delivery_failed"
    assert terminal_failure["failure_code"] == "child_start_failed"
    assert orch.deliver_agent_bundle.await_count == 3

    # A retired immutable revision is its own durable replay authority.  The
    # delivery operation journal has a shorter retention window, so purging
    # those terminal records must not make this look like a fresh no-host send.
    replayed_publication.revision.state = "retired"
    replayed_publication.revision.failure_code = None
    retired_before_retention = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=terminal_revision,
        transition_id=str(uuid.uuid4()),
    )
    assert retired_before_retention["status"] == "delivery_failed"
    assert retired_before_retention["failure_code"] == "revision_superseded"
    assert "superseded" in retired_before_retention["error"]
    assert retained_delivery_operations
    assert orch.deliver_agent_bundle.await_count == 3

    # Model a terminal work-operation retention sweep independently removing
    # every prior delivery record. The Plane revision/publication remains.
    retained_delivery_operations.clear()
    retired_after_retention = await aa.generate_from_session(
        orch,
        owner_id,
        draft_id,
        expected_revision=terminal_revision,
        transition_id=str(uuid.uuid4()),
    )
    assert retired_after_retention == retired_before_retention
    assert retired_after_retention["status"] != "no_host"
    assert orch.deliver_agent_bundle.await_count == 3


async def test_durable_recovery_pending_is_not_legacy_terminalized(
    real_lifecycle,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Recovery Pending Generation",
        description="retains the journal-owned generation claim",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    claim_id = str(uuid.uuid4())
    real_lifecycle.generated_agent_publication_service.publish = AsyncMock(
        side_effect=GeneratedAgentPublicationRecoveryPendingError(
            "durable publication requires recovery"
        )
    )

    with pytest.raises(
        GeneratedAgentPublicationRecoveryPendingError,
        match="requires recovery",
    ):
        await real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=draft["target_agent_id"],
            expected_state_revision=0,
            generation_claim_id=claim_id,
        )

    current = real_lifecycle.db.get_draft_agent(draft["id"])
    assert current is not None
    assert current["status"] == "generating"
    assert current["generation_claim_id"] == claim_id


async def test_cancel_during_llm_generation_exactly_terminalizes_claim(
    real_lifecycle,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Cancelled LLM Generation",
        description="tests cancellation before publication begins",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    entered = asyncio.Event()

    async def blocked_generation(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    real_lifecycle.generator.generate_tools_file = AsyncMock(
        side_effect=blocked_generation
    )
    generation = asyncio.create_task(
        real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=draft["target_agent_id"],
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    generation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(generation, timeout=10)

    current = real_lifecycle.db.get_draft_agent(draft["id"])
    assert current is not None
    assert current["status"] == "error"
    assert current["generation_claim_id"] is None
    assert "cancelled" in str(current["error_message"]).lower()
    assert not (
        real_lifecycle.generated_agent_publication_service.bundle_store.root
        / "revisions"
        / draft["target_agent_id"]
    ).exists()


async def test_long_generation_renews_and_joins_exact_claim_heartbeat(
    real_lifecycle,
    monkeypatch,
):
    real_renew = real_lifecycle.db.renew_draft_generation
    renewals = 0

    def counted_renewal(**kwargs):
        nonlocal renewals
        renewals += 1
        return real_renew(**kwargs)

    async def slow_generation(**_kwargs):
        await asyncio.sleep(0.04)
        return CANNED_TOOLS

    monkeypatch.setattr(
        lifecycle,
        "_GENERATION_CLAIM_RENEW_INTERVAL_SECONDS",
        0.005,
    )
    monkeypatch.setattr(
        real_lifecycle.db,
        "renew_draft_generation",
        counted_renewal,
    )
    real_lifecycle.generator.generate_tools_file = AsyncMock(
        side_effect=slow_generation
    )

    result = await _gen_byo(
        real_lifecycle,
        name="Generation Heartbeat",
    )

    assert result["status"] == "generated"
    assert renewals >= 1
    assert not any(
        task.get_name().startswith("agent-generation-claim-heartbeat-")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


async def test_cancel_while_claim_commit_is_ambiguous_joins_and_terminalizes(
    real_lifecycle,
    monkeypatch,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Cancelled Claim Commit",
        description="tests cancellation while the generation claim commits",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    entered = threading.Event()
    release = threading.Event()
    claim_exited = threading.Event()
    real_claim = real_lifecycle.db.claim_draft_generation

    def blocked_claim(**kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("generation claim barrier timed out")
        try:
            result = real_claim(**kwargs)
            assert result is not None
            raise RuntimeError("generation claim acknowledgement was lost")
        finally:
            claim_exited.set()

    monkeypatch.setattr(
        real_lifecycle.db,
        "claim_draft_generation",
        blocked_claim,
    )
    generation = asyncio.create_task(
        real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=draft["target_agent_id"],
        )
    )
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(entered.wait, 5),
            timeout=6,
        )
        generation.cancel()
        await asyncio.sleep(0)
        assert not generation.done()
        generation.cancel()
        await asyncio.sleep(0)
        assert not generation.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(generation, timeout=10)

    assert claim_exited.is_set()
    current = real_lifecycle.db.get_draft_agent(draft["id"])
    assert current is not None
    assert current["status"] == "error"
    assert current["generation_claim_id"] is None
    assert "cancelled" in str(current["error_message"]).lower()


async def test_lost_claim_commit_acknowledgement_resumes_exact_durable_claim(
    real_lifecycle,
    monkeypatch,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Lost Claim Acknowledgement",
        description="resumes an exact generation claim after a lost response",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    real_claim = real_lifecycle.db.claim_draft_generation
    committed_claims = 0

    def commit_then_lose_acknowledgement(**kwargs):
        nonlocal committed_claims
        claimed = real_claim(**kwargs)
        assert claimed is not None
        committed_claims += 1
        raise RuntimeError("generation claim acknowledgement was lost")

    monkeypatch.setattr(
        real_lifecycle.db,
        "claim_draft_generation",
        commit_then_lose_acknowledgement,
    )

    result = await real_lifecycle.generate_code(
        draft["id"],
        target="byo",
        agent_id=draft["target_agent_id"],
        expected_state_revision=0,
        generation_claim_id=str(uuid.uuid4()),
    )

    assert result["status"] == "generated"
    assert committed_claims == 1
    current = real_lifecycle.db.get_draft_agent(draft["id"])
    assert current is not None
    assert current["status"] == "generated"
    assert current["generation_claim_id"] is None


async def test_lost_claim_ack_does_not_adopt_same_claim_mutated_successor(
    real_lifecycle,
    monkeypatch,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Mutated Claim Successor",
        description="refuses a same-claim row after its revision changed",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    real_claim = real_lifecycle.db.claim_draft_generation

    def commit_mutate_then_lose_acknowledgement(**kwargs):
        claimed = real_claim(**kwargs)
        assert claimed is not None
        with real_lifecycle.db._lock:
            row = real_lifecycle.db.rows[draft["id"]]
            row["state_revision"] = int(row["state_revision"]) + 1
        raise RuntimeError("generation claim acknowledgement was lost")

    monkeypatch.setattr(
        real_lifecycle.db,
        "claim_draft_generation",
        commit_mutate_then_lose_acknowledgement,
    )

    with pytest.raises(
        RuntimeError,
        match="generation claim acknowledgement was lost",
    ):
        await real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=draft["target_agent_id"],
            expected_state_revision=0,
            generation_claim_id=str(uuid.uuid4()),
        )

    real_lifecycle.generator.generate_tools_file.assert_not_awaited()


async def test_cancel_during_failed_claim_lookup_keeps_cancellation_primary(
    real_lifecycle,
    monkeypatch,
):
    draft = await real_lifecycle.create_draft(
        user_id="u-byo",
        agent_name="Cancelled Claim Lookup",
        description="preserves cancellation while claim recovery lookup fails",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN,
    )
    entered = threading.Event()
    release = threading.Event()
    def claim_failure(**_kwargs):
        raise RuntimeError("claim acknowledgement failed")

    def failing_recovery_lookup(**_kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("claim lookup barrier timed out")
        raise RuntimeError("authoritative claim lookup failed")

    monkeypatch.setattr(
        real_lifecycle.db,
        "claim_draft_generation",
        claim_failure,
    )
    monkeypatch.setattr(
        real_lifecycle.db,
        "get_exact_live_draft_generation_claim",
        failing_recovery_lookup,
    )
    generation = asyncio.create_task(
        real_lifecycle.generate_code(
            draft["id"],
            target="byo",
            agent_id=draft["target_agent_id"],
            expected_state_revision=0,
            generation_claim_id=str(uuid.uuid4()),
        )
    )
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(entered.wait, 5),
            timeout=6,
        )
        generation.cancel()
        await asyncio.sleep(0)
        generation.cancel()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(generation, timeout=10)

    ambiguity = raised.value.__cause__
    assert isinstance(ambiguity, RuntimeError)
    assert "acknowledgement and authoritative lookup" in str(ambiguity)
    assert isinstance(ambiguity.__cause__, RuntimeError)
    assert "authoritative claim lookup failed" in str(ambiguity.__cause__)
    assert any(
        "claim acknowledgement error" in note
        for note in getattr(ambiguity, "__notes__", ())
    )


async def test_v3_authoring_delivery_forwards_immutable_artifact_path():
    agent_id = str(uuid.uuid4())
    draft_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    artifact_path = f"revisions/{agent_id}/{revision_id}"
    executable = {
        "agent_main.py": "pass\n",
        "astralprims_ui.py": "pass\n",
        "mcp_tools.py": "TOOL_REGISTRY = {}\n",
        "protected_executor.py": "class ProtectedExecutor: pass\n",
    }
    manifest = {
        "manifest_version": 2,
        "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
        "revision_id": revision_id,
        "agent_id": agent_id,
        "required_runtime_lock_sha256": "a" * 64,
        "bundle_sha256": "b" * 64,
        "digest_algorithm": "sha256",
        "files": [],
    }
    orch = _fake_orch()
    orch.lifecycle_manager.generate_code = AsyncMock(
        return_value={
            "status": "generated",
            "agent_name": "Path Owner",
            "state_revision": 0,
            "files": {**executable, "manifest.json": json.dumps(manifest)},
            "runtime_manifest": manifest,
            "bundle_sha256": "b" * 64,
            "revision_id": revision_id,
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "a" * 64,
            "artifact_relative_path": artifact_path,
        }
    )
    orch.deliver_agent_bundle = AsyncMock(return_value=1)
    _seed_fake_draft(
        orch,
        user_id="owner",
        agent_id=agent_id,
        draft_id=draft_id,
        agent_name="Path Owner",
        tool_names=[],
        declared_scopes=[],
    )

    result = await aa._generate_and_deliver(
        orch,
        user_id="owner",
        agent_id=agent_id,
        draft_id=draft_id,
        tool_names=[],
        declared_scopes=[],
    )

    assert result["status"] == "delivered"
    assert orch.deliver_agent_bundle.await_args.kwargs[
        "artifact_relative_path"
    ] == artifact_path


async def test_bundle_is_self_contained(real_lifecycle):
    # contracts/host-bundle.md §2: the desktop host ships no backend package.
    gen = await _gen_byo(real_lifecycle, name="Byo Selfcontained")
    files = aa._bundle_files(gen)
    assert files
    for fname, src in files.items():
        for forbidden in ("from shared", "import shared", "from agents.", "sys.path.insert"):
            assert forbidden not in src, f"{fname} reaches for the backend package"


async def test_generated_card_agent_id_matches_the_user_agent_row(real_lifecycle, tmp_path):
    # The registry looks up user_agent[card.agent_id]; a slug-derived '<slug>-1'
    # finds no row and registration is refused fail-closed (and silently).
    agent_id = str(uuid.uuid4())
    ua.create_user_agent(
        real_lifecycle.user_agent_registry,
        agent_id=agent_id,
        owner_user_id="owner-sub-9",
        display_name="Byo Carded",
    )
    gen = await _gen_byo(
        real_lifecycle,
        name="Byo Carded",
        target_agent_id=agent_id,
    )
    files = aa._bundle_files(gen)
    frames = _run_bundle(_write_bundle(tmp_path, files), [])
    assert frames[0]["type"] == "register_agent"
    card = frames[0]["agent_card"]
    row = ua.get_user_agent(real_lifecycle.user_agent_registry, agent_id)
    assert card["agent_id"] == row["agent_id"]
    assert [s["name"] for s in card["skills"]] == ["greet"]
    assert "api_key" not in frames[0]      # authority is the owner's session


def test_backend_target_agent_id_is_unchanged(real_lifecycle):
    # 027 must stay byte-identical: the slug-derived id is still the default.
    files = real_lifecycle.generator.generate_template_files(
        agent_name="Legacy", description="d", slug="legacy_thing")
    assert 'agent_id = "legacy-thing-1"' in files["legacy_thing_agent.py"]


async def test_bundle_runner_dispatch_semantics(real_lifecycle, tmp_path):
    gen = await _gen_byo(real_lifecycle, name="Byo Dispatch")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [
        {"type": "mcp_request", "request_id": "r1", "method": "tools/list"},
        {"type": "mcp_request", "request_id": "r2", "method": "tools/call",
         "params": {"name": "greet", "arguments": {"name": "Sam"}}},
        {"type": "mcp_request", "request_id": "r3", "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
        {"type": "mcp_request", "request_id": "r4", "method": "bogus/method"},
        "not json at all",
    ])
    by_id = {f.get("request_id"): f for f in frames if f["type"] == "mcp_response"}
    assert [t["name"] for t in by_id["r1"]["result"]["tools"]] == ["greet"]
    assert by_id["r2"]["result"] == {"greeted": "Sam"}
    assert by_id["r2"]["ui_components"][0]["type"] == "card"
    assert by_id["r3"]["error"]["code"] == -32601          # unknown tool
    assert by_id["r4"]["error"]["code"] == -32601          # unknown method
    assert len(by_id) == 4                                  # the junk line was discarded


async def test_bundle_runner_maps_a_raised_exception_to_32603(real_lifecycle, tmp_path):
    # The tool must survive spec validation (which calls it with sample args), so
    # it only explodes on an explicit mode — otherwise auto-fix rewrites it.
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value='''
from astralprims import Text


def boom(mode="ok", **kwargs):
    if mode == "explode":
        raise ValueError("kaboom")
    return {"_ui_components": [Text(content="fine").to_dict()], "_data": {}}


TOOL_REGISTRY = {"boom": {"function": boom, "description": "boom",
                          "input_schema": {"type": "object",
                                           "properties": {"mode": {"type": "string"}}},
                          "scope": "tools:read"}}
''')
    gen = await _gen_byo(real_lifecycle, name="Byo Boom")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [{"type": "mcp_request", "request_id": "r1",
                                   "method": "tools/call",
                                   "params": {"name": "boom",
                                              "arguments": {"mode": "explode"}}}])
    resp = [f for f in frames if f.get("request_id") == "r1"][0]
    assert resp["error"]["code"] == -32603 and "kaboom" in resp["error"]["message"]


async def test_bundle_runner_maps_an_error_alert_to_an_error_response(real_lifecycle,
                                                                      tmp_path):
    """The tool-error convention the backend MCPServer implements: a tool that
    handled its own failure returns create_ui_response([Alert(variant='error')]).
    The BYO runner dropped that check, so a FAILED tool call came back as a
    SUCCESS mcp_response."""
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value='''
from astralprims import Alert, Text, create_ui_response


def risky(mode="ok", **kwargs):
    if mode == "fail":
        return create_ui_response([Alert(message="upstream said no", variant="error")])
    return {"_ui_components": [Text(content="fine").to_dict()], "_data": {}}


TOOL_REGISTRY = {"risky": {"function": risky, "description": "r",
                           "input_schema": {"type": "object",
                                            "properties": {"mode": {"type": "string"}}},
                           "scope": "tools:read"}}
''')
    gen = await _gen_byo(real_lifecycle, name="Byo Alert")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [
        {"type": "mcp_request", "request_id": "ok", "method": "tools/call",
         "params": {"name": "risky", "arguments": {"mode": "ok"}}},
        {"type": "mcp_request", "request_id": "bad", "method": "tools/call",
         "params": {"name": "risky", "arguments": {"mode": "fail"}}},
    ])
    by_id = {f.get("request_id"): f for f in frames if f["type"] == "mcp_response"}
    assert not by_id["ok"].get("error")
    assert by_id["bad"]["error"]["code"] == -32603
    assert "upstream said no" in by_id["bad"]["error"]["message"]
    # 064: an error and renderable UI are mutually exclusive on the wire.
    assert "ui_components" not in by_id["bad"]


async def test_generate_code_refuses_the_backend_target_for_a_byo_draft(real_lifecycle):
    """The exec decision is a property of the ROW, not the caller's argument: the
    REST generate endpoint passes no target, and used to run a BYO draft's code
    in-process."""
    draft = await real_lifecycle.create_draft(
        user_id="u-byo", agent_name="Byo Rowkeyed",
        description="greets the owner by their name",
        tools_spec=[{"name": "greet", "description": "greet"}],
        origin=BYO_ORIGIN)
    with pytest.raises(ValueError, match="BYO"):
        await real_lifecycle.generate_code(draft["id"])       # default target=backend


async def test_backend_coupled_tools_file_is_refused_and_not_delivered(real_lifecycle):
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value=(
        "from shared.base_agent import BaseA2AAgent\n"
        "TOOL_REGISTRY = {}\n"))
    gen = await _gen_byo(real_lifecycle, name="Byo Coupled")
    assert gen["status"] == "error"
    assert "self-contained" in (gen["error_message"] or "")
    assert aa._bundle_files(gen) == {}      # nothing to ship


async def test_authoring_refuses_to_deliver_an_empty_bundle():
    o = _fake_orch()
    o.lifecycle_manager.generate_code = AsyncMock(return_value={"status": "generated"})
    res = await aa.author_and_deliver(
        o, user_id="u-empty", agent_name="Greeter3",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    assert res["status"] == "generation_failed" and "empty" in res["error"]
    o.deliver_agent_bundle.assert_not_awaited()


# ── G1/SC-002: BYO code is NEVER executed on the server, in ANY process ───────

async def test_byo_validation_never_execs_user_code(real_lifecycle):
    def _boom(*a, **kw):
        raise AssertionError("in-process exec of user code (G1 violation)")

    real_lifecycle.validator.validate = _boom      # the ONLY exec path
    gen = await _gen_byo(real_lifecycle, name="Byo Static")
    assert gen["status"] == "generated", gen.get("error_message")
    report = json.loads(gen["validation_report"])
    assert report["tools_tested"] == 1 and report["passed"]   # it really did validate


def test_static_validation_does_not_import_run_or_touch_the_filesystem(real_lifecycle,
                                                                       tmp_path):
    """The reviewer's proof-of-exploit, inverted: a module whose import writes a
    file into the orchestrator's agent tree must not write ANYTHING, because the
    module is never imported. It is read as text."""
    probe = (tmp_path / "SBX_PROBE.txt").as_posix()
    hostile = (
        "import os, socket\n"
        f"open({probe!r}, 'w').write('pwned')\n"
        "socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "from astralprims import Text\n\n"
        "def probe(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'probe': {'function': probe, 'description': 'p',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n")
    report = real_lifecycle.validator.validate_static(hostile, "byo_probe")
    assert not os.path.exists(probe), "BYO code EXECUTED during validation (G1 violation)"
    assert report.passed and report.tools_tested == 1   # shape is fine; behavior is the host's


def test_static_validation_refuses_a_non_stdlib_import(real_lifecycle):
    """The desktop host ships stdlib + astralprims ONLY. An `import requests`
    bundle dies at import on the user's machine with no register_agent — so it is
    refused HERE, at generation, not surfaced as a silence timeout."""
    report = real_lifecycle.validator.validate_static(
        "import requests\nfrom astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n",
        "byo_reqs")
    assert not report.passed
    assert any("requests" in f.message and f.severity == "error" for f in report.findings)


def test_static_validation_accepts_stdlib_and_astralprims(real_lifecycle):
    report = real_lifecycle.validator.validate_static(CANNED_TOOLS, "byo_ok")
    assert report.passed and report.tools_tested == 1 and report.tools_passed == 1


def test_static_validation_catches_registry_and_return_shape(real_lifecycle):
    missing_registry = real_lifecycle.validator.validate_static(
        "from astralprims import Text\n\ndef t(**kwargs):\n    return {}\n", "byo_x")
    assert not missing_registry.passed

    bad_return = real_lifecycle.validator.validate_static(
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n    return {'nope': 1}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n",
        "byo_y")
    assert not bad_return.passed
    assert any(f.category == "RETURN_FORMAT" for f in bad_return.findings)


async def test_non_stdlib_bundle_is_never_delivered(real_lifecycle):
    """End-to-end: the import allowlist is a GATE on the generated bundle."""
    from unittest.mock import AsyncMock as _AM
    real_lifecycle.generator.generate_tools_file = _AM(return_value=(
        "import httpx\nfrom astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"))
    real_lifecycle.generator.refine_tools_file = _AM(side_effect=RuntimeError("no llm"))
    gen = await _gen_byo(real_lifecycle, name="Byo Httpx")
    report = json.loads(gen["validation_report"])
    assert not report["passed"]
    assert any("httpx" in f["message"] for f in report["findings"])


# ── The codegen prompt and the BYO gate must never disagree ───────────────────

def test_byo_prompt_required_imports_pass_the_byo_gate():
    """An obedient LLM emits the prompt's own required-imports block. If the gate
    rejects that block, BYO codegen can NEVER succeed (it did: the block mandated
    a sys.path.insert the self-containment gate hard-fails)."""
    from orchestrator.agent_spec import generate_llm_prompt_section, BYO_REQUIRED_IMPORTS_BLOCK
    from orchestrator.agent_generator import byo_import_violations
    from orchestrator.agent_validator import disallowed_imports

    assert byo_import_violations(BYO_REQUIRED_IMPORTS_BLOCK) == []
    assert disallowed_imports(BYO_REQUIRED_IMPORTS_BLOCK) == []

    byo_prompt = generate_llm_prompt_section(self_contained=True)
    assert "sys.path.insert" not in byo_prompt
    # The 027 prompt is unchanged (it DOES need the backend shim).
    assert "sys.path.insert" in generate_llm_prompt_section()


def test_byo_security_rules_do_not_recommend_unavailable_http_libraries():
    from orchestrator.agent_generator import security_rules_block
    byo = security_rules_block(self_contained=True)
    assert "urllib.request" in byo
    assert "use `requests`/`httpx` for HTTP only" not in byo
    assert "use `requests`/`httpx` for HTTP only" in security_rules_block()


def test_generated_runner_bakes_the_id_it_is_handed(real_lifecycle):
    files = real_lifecycle.generator.generate_byo_files(
        agent_name="X", description="d", agent_id="ua-x-abc", skill_tags=["t"],
        constitution_version="9.9.9")
    tree = ast.parse(files["agent_main.py"])
    consts = {t.id: ast.literal_eval(n.value)       # module-level constants only
              for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    assert consts["AGENT_ID"] == "ua-x-abc"
    assert json.loads(files["manifest.json"])["constitution_version"] == "9.9.9"


async def test_byo_codegen_uses_the_per_call_owner_resolver_over_the_system_one():
    """Found live 2026-07-14: BYO code generation resolved the admin-managed
    SYSTEM LLM (feature 054), which is unset on deployments that never configured
    one — so generation failed 'LLM not configured' even though the owner (who was
    actively authoring) had a working model. A user authoring their own private
    agent must generate its code with THEIR LLM. This pins the resolver override
    precedence _aresolve_client threads through, without a real LLM call."""
    from orchestrator.agent_generator import AgentCodeGenerator

    class _Cfg:
        base_url = "http://owner.example/v1"
        model = "owner-model"
        api_key = "owner-key"

    # System resolver is UNSET (the failing deployment state).
    gen = AgentCodeGenerator(config_resolver=lambda: None)
    assert await gen._aresolve_client() == (None, None)

    # The owner's per-call resolver is used instead, yielding a client + model.
    client, model = await gen._aresolve_client(config_resolver=lambda: _Cfg())
    assert client is not None and model == "owner-model"
    assert str(client.base_url).startswith("http://owner.example")
