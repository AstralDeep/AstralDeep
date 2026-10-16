"""Exercises standards-aware intake and the current-human import handoff through
the real catalog, transactional audit, and shared authoring forms.
"""

import hashlib
import json
from html.parser import HTMLParser
from uuid import uuid4

import pytest

from orchestrator.projection_surfaces import authoring
from orchestrator.user_skill_import import SkillImportError, parse_skill_import
from persistent_agents.models import AssignmentError
from tests.test_user_skill_import import (
    bound as bound, catalog as catalog, fixture as fixture, human as human,
    runtime as runtime, selected, service as service, signing_key as signing_key,
    surface_orch as surface_orch,
)


def source(*, frontmatter="", body="Use the exact weekly outline: wins, risks, asks."):
    return ("---\nname: weekly-status\ndescription: Format the weekly update.\n"
            f"license: MIT\n{frontmatter}---\n{body}\n")


@pytest.mark.parametrize("change", [
    {"frontmatter": "name: second-name\n"},
    {"frontmatter": "metadata: {author: [one, two]}\n"},
    {"frontmatter": "compatibility: [python]\n"},
    {"frontmatter": "allowed-tools: [Read, Grep]\n"},
    {"frontmatter": "scripts: [scripts/run.py]\n"},
    {"body": "Run scripts/extract.py before writing the final update."},
    {"body": "Read `references/guide.md` before writing the final update."},
    {"body": "See [guide](<references/guide.md>) for the exact outline."},
])
def test_unsupported_or_malformed_inputs_cannot_be_imported(change):
    with pytest.raises(SkillImportError):
        parse_skill_import(source(**change))


@pytest.mark.parametrize("frontmatter", [
    "name: Weekly-status\ndescription: A weekly format.\nlicense: MIT\n",
    "name: weekly--status\ndescription: A weekly format.\nlicense: MIT\n",
    "name: weekly-status\nlicense: MIT\n",
    "name: weekly-status\ndescription: ''\nlicense: MIT\n",
    "name: weekly-status\ndescription: " + "x" * 1025 + "\nlicense: MIT\n",
])
def test_standard_required_fields_are_validated_without_truncation(frontmatter):
    with pytest.raises(SkillImportError):
        parse_skill_import("---\n" + frontmatter + "---\nFollow the weekly outline.")


def test_frontmatter_terminator_must_be_a_complete_line():
    with pytest.raises(SkillImportError):
        parse_skill_import(source().replace("---\nUse", "---suffix\nUse"))


def test_yaml_quotes_folded_description_and_attribution_remain_exact():
    supplied = ("---\nname: 'weekly-status' # format name\n"
                "description: >-\n  Format the weekly update\n  for the team.\n"
                "license: MIT\nmetadata:\n  author: Example Team\n"
                "  copyright: '2026 Example Team'\n---\nFollow the weekly outline.")
    preview = parse_skill_import(supplied)
    assert preview.name == "weekly-status"
    assert preview.description == "Format the weekly update for the team."
    assert dict(preview.attribution) == {
        "author": "Example Team", "copyright": "2026 Example Team"}


@pytest.mark.parametrize("frontmatter", [
    "metadata: value\n", "metadata: {author: {nested: value}}\n",
    "metadata: {author: '&' }\n".replace("&", "x" * 257),
    "metadata: {other: '&'}\n".replace("&", "x" * 513),
    "metadata:\n" + "".join(f"  key{i}: value\n" for i in range(33)),
    "allowed-tools: ''\n", "compatibility: ''\n",
])
def test_metadata_bounds_refuse_without_dropping_attribution(frontmatter):
    with pytest.raises(SkillImportError):
        parse_skill_import(source(frontmatter=frontmatter))


def test_license_bounds_are_refused_without_truncation():
    with pytest.raises(SkillImportError, match="never truncated"):
        parse_skill_import(source().replace("license: MIT", "license: " + "x" * 129))


