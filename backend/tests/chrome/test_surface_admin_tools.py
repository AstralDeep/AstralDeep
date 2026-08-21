"""Feature 027 — T028: Admin tools surface (structure + handler behavior).

Runs without Postgres: a minimal fake orchestrator exposes only
``feedback_repo`` / ``onboarding_repo`` duck-typed fakes built on the
real DTO classes. Assertions are structural (markers, actions, escaping)
in the style of ``test_topbar.py`` / ``test_render_golden.py``.
"""
import asyncio
from datetime import datetime, timezone

from feedback.schemas import KnowledgeUpdateProposalDTO, ToolQualitySignalDTO
from onboarding.repository import DuplicateSlug, StepNotFound
from onboarding.schemas import TutorialStepDTO
from orchestrator.projection_surfaces import admin_tools

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def make_signal(tool_name="search_tool", agent_id="grants"):
    return ToolQualitySignalDTO(
        id="q1", agent_id=agent_id, tool_name=tool_name,
        window_start=T0, window_end=T1,
        dispatch_count=40, failure_count=10, negative_feedback_count=5,
        failure_rate=0.25, negative_feedback_rate=0.125,
        status="underperforming", computed_at=T1,
    )


def make_proposal(pid="p1", status="pending"):
    return KnowledgeUpdateProposalDTO(
        id=pid, agent_id="grants", tool_name="search_tool",
        artifact_path="grants/search.md",
        diff_payload="--- a\n+++ b\n+<script>alert(1)</script>",
        artifact_sha_at_gen="abc",
        evidence={"audit_event_ids": ["a1", "a2"], "component_feedback_ids": ["f1"]},
        status=status, reviewer_user_id=None, reviewed_at=None,
        reviewer_rationale=None, applied_at=None, generated_at=T1,
    )


def make_step(step_id=1, slug="welcome", archived=False, **over):
    base = dict(
        id=step_id, slug=slug, audience="user", display_order=step_id,
        target_kind="static", target_key="topbar.brand",
        title=f"Title {step_id}", body=f"Body {step_id}",
        archived_at=T1 if archived else None, updated_at=None,
    )
    base.update(over)
    return TutorialStepDTO(**base)


class FakeFeedbackRepo:
    def __init__(self, snaps=(), proposals=()):
        self.snaps = list(snaps)
        self.proposals = list(proposals)
        self.transitions = []

    def list_underperforming(self, *, limit=50, cursor=None):
        return list(self.snaps), None

    def category_breakdown(self, agent_id, tool_name, window_start, window_end):
        return {"wrong-data": 3, "too-slow": 1}

    def list_proposals(self, *, status=None, agent_id=None, tool_name=None,
                       limit=50, cursor=None):
        items = [
            p for p in self.proposals
            if (status is None or p.status == status)
            and (agent_id is None or p.agent_id == agent_id)
            and (tool_name is None or p.tool_name == tool_name)
        ]
        return items[:limit], None

    def get_proposal(self, proposal_id):
        for p in self.proposals:
            if str(p.id) == str(proposal_id):
                return p
        return None

    def transition_proposal(self, proposal_id, *, new_status, reviewer_user_id,
                            reviewer_rationale=None, applied=False):
        existing = self.get_proposal(proposal_id)
        if existing is None:
            return None
        self.transitions.append((proposal_id, new_status, reviewer_user_id, reviewer_rationale))
        return KnowledgeUpdateProposalDTO(
            id=existing.id, agent_id=existing.agent_id, tool_name=existing.tool_name,
            artifact_path=existing.artifact_path, diff_payload=existing.diff_payload,
            artifact_sha_at_gen=existing.artifact_sha_at_gen, evidence=existing.evidence,
            status=new_status, reviewer_user_id=reviewer_user_id, reviewed_at=T1,
            reviewer_rationale=reviewer_rationale, applied_at=None, generated_at=T1,
        )


