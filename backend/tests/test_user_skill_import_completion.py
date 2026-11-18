
import json
from uuid import uuid4

import pytest

from orchestrator.projection_surfaces import authoring
from orchestrator.user_skill_import import SkillImportError, parse_skill_import


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