def test_escape_heavy_source_is_refused_before_confirmation_exceeds_socket_bounds():
    supplied = source() + "\n" * (65536 - len(source().encode()))
    assert len(supplied.encode()) == 65536
    with pytest.raises(SkillImportError) as refused:
        parse_skill_import(supplied)
    assert refused.value.code == "skill_import_too_large"


@pytest.mark.parametrize("device", ["browser", "windows", "mobile", "tablet"])
def test_maximum_source_round_trips_projection_form_and_socket_boundary(device, monkeypatch):
    from orchestrator.human_request_authority import _socket_message
    from rote.adapter import ComponentAdapter
    from rote.capabilities import DeviceProfile
    from shared.feature_flags import flags
    from shared.protocol import ChromeSurface
    from webrender.chrome.surfaces import _sdui

    monkeypatch.setitem(flags._flags, "skill_import", True)
    instructions = "📝" * 4000
    supplied = source(body=instructions)
    supplied += " " * (65536 - len(supplied.encode()))
    assert len(supplied.encode()) == 65536
    identity = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0)
    prefill = authoring._import_prefill({"skill_import": {
        **identity, "source": supplied, "name": "weekly-status", "instructions": instructions}})
    form = next(item for item in authoring._skill_form_components(None, _sdui, prefill=prefill)
                if item.get("submit_action"))
    profile = DeviceProfile.from_dict({"device_type": device, "screen_width": 1280,
                                      "screen_height": 800, "supported_types": ["param_picker"]})
    adapted = ComponentAdapter.adapt([form], profile)
    frame = json.loads(ChromeSurface(surface_key="agent_authoring", title="My skills",
                                    components=adapted, request_generation=str(uuid4())).to_json())
    assert frame["components"][0]["submit_payload"]["skill_import_source"] == supplied
    assert frame["components"][0]["submit_payload"]["skill_id"] == identity["skill_id"]
    payload = {**frame["components"][0]["submit_payload"], "fields": {
        "skill_name": "weekly-status", "skill_instructions": instructions,
        "skill_applies": "", "skill_command": ""}}
    event = {"type": "ui_event", "action": "chrome_user_skill_save", "payload": payload}
    recovered = json.loads(_socket_message(event))
    assert recovered["payload"]["skill_import_source"] == supplied
    assert recovered["payload"]["fields"]["skill_instructions"] == instructions


@pytest.mark.parametrize("change", [
    {"skill_id": "invalid"}, {"expected_revision": 1}, {"name": "changed-name"},
    {"source": "not frontmatter"}, {"source": source().replace("license: MIT\n", "")},
])
def test_import_prefill_refuses_changed_or_unbound_data(change, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "skill_import", True)
    raw = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
               name="weekly-status", instructions="Use the exact weekly outline: wins, risks, asks.",
               source=source())
    assert authoring._import_prefill({"skill_import": {**raw, **change}}) is None