class FakeOnboardingRepo:
    def __init__(self, steps=()):
        self.steps = {s.id: s for s in steps}
        self.list_calls = []
        self.created = []
        self.updated = []
        self.archived = []
        self.restored = []

    def list_all_steps(self, include_archived=True):
        self.list_calls.append(include_archived)
        return sorted(self.steps.values(), key=lambda s: (s.display_order, s.id))

    def get_step(self, step_id):
        return self.steps.get(step_id)

    def create_step(self, *, editor_user_id, slug, audience, display_order,
                    target_kind, target_key, title, body):
        if any(s.slug == slug for s in self.steps.values()):
            raise DuplicateSlug(slug)
        dto = make_step(
            step_id=max(self.steps, default=0) + 1, slug=slug, audience=audience,
            display_order=display_order, target_kind=target_kind,
            target_key=target_key, title=title, body=body,
        )
        self.steps[dto.id] = dto
        self.created.append((editor_user_id, slug))
        return dto

    def update_step(self, *, step_id, editor_user_id, partial):
        current = self.steps.get(step_id)
        if current is None:
            raise StepNotFound(step_id)
        merged = current.model_dump() | partial
        dto = TutorialStepDTO(**merged)
        self.steps[step_id] = dto
        changed = [k for k in partial if getattr(current, k) != partial[k]]
        self.updated.append((step_id, editor_user_id, dict(partial)))
        return dto, changed

    def archive_step(self, *, step_id, editor_user_id):
        current = self.steps.get(step_id)
        if current is None:
            raise StepNotFound(step_id)
        dto = TutorialStepDTO(**(current.model_dump() | {"archived_at": T1}))
        self.steps[step_id] = dto
        self.archived.append(step_id)
        return dto

    def restore_step(self, *, step_id, editor_user_id):
        current = self.steps.get(step_id)
        if current is None:
            raise StepNotFound(step_id)
        dto = TutorialStepDTO(**(current.model_dump() | {"archived_at": None}))
        self.steps[step_id] = dto
        self.restored.append(step_id)
        return dto


class FakeOrch:
    def __init__(self, feedback_repo=None, onboarding_repo=None):
        self.feedback_repo = feedback_repo
        self.onboarding_repo = onboarding_repo


def admin_orch(**kw):
    kw.setdefault("feedback_repo", FakeFeedbackRepo(
        snaps=[make_signal()], proposals=[make_proposal()],
    ))
    kw.setdefault("onboarding_repo", FakeOnboardingRepo(
        steps=[make_step(1), make_step(2, slug="archived-step", archived=True)],
    ))
    return FakeOrch(**kw)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def test_module_contract():
    assert admin_tools.TITLE == "Admin tools"
    assert admin_tools.ADMIN_ONLY is True
    for action in ("chrome_admin_proposal_decide", "chrome_admin_step_save",
                   "chrome_admin_step_archive", "chrome_admin_step_restore"):
        assert action in admin_tools.HANDLERS, f"missing handler: {action}"


def test_registered_in_surface_registry():
    from orchestrator.projection_surfaces import SURFACE_MODULES, get_surface
    assert SURFACE_MODULES["admin_tools"] == "orchestrator.projection_surfaces.admin_tools"
    assert get_surface("admin_tools") is admin_tools


# ---------------------------------------------------------------------------
# Render — admin gate
# ---------------------------------------------------------------------------

def test_render_denies_non_admin():
    html = run(admin_tools.render(admin_orch(), "u1", ["user"], {}))
    assert "astral-chrome-error" in html
    assert "Admin role required" in html
    # No admin data leaks past the gate.
    assert "search_tool" not in html and "Tutorial steps" not in html


def test_render_denies_empty_roles():
    html = run(admin_tools.render(admin_orch(), "u1", None, {}))
    assert "astral-chrome-error" in html


# ---------------------------------------------------------------------------
# Render — quality tab (default)
# ---------------------------------------------------------------------------

def test_quality_tab_is_default_and_lists_signals_and_proposals():
    html = run(admin_tools.render(admin_orch(), "admin1", ["admin"], {}))
    assert 'data-admin-tab="quality"' in html
    # Flagged tool card with stats + category breakdown + pending badge.
    assert "Underperforming tools" in html
    assert "search_tool" in html and "grants" in html
    assert "25.0%" in html and "12.5%" in html
    assert "wrong-data" in html and "too-slow" in html
    assert "proposal pending" in html
    # Pending proposals with decide actions (router exposes accept/reject).
    assert "Pending knowledge-update proposals" in html
    assert 'data-ui-action="chrome_admin_proposal_decide"' in html
    assert "&quot;decision&quot;: &quot;accept&quot;" in html
    assert "&quot;decision&quot;: &quot;reject&quot;" in html
    # Reject collects the rationale field from its data-ui-form container.
    assert "data-ui-form" in html and 'data-ui-collect="true"' in html
    assert 'name="rationale"' in html
    # Diff payload is escaped, never raw.
    assert "<script>" not in html and "&lt;script&gt;" in html
    # Tab bar present.
    assert "Tool quality" in html and "Tutorial admin" in html


def test_quality_tab_empty_states():
    orch = admin_orch(feedback_repo=FakeFeedbackRepo())
    html = run(admin_tools.render(orch, "admin1", ["admin"], {"tab": "quality"}))
    assert "No underperforming tools" in html
    assert "No pending knowledge-update proposals" in html


def test_quality_tab_missing_subsystem():
    orch = FakeOrch(feedback_repo=None, onboarding_repo=None)
    html = run(admin_tools.render(orch, "admin1", ["admin"], {}))
    assert "Feedback subsystem not initialized" in html


# ---------------------------------------------------------------------------
# Render — tutorial tab
# ---------------------------------------------------------------------------

def test_tutorial_tab_lists_steps_including_archived():
    orch = admin_orch()
    html = run(admin_tools.render(orch, "admin1", ["admin"], {"tab": "tutorial"}))
    assert 'data-admin-tab="tutorial"' in html
    # include_archived=True — the GET /api/admin/tutorial/steps internals.
    assert orch.onboarding_repo.list_calls == [True]
    assert "welcome" in html and "archived-step" in html
    assert "Archived" in html
    # Active step gets Archive, archived step gets Restore.
    assert 'data-ui-action="chrome_admin_step_archive"' in html
    assert 'data-ui-action="chrome_admin_step_restore"' in html
    assert "New step" in html


def test_tutorial_edit_form_prefills_step_values():
    html = run(admin_tools.render(
        admin_orch(), "admin1", ["admin"], {"tab": "tutorial", "step_id": 1}))
    assert 'data-step-form="1"' in html and "data-ui-form" in html
    assert 'data-ui-action="chrome_admin_step_save"' in html
    assert 'data-ui-collect="true"' in html
    assert "&quot;step_id&quot;: 1" in html
    assert 'value="Title 1"' in html and "Body 1" in html
    # Slug is stable on edit: shown but not collectable.
    assert 'name="slug"' not in html


def test_tutorial_new_form_has_slug_field_and_draft_prefill():
    draft = {"slug": "draft-slug", "title": "<b>Draft</b>"}
    html = run(admin_tools.render(
        admin_orch(), "admin1", ["admin"],
        {"tab": "tutorial", "step_id": "new", "draft": draft}))
    assert 'data-step-form="new"' in html
    assert 'name="slug"' in html and 'value="draft-slug"' in html
    # Draft values are escaped on re-render.
    assert "<b>Draft</b>" not in html and "&lt;b&gt;Draft&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# Handlers — admin gate (defense in depth; required by T028)
# ---------------------------------------------------------------------------

def test_every_handler_rejects_non_admin():
    orch = admin_orch()
    for action, fn in admin_tools.HANDLERS.items():
        result = run(fn(orch, None, "u1", ["user"], {"step_id": 1, "proposal_id": "p1"}))
        assert isinstance(result, tuple), f"{action} must return the error tuple"
        surface, params, notice = result
        assert surface == "admin_tools"
        assert "Admin role required" in notice
        assert "text-red-400" in notice  # error-styled notice block
    # Nothing mutated.
    assert orch.feedback_repo.transitions == []
    assert orch.onboarding_repo.created == []
    assert orch.onboarding_repo.archived == []


# ---------------------------------------------------------------------------
# Handlers — proposal decide
# ---------------------------------------------------------------------------

def test_proposal_reject_uses_router_internals():
    orch = admin_orch()
    result = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"],
        {"proposal_id": "p1", "decision": "reject", "fields": {"rationale": "bad diff"}}))
    surface, params, notice = result
    assert surface == "admin_tools" and params == {"tab": "quality"}
    assert "Proposal rejected" in notice
    assert orch.feedback_repo.transitions == [("p1", "rejected", "admin1", "bad diff")]


def test_proposal_reject_requires_rationale():
    orch = admin_orch()
    result = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"],
        {"proposal_id": "p1", "decision": "reject", "fields": {"rationale": "  "}}))
    assert "rationale is required" in result[2]
    assert orch.feedback_repo.transitions == []