@pytest.mark.asyncio
async def test_preview_and_confirmed_import_keep_identity_and_provenance(
        catalog, human, bound, fixture, runtime, surface_orch):
    from orchestrator.human_request_authority import bind_human_caller
    from webrender.chrome.surfaces import _sdui

    human[2]._user_skill_store = catalog
    caller = await selected(human, bound, fixture)
    supplied = source(frontmatter="metadata: {author: Example Team}\n")
    identity = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0)
    payload = {**identity, "skill_slug": "", "skill_enabled": "false",
               "fields": {"skill_name": "", "skill_instructions": supplied}}
    with bind_human_caller(caller):
        _, params, notice = await authoring._h_skill_save(
            human[2], object(), fixture[1], [], payload)
        assert "Nothing is saved yet" in notice and "Example Team" in notice
        prefill = authoring._import_prefill(params)
        components = authoring._skill_form_components(None, _sdui, prefill=prefill)
        form = next(item for item in components if item.get("submit_action"))
        assert {key: form["submit_payload"][key] for key in identity} == identity
        assert form["submit_payload"]["skill_enabled"] == "false"
        assert form["submit_payload"]["skill_import_source"] == supplied

        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "input" and values.get("type") == "hidden":
                    self.values[values["name"]] = values.get("value", "")

        inputs = Inputs()
        inputs.feed(authoring._skill_form(None, prefill=prefill))
        assert inputs.values["skill_id"] == identity["skill_id"]
        assert inputs.values["command_id"] == identity["command_id"]
        assert inputs.values["skill_import_source"] == supplied
        assert inputs.values["skill_enabled"] == "false"
        confirmed = {**form["submit_payload"], "fields": {
            "skill_name": "weekly-status", "skill_instructions": prefill.instructions,
            "skill_applies": "", "skill_command": ""}}
        _, _, saved = await authoring._h_skill_save(
            human[2], object(), fixture[1], [], confirmed)
        _, _, replayed = await authoring._h_skill_save(
            human[2], object(), fixture[1], [], confirmed)
        assert "saved" in saved and "saved" in replayed
    skills = await catalog.list(caller=caller)
    assert len(skills) == 1 and skills[0].skill_id == identity["skill_id"]
    assert not skills[0].enabled and skills[0].revision == 1
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_revision")["n"] == 1
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_revision WHERE command_id=%s",
                            (identity["command_id"],))["n"] == 1
        event = tx.fetch_one("SELECT outputs_meta FROM audit_events "
                             "WHERE action_type='user_skill_create'")
        meta = json.loads(event["outputs_meta"]) if isinstance(event["outputs_meta"], str) else event["outputs_meta"]
        assert meta["command_id"] == identity["command_id"]
        assert meta["import"]["source_sha256"] == hashlib.sha256(supplied.encode()).hexdigest()
        assert meta["import"]["license"] == "MIT"
        assert meta["import"]["attribution"] == {"author": "Example Team"}
    assert catalog.audit.verify_chain(fixture[1]) is None


@pytest.mark.asyncio
async def test_import_save_rechecks_flag_source_and_exact_retry_metadata(
        catalog, human, bound, fixture, runtime, monkeypatch):
    from shared.feature_flags import flags

    caller = await selected(human, bound, fixture)
    supplied = source()
    command = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                   name="weekly-status", instructions="Follow the weekly outline.",
                   applies_to="always", command="", enabled=False, slug="",
                   import_source=supplied)
    first = await catalog.save(caller=caller, **command)
    assert (await catalog.save(caller=caller, **command)).replayed
    with pytest.raises(AssignmentError, match="skill_conflict"):
        await catalog.save(caller=caller, **{**command, "import_source": supplied + "\n"})
    with pytest.raises(AssignmentError, match="skill_invalid"):
        await catalog.save(caller=caller, **{**command, "enabled": True})
    monkeypatch.setitem(flags._flags, "skill_import", False)
    with pytest.raises(AssignmentError, match="skill_import_disabled"):
        await catalog.save(caller=caller, **command)
    with runtime.transaction() as tx:
        head = runtime.repositories.preferences.skills.get(tx, owner_id=fixture[1],
            skill_id=first.head.skill_id)
        assert head.revision == 1 and not head.enabled


@pytest.mark.asyncio
async def test_import_transaction_rolls_back_provenance_on_audit_failure(
        catalog, human, bound, fixture, runtime, monkeypatch):
    caller = await selected(human, bound, fixture)
    await catalog.list(caller=caller)
    command = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                   name="weekly-status", instructions="Follow the weekly outline.",
                   applies_to="always", enabled=False, import_source=source())
    append = catalog.audit.insert_in_transaction

    def failed(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("synthetic audit storage failure")

    monkeypatch.setattr(catalog.audit, "insert_in_transaction", failed)
    with pytest.raises(AssignmentError):
        await catalog.save(caller=caller, **command)
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_revision")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events "
                            "WHERE action_type='user_skill_create'")["n"] == 0
    monkeypatch.setattr(catalog.audit, "insert_in_transaction", append)
    saved = await catalog.save(caller=caller, **command)
    assert not saved.replayed and saved.head.revision == 1
    assert catalog.audit.verify_chain(fixture[1]) is None