def test_proposal_decide_unknown_proposal_and_decision():
    orch = admin_orch()
    missing = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"], {"proposal_id": "nope", "decision": "accept"}))
    assert "Proposal not found" in missing[2]
    weird = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"], {"proposal_id": "p1", "decision": "shrug"}))
    assert "Unknown decision" in weird[2]
    no_id = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"], {"decision": "accept"}))
    assert "Missing proposal_id" in no_id[2]


def test_proposal_reject_non_pending_is_error_not_exception():
    orch = admin_orch(feedback_repo=FakeFeedbackRepo(
        proposals=[make_proposal(status="rejected")]))
    result = run(admin_tools.HANDLERS["chrome_admin_proposal_decide"](
        orch, None, "admin1", ["admin"],
        {"proposal_id": "p1", "decision": "reject", "fields": {"rationale": "x"}}))
    assert "Invalid input" in result[2]


# ---------------------------------------------------------------------------
# Handlers — step save / archive / restore
# ---------------------------------------------------------------------------

def _create_fields(**over):
    fields = {
        "slug": "new-step", "audience": "user", "display_order": 3,
        "target_kind": "static", "target_key": "topbar.settings",
        "title": "New", "body": "Hello",
    }
    fields.update(over)
    return fields


def test_step_save_creates_when_no_step_id():
    orch = admin_orch()
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"], {"fields": _create_fields()}))
    surface, params, notice = result
    assert surface == "admin_tools" and params == {"tab": "tutorial"}
    assert "created" in notice
    assert orch.onboarding_repo.created == [("admin1", "new-step")]


def test_step_save_create_validation_error_preserves_draft():
    orch = admin_orch()
    fields = _create_fields(title="   ")
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"], {"fields": fields}))
    surface, params, notice = result
    assert "title" in notice
    assert params.get("draft") == fields and params.get("step_id") == "new"
    assert orch.onboarding_repo.created == []


def test_step_save_create_target_consistency_enforced():
    orch = admin_orch()
    # target_kind none + non-empty key → same rejection the POST body gives.
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"],
        {"fields": _create_fields(target_kind="none", target_key="x")}))
    assert "target" in result[2].lower()
    assert orch.onboarding_repo.created == []
    # target_kind none + empty key normalizes to NULL and succeeds.
    ok = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"],
        {"fields": _create_fields(target_kind="none", target_key="")}))
    assert "created" in ok[2]


def test_step_save_duplicate_slug():
    orch = admin_orch()
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"], {"fields": _create_fields(slug="welcome")}))
    assert "already exists" in result[2]


def test_step_save_updates_with_step_id_and_excludes_slug():
    orch = admin_orch()
    fields = _create_fields(slug="hax-rename", title="Renamed")
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"], {"step_id": 1, "fields": fields}))
    assert "saved" in result[2]
    (step_id, editor, patch), = orch.onboarding_repo.updated
    assert step_id == 1 and editor == "admin1"
    assert "slug" not in patch  # slugs are stable (PUT contract)
    assert patch["title"] == "Renamed"


def test_step_save_update_unknown_step():
    orch = admin_orch()
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        orch, None, "admin1", ["admin"], {"step_id": 99, "fields": _create_fields()}))
    assert "Step not found" in result[2]


def test_step_save_no_fields():
    result = run(admin_tools.HANDLERS["chrome_admin_step_save"](
        admin_orch(), None, "admin1", ["admin"], {}))
    assert "No form fields" in result[2]


def test_step_archive_and_restore_round_trip():
    orch = admin_orch()
    archived = run(admin_tools.HANDLERS["chrome_admin_step_archive"](
        orch, None, "admin1", ["admin"], {"step_id": 1}))
    assert "archived" in archived[2] and orch.onboarding_repo.archived == [1]
    restored = run(admin_tools.HANDLERS["chrome_admin_step_restore"](
        orch, None, "admin1", ["admin"], {"step_id": 1}))
    assert "restored" in restored[2] and orch.onboarding_repo.restored == [1]


def test_step_archive_invalid_or_missing_id():
    orch = admin_orch()
    bad = run(admin_tools.HANDLERS["chrome_admin_step_archive"](
        orch, None, "admin1", ["admin"], {"step_id": "abc"}))
    assert "Invalid step_id" in bad[2]
    missing = run(admin_tools.HANDLERS["chrome_admin_step_archive"](
        orch, None, "admin1", ["admin"], {"step_id": 42}))
    assert "Step not found" in missing[2]