@pytest.mark.asyncio
async def test_lost_import_flag_after_apply_rolls_back_revision_and_audit(
        catalog, human, bound, fixture, runtime, monkeypatch):
    from shared.feature_flags import flags

    caller = await selected(human, bound, fixture)
    await catalog.list(caller=caller)
    apply = catalog.repository.apply_change

    def disabled(tx, **kwargs):
        result = apply(tx, **kwargs)
        monkeypatch.setitem(flags._flags, "skill_import", False)
        return result

    monkeypatch.setattr(catalog.repository, "apply_change", disabled)
    with pytest.raises(AssignmentError, match="skill_import_disabled"):
        await catalog.save(caller=caller, skill_id=str(uuid4()), command_id=str(uuid4()),
            expected_revision=0, name="weekly-status", instructions="Follow the weekly outline.",
            applies_to="always", enabled=False, import_source=source())
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_revision")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events "
                            "WHERE action_type='user_skill_create'")["n"] == 0


@pytest.mark.asyncio
async def test_import_cannot_borrow_read_or_foreign_owner_authority(
        catalog, human, bound, fixture, runtime):
    from orchestrator.human_request_authority import bind_human_caller

    reader = await selected(human, bound, fixture, method="GET")
    payload = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                   name="weekly-status", instructions="Follow the weekly outline.",
                   applies_to="always", enabled=False, import_source=source())
    with pytest.raises(AssignmentError, match="human_write_required"):
        await catalog.save(caller=reader, **payload)
    human[2]._user_skill_store = catalog
    caller = await selected(human, bound, fixture)
    with bind_human_caller(caller), pytest.raises(AssignmentError, match="skill_authentication_required"):
        await authoring._h_skill_save(human[2], object(), "foreign-owner", [], {
            "skill_id": payload["skill_id"], "command_id": payload["command_id"],
            "expected_revision": 0, "skill_slug": "", "skill_enabled": "false",
            "fields": {"skill_name": "", "skill_instructions": source()}})
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0


@pytest.mark.asyncio
async def test_final_import_does_not_accept_unapproved_reuse(catalog, human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    with pytest.raises(SkillImportError) as refused:
        await catalog.save(caller=caller, skill_id=str(uuid4()), command_id=str(uuid4()),
            expected_revision=0, name="weekly-status", instructions="Follow the weekly outline.",
            applies_to="always", enabled=False,
            import_source=source().replace("license: MIT\n", ""))
    assert refused.value.code == "skill_import_license_required"
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0


@pytest.mark.asyncio
async def test_import_retry_cannot_add_provenance_to_an_existing_manual_command(
        catalog, human, bound, fixture, runtime):
    caller = await selected(human, bound, fixture)
    command = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                   name="weekly-status", instructions="Follow the weekly outline.",
                   applies_to="always", enabled=False)
    await catalog.save(caller=caller, **command)
    with pytest.raises(AssignmentError, match="skill_conflict"):
        await catalog.save(caller=caller, **command, import_source=source())
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_revision")["n"] == 1
        event = tx.fetch_one("SELECT outputs_meta FROM audit_events "
                             "WHERE action_type='user_skill_create'")
        meta = json.loads(event["outputs_meta"]) if isinstance(event["outputs_meta"], str) else event["outputs_meta"]
        assert "import" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"expected_revision": 1},
    {"fields": {"skill_name": [], "skill_instructions": "some instructions"}},
    {"skill_import_source": source(), "fields": {"skill_import_source": "changed"}},
])
async def test_import_handler_refuses_inconsistent_form_identity_before_audit(
        catalog, human, bound, fixture, runtime, change):
    from orchestrator.human_request_authority import bind_human_caller

    human[2]._user_skill_store = catalog
    caller = await selected(human, bound, fixture)
    payload = dict(skill_id=str(uuid4()), command_id=str(uuid4()), expected_revision=0,
                   skill_slug="", skill_enabled="false", fields={"skill_instructions": source()})
    with bind_human_caller(caller), pytest.raises(AssignmentError, match="skill_invalid"):
        await authoring._h_skill_save(human[2], object(), fixture[1], [], {**payload, **change})
    with runtime.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM owner_skill_head")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM audit_events "
                            "WHERE action_type='user_skill_import_preview'")["n"] == 0
