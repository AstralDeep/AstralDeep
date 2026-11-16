"""Runs every connection and adapter vector through the reference modules and pins the contracted outcome of each named case.
A failure means connection.md, the committed vectors and the connection references disagree.
"""

import itertools
import json
from pathlib import Path

import connection
import connection_oauth
import exec_policy
import generate_connection_vectors
import pytest
from astral_mesh_ref import crypto, fold, testkit

VECTORS = Path(__file__).resolve().parents[2] / "vectors"
DOC = json.loads((VECTORS / "connection-v1.json").read_text(encoding="utf-8"))
OAUTH = json.loads((VECTORS / "connection-oauth-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
OAUTH_CASES = {case["name"]: case for case in OAUTH["cases"]}
LABEL = {device["id"]: name for name, device in DOC["test_devices"].items()}

E = "https://mcp.research.example/mcp"
RM = "https://mcp.research.example/.well-known/oauth-protected-resource"
AS = "https://login.research.example"
UNSUP = "unsupported:connection."

EXPECTED = {
    "key_record": "record",
    "key_state": "state",
    "key_other_setting": "other",
    "key_bare_connection_word": "other",
    "key_plural_prefix": "other",
    "key_uppercase_hex": "connection.malformed",
    "key_id_31_hex": "connection.malformed",
    "key_id_33_hex": "connection.malformed",
    "key_trailing_slash": "connection.malformed",
    "key_record_with_device": "connection.malformed",
    "key_state_without_device": "connection.malformed",
    "key_state_device_uppercase": "connection.malformed",
    "key_state_device_without_prefix": "connection.malformed",
    "key_unknown_reserved_family": "connection.malformed",
    "record_mcp_oauth_valid": "ok",
    "record_mcp_none_valid": "ok",
    "record_client_id_accepted": "ok",
    "record_local_ipv4_valid": "ok",
    "record_local_ipv6_valid": "ok",
    "record_local_https_loopback_valid": "ok",
    "record_local_names_node_valid": "ok",
    "record_local_with_public_issuer_valid": "ok",
    "record_egress_extra_port_entry_valid": "ok",
    "record_endpoint_with_port_valid": "ok",
    "record_endpoint_without_path_valid": "ok",
    "record_endpoint_punycode_valid": "ok",
    "record_issuer_trailing_slash_valid": "ok",
    "record_tool_name_with_dot_valid": "ok",
    "record_empty_tools_valid": "ok",
    "record_flow_private_valid": "ok",
    "record_tools_128_valid": "ok",
    "record_scopes_32_valid": "ok",
    "record_egress_16_valid": "ok",
    "record_name_80_valid": "ok",
    "record_egress_sorted_by_host_then_port_valid": "ok",
    "record_scheme_key_unsupported": UNSUP + "scheme_unsupported",
    "record_unknown_scheme_unsupported": UNSUP + "scheme_unsupported",
    "record_kind_a2a_unsupported": UNSUP + "kind_unsupported",
    "record_kind_unknown_unsupported": UNSUP + "kind_unsupported",
    "record_version_2_unsupported": UNSUP + "version_unsupported",
    "record_order_version_before_kind": UNSUP + "version_unsupported",
    "record_order_kind_before_fields": UNSUP + "kind_unsupported",
    "record_order_fields_before_scheme": UNSUP + "record_unsupported",
    "record_order_scheme_before_values": UNSUP + "scheme_unsupported",
    "record_unknown_field": UNSUP + "record_unsupported",
    "record_missing_field": UNSUP + "record_unsupported",
    "record_auth_unknown_field": UNSUP + "record_unsupported",
    "record_tools_129": UNSUP + "record_unsupported",
    "record_scopes_33": UNSUP + "record_unsupported",
    "record_egress_17": UNSUP + "record_unsupported",
    "record_endpoint_query": UNSUP + "record_unsupported",
    "record_endpoint_fragment": UNSUP + "record_unsupported",
    "record_endpoint_userinfo": UNSUP + "record_unsupported",
    "record_endpoint_trailing_slash": UNSUP + "record_unsupported",
    "record_endpoint_uppercase_host": UNSUP + "record_unsupported",
    "record_endpoint_default_port": UNSUP + "record_unsupported",
    "record_endpoint_http_not_local": UNSUP + "record_unsupported",
    "record_endpoint_localhost_name": UNSUP + "record_unsupported",
    "record_endpoint_private_lan_address": UNSUP + "record_unsupported",
    "record_endpoint_empty_segment": UNSUP + "record_unsupported",
    "record_endpoint_dot_segment": UNSUP + "record_unsupported",
    "record_endpoint_lowercase_percent": UNSUP + "record_unsupported",
    "record_endpoint_port_leading_zero": UNSUP + "record_unsupported",
    "record_endpoint_port_out_of_range": UNSUP + "record_unsupported",
    "record_endpoint_trailing_dot_host": UNSUP + "record_unsupported",
    "record_endpoint_single_label_host": UNSUP + "record_unsupported",
    "record_endpoint_numeric_last_label": UNSUP + "record_unsupported",
    "record_endpoint_2049_characters": UNSUP + "record_unsupported",
    "record_local_without_device": UNSUP + "record_unsupported",
    "record_device_without_local": UNSUP + "record_unsupported",
    "record_local_dns_endpoint": UNSUP + "record_unsupported",
    "record_local_device_malformed": UNSUP + "record_unsupported",
    "record_resource_differs": UNSUP + "record_unsupported",
    "record_resource_null_for_oauth": UNSUP + "record_unsupported",
    "record_issuer_null_for_oauth": UNSUP + "record_unsupported",
    "record_issuer_for_none": UNSUP + "record_unsupported",
    "record_scopes_for_none": UNSUP + "record_unsupported",
    "record_client_id_for_none": UNSUP + "record_unsupported",
    "record_issuer_http_not_local": UNSUP + "record_unsupported",
    "record_issuer_query": UNSUP + "record_unsupported",
    "record_client_id_with_space": UNSUP + "record_unsupported",
    "record_scopes_unsorted": UNSUP + "record_unsupported",
    "record_scopes_duplicate": UNSUP + "record_unsupported",
    "record_scope_with_quote": UNSUP + "record_unsupported",
    "record_tools_unsorted": UNSUP + "record_unsupported",
    "record_tools_duplicate": UNSUP + "record_unsupported",
    "record_tool_bad_hash": UNSUP + "record_unsupported",
    "record_tool_extra_field": UNSUP + "record_unsupported",
    "record_tool_name_129": UNSUP + "record_unsupported",
    "record_flow_unknown": UNSUP + "record_unsupported",
    "record_egress_missing_endpoint_host": UNSUP + "record_unsupported",
    "record_egress_missing_issuer_host": UNSUP + "record_unsupported",
    "record_egress_port_443_spelled": UNSUP + "record_unsupported",
    "record_egress_sorted_as_text": UNSUP + "record_unsupported",
    "record_egress_duplicate": UNSUP + "record_unsupported",
    "record_egress_ipv4_not_local": UNSUP + "record_unsupported",
    "record_egress_loopback_not_local": UNSUP + "record_unsupported",
    "record_egress_wildcard": UNSUP + "record_unsupported",
    "record_egress_uppercase": UNSUP + "record_unsupported",
    "record_egress_empty": UNSUP + "record_unsupported",
    "record_at_size_limit": UNSUP + "kind_unsupported",
    "record_over_size_limit": "connection.malformed",
    "record_not_object": "connection.malformed",
    "record_version_true": "connection.malformed",
    "record_version_0": "connection.malformed",
    "record_version_text": "connection.malformed",
    "record_id_mismatch": "connection.malformed",
    "record_id_missing": "connection.malformed",
    "record_name_empty": "connection.malformed",
    "record_name_81": "connection.malformed",
    "record_name_control": "connection.malformed",
    "record_name_bidi_override": "connection.malformed",
    "record_name_not_text": "connection.malformed",
    "record_kind_uppercase": "connection.malformed",
    "record_kind_missing": "connection.malformed",
    "state_not_signed_in_valid": "ok",
    "state_signed_in_valid": "ok",
    "state_signed_in_without_issuer_valid": "ok",
    "state_signed_in_with_tools_valid": "ok",
    "state_tools_128_valid": "ok",
    "state_needs_attention_refresh_refused_valid": "ok",
    "state_needs_attention_expired_valid": "ok",
    "state_needs_attention_scope_valid": "ok",
    "state_needs_attention_refused_valid": "ok",
    "state_tools_on_needs_attention": "connection.malformed",
    "state_tools_129": "connection.malformed",
    "state_tools_unsorted": "connection.malformed",
    "state_needs_attention_without_reason": "connection.malformed",
    "state_reason_on_signed_in": "connection.malformed",
    "state_unknown_state": "connection.malformed",
    "state_unknown_reason": "connection.malformed",
    "state_unknown_field": "connection.malformed",
    "state_version_2": "connection.malformed",
    "state_negative_ms": "connection.malformed",
    "state_issuer_http_not_loopback": "connection.malformed",
    "state_null": "connection.malformed",
    "fold_valid_put": {"listed": ["First:ok"]},
    "fold_put_by_node_member": {"ignored": ["connection.consent_node_device"]},
    "fold_consent_older_than_10_minutes": {"ignored": ["connection.consent_stale"]},
    "fold_consent_10_minutes_old_accepted": {"listed": ["First:ok"]},
    "fold_consent_after_event_clock": {"ignored": ["connection.consent_stale"]},
    "fold_consent_at_event_clock_accepted": {"listed": ["First:ok"]},
    "fold_wrong_digest": {"ignored": ["connection.consent_wrong_action"]},
    "fold_wrong_consent_kind": {"ignored": ["connection.consent_wrong_action"]},
    "fold_tampered_signature": {"ignored": ["connection.consent_bad_signature"]},
    "fold_high_s_signature": {"ignored": ["connection.consent_bad_signature"]},
    "fold_recarried_older_value_ignored": {
        "listed": ["Second:ok"],
        "ignored": ["connection.consent_foreign_feed"],
    },
    "fold_recarried_by_node_ignored": {
        "listed": ["Second:ok"],
        "ignored": ["connection.consent_foreign_feed"],
    },
    "fold_put_consenting_device_removed": {
        "listed": ["First:ok"],
        "ignored": ["connection.consent_unknown_device"],
        "removed_approvals": ["Second:dave"],
    },
    "fold_removed_approval_without_older_put": {
        "ignored": ["connection.consent_unknown_device"],
        "removed_approvals": ["First:dave"],
    },
    "fold_removed_approval_superseded": {
        "listed": ["Second:ok"],
        "ignored": ["connection.consent_unknown_device"],
    },
    "fold_unknown_consent_device": {"ignored": ["connection.consent_unknown_device"]},
    "fold_forged_removed_approval": {
        "ignored": ["connection.consent_unknown_device"],
    },
    "fold_removal_without_consent": {},
    "fold_removal_by_node": {},
    "fold_removal_by_removed_device_ignored": {
        "listed": ["First:ok"],
        "ignored": ["connection.writer_not_member"],
    },
    "fold_reput_after_removal": {"listed": ["Second:ok"]},
    "fold_later_put_wins": {"listed": ["Second:ok"]},
    "fold_later_invalid_put_does_not_win": {
        "listed": ["First:ok"],
        "ignored": ["connection.consent_bad_signature"],
    },
    "fold_arrival_order_independent": {
        "listed": ["First:ok"],
        "ignored": ["connection.consent_bad_signature"],
    },
    "fold_equal_clock_breaks_by_feed": "feed_order",
    "fold_counter_breaks_equal_milliseconds": {"listed": ["High:ok"]},
    "fold_state_by_own_device": {
        "listed": ["First:ok"],
        "states": ["First:alice:signed_in"],
    },
    "fold_state_written_by_another_device": {
        "listed": ["First:ok"],
        "ignored": ["connection.state_foreign"],
    },
    "fold_state_of_removed_member": {
        "listed": ["First:ok"],
        "ignored": ["connection.writer_not_member"],
    },
    "fold_order_writer_before_state_foreign": {
        "listed": ["First:ok"],
        "ignored": ["connection.writer_not_member"],
    },
    "fold_latest_state_wins": {
        "listed": ["First:ok"],
        "states": ["First:alice:needs_attention"],
    },
    "fold_later_unreadable_state_does_not_win": {
        "listed": ["First:ok"],
        "ignored": ["connection.malformed"],
        "states": ["First:alice:signed_in"],
    },
    "fold_state_with_tools_from_node": {
        "listed": ["Local:ok"],
        "states": ["Local:node:signed_in"],
    },
    "fold_state_for_unlisted_connection_dropped": {},
    "fold_states_of_several_devices": {
        "listed": ["First:ok"],
        "states": ["First:alice:signed_in", "First:bob:needs_attention"],
    },
    "fold_unknown_kind_unsupported": {"listed": ["Agent:connection.kind_unsupported"]},
    "fold_unsupported_put_wins_over_older_supported": {
        "listed": ["Second:connection.kind_unsupported"],
    },
    "fold_scheme_key_unsupported": {"listed": ["Keyed:connection.scheme_unsupported"]},
    "fold_server_id_collision": {
        "listed": ["Higher:connection.server_id_collision", "Lower:ok"],
    },
    "fold_collision_with_unsupported_lower": {
        "listed": [
            "Higher:connection.server_id_collision",
            "Lower:connection.kind_unsupported",
        ],
    },
    "fold_collision_keeps_an_earlier_reason": {
        "listed": ["Higher:connection.kind_unsupported", "Lower:ok"],
    },
    "fold_collision_with_legacy_server": {
        "listed": ["First:connection.server_id_collision"],
    },
    "fold_local_record_names_node": {"listed": ["Local:ok"]},
    "fold_mem_remember_reserved_key_not_read": {},
    "fold_other_events_not_read": {},
    "fold_malformed_reserved_key": {"ignored": ["connection.malformed"]},
    "fold_unknown_reserved_family": {"ignored": ["connection.malformed"]},
    "fold_wrong_labels": {"ignored": ["connection.malformed"]},
    "fold_chat_scope": {"ignored": ["connection.malformed"]},
    "fold_remove_with_consent_field": {
        "listed": ["First:ok"],
        "ignored": ["connection.malformed"],
    },
    "fold_put_without_consent": {"ignored": ["connection.consent_missing"]},
    "fold_put_record_malformed": {"ignored": ["connection.malformed"]},
    "fold_consent_shape_malformed": {"ignored": ["connection.malformed"]},
    "fold_put_id_differs_from_key": {"ignored": ["connection.malformed"]},
    "fold_order_malformed_before_consent_missing": {
        "ignored": ["connection.malformed"]
    },
    "fold_order_malformed_before_wrong_action": {"ignored": ["connection.malformed"]},
    "fold_order_wrong_action_before_unknown_device": {
        "ignored": ["connection.consent_wrong_action"],
    },
    "fold_order_wrong_action_before_node_device": {
        "ignored": ["connection.consent_wrong_action"],
    },
    "fold_order_node_device_before_foreign_feed": {
        "ignored": ["connection.consent_node_device"],
    },
    "fold_order_foreign_feed_before_bad_signature": {
        "ignored": ["connection.consent_foreign_feed"],
    },
    "fold_order_bad_signature_before_stale": {
        "ignored": ["connection.consent_bad_signature"],
    },
    "use_remote_on_any_device": "ok",
    "use_local_on_its_device": "ok",
    "use_local_elsewhere": "connection.local_elsewhere",
    "use_local_on_the_node_it_names": "ok",
    "use_node_local_from_an_owner_device": "connection.local_elsewhere",
    "use_unsupported": "connection.kind_unsupported",
    "use_collision": "connection.server_id_collision",
    "exposed_simple": "ok",
    "exposed_tool_53_accepted": "ok",
    "exposed_tool_54_too_long": "tool.name_too_long",
    "exposed_dot_invalid": "tool.name_invalid",
    "exposed_length_before_characters": "tool.name_too_long",
    "exposed_leading_digit_accepted": "ok",
    "exposed_longest_ask_tool_fits": "ok",
    "slug_simple": "ask_travel_agent",
    "slug_punctuation_runs": "ask_uk_research_agent",
    "slug_non_ascii": "ask_caf_and",
    "slug_digits": "ask_agent_007",
    "slug_leading_and_trailing_symbols": "ask_helper",
    "slug_hyphen_and_underscore_are_separators": "ask_multi_agent_desk",
    "slug_all_symbols": "ask_agent",
    "slug_non_ascii_only": "ask_agent",
    "slug_long_name_cut_to_49": "ask_" + "a" * 49,
    "slug_cut_lands_on_separator": "ask_" + "a" * 48,
    "egress_endpoint_allowed": "ok",
    "egress_token_endpoint_allowed": "ok",
    "egress_bare_entry_is_port_443": "ok",
    "egress_host_not_listed": "connection.egress_refused",
    "egress_port_differs": "connection.egress_refused",
    "egress_http_to_dns_host": "connection.egress_refused",
    "egress_loopback_http_allowed": "ok",
    "egress_ipv6_loopback_allowed": "ok",
    "egress_http_default_port_is_80": "ok",
    "egress_uppercase_host": "connection.egress_refused",
    "egress_userinfo": "connection.egress_refused",
    "egress_default_port_spelled": "connection.egress_refused",
    "exec_bare_entry_is_443": {"host": "mcp.research.example", "port": 443},
    "exec_port_80": {"host": "mcp.research.example", "port": 80},
    "exec_other_port": {"host": "mcp.research.example", "port": 8443},
    "exec_loopback": {"host": "127.0.0.1", "port": 8765},
    "coexistence_deployed_folds": "coexistence",
}

OAUTH_EXPECTED = {
    "challenge_bearer_quoted": {"resource_metadata": RM + "/mcp", "scope": "a b"},
    "challenge_scheme_case_insensitive": {"scope": "a"},
    "challenge_param_names_lowercased": {"resource_metadata": RM},
    "challenge_token_value": {"error": "insufficient_scope", "scope": "files.write"},
    "challenge_quoted_pair": {"realm": 'say "hi"'},
    "challenge_basic_before_bearer": {"scope": "a"},
    "challenge_basic_params_before_bearer": {"error": "invalid_token"},
    "challenge_token68_before_bearer": {"scope": "a"},
    "challenge_two_bearer_first_wins": {"scope": "first"},
    "challenge_bearer_without_params": {},
    "challenge_spaces_around_equals": {"scope": "a"},
    "challenge_no_bearer": None,
    "challenge_absent": None,
    "challenge_empty": None,
    "challenge_duplicate_param": None,
    "challenge_unterminated_quote": None,
    "challenge_non_ascii": None,
    "challenge_param_without_scheme": None,
    "rm_header_on_origin": [RM + "/mcp"],
    "rm_header_exact_well_known": [RM],
    "rm_header_offsite_host": "connection.resource_metadata_offsite",
    "rm_header_subdomain": "connection.resource_metadata_offsite",
    "rm_header_other_port": "connection.resource_metadata_offsite",
    "rm_header_http": "connection.resource_metadata_offsite",
    "rm_header_default_port_spelled": "connection.resource_metadata_offsite",
    "rm_header_uppercase_host": "connection.resource_metadata_offsite",
    "rm_header_path_prefix_lookalike": "connection.resource_metadata_offsite",
    "rm_header_other_well_known": "connection.resource_metadata_offsite",
    "rm_header_query": "connection.resource_metadata_offsite",
    "rm_header_fragment": "connection.resource_metadata_offsite",
    "rm_header_userinfo": "connection.resource_metadata_offsite",
    "rm_header_dot_segment": "connection.resource_metadata_offsite",
    "rm_header_relative": "connection.resource_metadata_offsite",
    "rm_no_header_path_then_root": [RM + "/mcp", RM],
    "rm_no_header_nested_path": [RM + "/api/mcp", RM],
    "rm_no_header_root_endpoint": [RM],
    "rm_basic_challenge_falls_back": [RM + "/mcp", RM],
    "rm_malformed_header_falls_back": [RM + "/mcp", RM],
    "rm_bearer_without_resource_metadata": [RM + "/mcp", RM],
    "prm_found_from_header": [AS],
    "prm_path_missing_root_found": [AS],
    "prm_header_url_missing_has_no_fallback": "connection.no_resource_metadata",
    "prm_none_found": "connection.no_resource_metadata",
    "prm_body_not_object_is_missing": [AS],
    "prm_resource_mismatch": "connection.resource_mismatch",
    "prm_resource_trailing_slash_mismatch": "connection.resource_mismatch",
    "prm_order_offsite_before_fetch": "connection.resource_metadata_offsite",
    "prm_order_resource_before_servers": "connection.resource_mismatch",
    "prm_no_authorization_servers": "connection.no_authorization_server",
    "prm_invalid_issuers_dropped": [AS, "https://login2.research.example/realms/uk"],
    "prm_all_issuers_invalid": "connection.no_authorization_server",
    "prm_duplicate_issuers_kept_once": [AS],
    "prm_loopback_issuer_accepted_when_local": ["http://127.0.0.1:8766"],
    "prm_loopback_issuer_dropped_when_not_local": [AS],
    "prm_invalid_scopes_supported_ignored": [AS],
    "as_urls_no_path": [
        AS + "/.well-known/oauth-authorization-server",
        AS + "/.well-known/openid-configuration",
    ],
    "as_urls_root_slash": [
        "https://tenant.research.example/.well-known/oauth-authorization-server",
        "https://tenant.research.example/.well-known/openid-configuration",
    ],
    "as_urls_with_path": [
        AS + "/.well-known/oauth-authorization-server/realms/uk",
        AS + "/.well-known/openid-configuration/realms/uk",
        AS + "/realms/uk/.well-known/openid-configuration",
    ],
    "as_urls_path_trailing_slash": [
        AS + "/.well-known/oauth-authorization-server/tenant1",
        AS + "/.well-known/openid-configuration/tenant1",
        AS + "/tenant1/.well-known/openid-configuration",
    ],
    "select_first_with_s256": AS,
    "select_skips_missing_metadata": "https://login2.research.example",
    "select_skips_absent_s256": "https://login2.research.example",
    "select_skips_empty_s256": "https://login2.research.example",
    "select_skips_plain_only": "https://login2.research.example",
    "select_stops_at_first_usable": AS,
    "select_issuer_mismatch_aborts": "connection.issuer_mismatch",
    "select_issuer_missing_in_metadata": "connection.issuer_mismatch",
    "select_oidc_path_insertion": AS + "/realms/uk",
    "select_oidc_path_appending_last": AS + "/realms/uk",
    "select_first_answer_decides_the_server": "connection.no_s256",
    "select_non_object_body_tries_next_url": AS,
    "select_redirect_answer_tries_next_url": AS,
    "select_all_unreachable": "connection.as_unreachable",
    "select_unreachable_and_no_s256": "connection.no_s256",
    "select_unreachable_and_not_found": "connection.no_s256",
    "select_unreachable_then_usable": "https://login2.research.example",
    "select_record_issuer_listed_later": "https://login2.research.example",
    "select_record_issuer_not_listed": "connection.issuer_changed",
    "select_record_issuer_without_s256": "connection.no_s256",
    "select_record_issuer_unreachable": "connection.as_unreachable",
    "reg_record_client_id": "record",
    "reg_record_client_id_over_metadata_document": "record",
    "reg_metadata_document": "metadata_document",
    "reg_metadata_document_flag_must_be_true": "dynamic",
    "reg_metadata_document_over_dynamic": "metadata_document",
    "reg_dynamic": "dynamic",
    "reg_nothing_offered": "connection.no_client_id",
    "reg_registration_endpoint_not_text": "connection.no_client_id",
    "dyn_native_accepted": ["native"],
    "dyn_native_400_then_web_accepted": ["native", "web"],
    "dyn_both_refused": "connection.registration_refused",
    "dyn_native_500_has_no_retry": "connection.registration_refused",
    "dyn_native_unreachable": "connection.registration_refused",
    "dyn_accepted_without_client_id": "connection.registration_refused",
    "dyn_web_unreachable": "connection.registration_refused",
    "scopes_from_challenge": ["mcp.read", "mcp.tools"],
    "scopes_challenge_runs_of_spaces": ["a", "b"],
    "scopes_from_scopes_supported": ["mcp.read"],
    "scopes_none": [],
    "scopes_empty_challenge_falls_through": ["mcp.read"],
    "scopes_invalid_challenge_falls_through": ["mcp.read"],
    "scopes_invalid_scopes_supported_gives_none": [],
    "scopes_sorted_and_unique": ["a", "b", "c"],
    "scopes_32_accepted": ["s%02d" % i for i in range(32)],
    "scopes_33_refused": "connection.too_many_scopes",
    "iss_supported_absent": "connection.iss_missing",
    "iss_supported_equal": "ok",
    "iss_supported_different": "connection.iss_mismatch",
    "iss_unsupported_absent": "ok",
    "iss_unsupported_equal": "ok",
    "iss_unsupported_different": "connection.iss_mismatch",
    "iss_flag_must_be_true": "ok",
    "iss_trailing_slash_differs": "connection.iss_mismatch",
    "iss_case_differs": "connection.iss_mismatch",
    "iss_empty_value": "connection.iss_mismatch",
    "iss_repeated": "connection.iss_mismatch",
    "stepup_first_allowed_with_union": ["put"],
    "stepup_second_without_success_hits_limit": ["put", "connection.step_up_limit"],
    "stepup_success_resets_the_count": ["put", "success", "put"],
    "stepup_no_new_scope_signs_in_again": ["sign_in"],
    "stepup_invalid_challenge_scope_adds_nothing": ["sign_in"],
    "stepup_not_insufficient_scope": ["connection.refused"],
    "stepup_refusal_does_not_count": ["connection.refused", "put"],
    "stepup_too_many_scopes": ["connection.too_many_scopes", "put"],
    "stepup_limit_before_scope_count": ["put", "connection.step_up_limit"],
    "bearer_same_url": "ok",
    "bearer_other_path_same_origin": "ok",
    "bearer_query_same_origin": "ok",
    "bearer_other_host": "connection.token_passthrough",
    "bearer_subdomain": "connection.token_passthrough",
    "bearer_host_prefix_lookalike": "connection.token_passthrough",
    "bearer_other_port": "connection.token_passthrough",
    "bearer_http_downgrade": "connection.token_passthrough",
    "bearer_default_port_spelled": "connection.token_passthrough",
    "bearer_userinfo": "connection.token_passthrough",
}

PUT_ORDER = [
    "malformed",
    "consent_missing",
    "consent_wrong_action",
    "consent_unknown_device",
    "consent_node_device",
    "consent_foreign_feed",
    "consent_bad_signature",
    "consent_stale",
]


def _label(device_id: str) -> str:
    return LABEL.get(device_id, device_id)


def fold_outcome(expect: dict) -> dict:
    ok = expect["ok"]
    summary = {
        "listed": sorted(
            f"{c['record']['name']}:{c['reason'] or 'ok'}" for c in ok["connections"]
        ),
        "ignored": sorted(ok["ignored"].values()),
        "removed_approvals": sorted(
            f"{a['record']['name']}:{_label(a['device'])}"
            for a in ok["removed_approvals"]
        ),
        "states": sorted(
            f"{c['record']['name']}:{_label(device)}:{state['state']}"
            for c in ok["connections"]
            for device, state in c["states"].items()
        ),
    }
    return {key: value for key, value in summary.items() if value}


def outcome(case: dict) -> object:
    kind, expect = case["kind"], case["expect"]
    if "error" in expect:
        return expect["error"]
    ok = expect["ok"]
    if kind == "connection_key":
        return ok["form"]
    if kind == "connection_record":
        return "ok" if ok["status"] == "ok" else "unsupported:" + ok["reason"]
    if kind == "connection_fold":
        return fold_outcome(expect)
    if kind == "connection_exec_host":
        return ok
    if kind == "connection_ask_slug":
        return ok["tool"]
    if kind == "connection_coexistence":
        return "coexistence"
    return "ok"


def oauth_outcome(case: dict) -> object:
    kind, expect = case["kind"], case["expect"]
    if kind == "oauth_challenge":
        return expect["ok"]["bearer"]
    if kind == "oauth_step_up_sequence":
        return [r.get("error") or r["ok"]["action"] for r in expect["ok"]["results"]]
    if "error" in expect:
        return expect["error"]
    ok = expect["ok"]
    return {
        "oauth_resource_metadata_urls": lambda: ok["urls"],
        "oauth_resource_metadata": lambda: ok["authorization_servers"],
        "oauth_as_metadata_urls": lambda: ok["urls"],
        "oauth_select_server": lambda: ok["issuer"],
        "oauth_registration": lambda: ok["branch"],
        "oauth_dynamic_registration": lambda: [
            r["application_type"] for r in ok["requests"]
        ],
        "oauth_scopes": lambda: ok["scopes"],
    }.get(kind, lambda: "ok")()


def test_documents_name_the_contract() -> None:
    assert DOC["format"] == OAUTH["format"] == "astral.vectors/v1"
    assert DOC["name"] == "connection-v1"
    assert OAUTH["name"] == "connection-oauth-v1"
    assert DOC["spec"] == OAUTH["spec"] == "106/contracts/connection.md"


def test_vectors_cover_exactly_the_named_cases() -> None:
    assert set(CASES) == set(EXPECTED)
    assert len(DOC["cases"]) == len(CASES)
    assert set(OAUTH_CASES) == set(OAUTH_EXPECTED)
    assert len(OAUTH["cases"]) == len(OAUTH_CASES)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert connection.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_has_the_contracted_outcome(name: str) -> None:
    if EXPECTED[name] == "feed_order":
        pytest.skip("pinned by test_equal_clock_goes_to_the_greater_feed")
    assert outcome(CASES[name]) == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(OAUTH_EXPECTED))
def test_adapter_case_reproduces_its_committed_expectation(name: str) -> None:
    case = OAUTH_CASES[name]
    assert connection_oauth.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(OAUTH_EXPECTED))
def test_adapter_case_has_the_contracted_outcome(name: str) -> None:
    assert oauth_outcome(OAUTH_CASES[name]) == OAUTH_EXPECTED[name]


@pytest.mark.parametrize(
    "name", sorted(n for n, c in CASES.items() if c["kind"] == "connection_fold")
)
def test_fold_ignores_the_order_events_arrive_in(name: str) -> None:
    case = CASES[name]
    events = case["input"]["events"]
    for order in (list(reversed(events)), events[1::2] + events[0::2]):
        shuffled = {**case["input"], "events": order}
        assert connection.run("connection_fold", shuffled) == case["expect"]


def test_generator_reproduces_the_committed_files() -> None:
    rendered = generate_connection_vectors.render()
    assert rendered["connection-v1"] == (VECTORS / "connection-v1.json").read_text(
        encoding="utf-8"
    )
    assert rendered["connection-oauth-v1"] == (
        VECTORS / "connection-oauth-v1.json"
    ).read_text(encoding="utf-8")


def test_check_mode_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check mode wrote a file")

    monkeypatch.setattr(Path, "write_text", refuse)
    generate_connection_vectors.main(["--check"])


def _codes_in(value: object) -> set[str]:
    if isinstance(value, str):
        text = value.removeprefix("unsupported:")
        return {text} if text.startswith(("connection.", "tool.")) else set()
    if isinstance(value, list):
        return set().union(set(), *(_codes_in(item) for item in value))
    if isinstance(value, dict):
        listed = {item.split(":", 1)[1] for item in value.get("listed", [])}
        return _codes_in(sorted(listed)) | _codes_in(value.get("ignored", []))
    return set()


def test_every_contract_code_is_exercised() -> None:
    seen = _codes_in(list(EXPECTED.values()) + list(OAUTH_EXPECTED.values()))
    wanted = set(connection.CODES) | set(connection_oauth.CODES)
    assert wanted <= seen, sorted(wanted - seen)


def test_adjacent_put_checks_are_pinned_in_the_contracted_order() -> None:
    assert connection.PUT_CHECKS == tuple("connection." + c for c in PUT_ORDER)
    for first, second in itertools.pairwise(PUT_ORDER[2:]):
        if (first, second) == ("consent_unknown_device", "consent_node_device"):
            assert "fold_order_wrong_action_before_node_device" in CASES
            continue
        name = f"fold_order_{first.removeprefix('consent_')}_before_{second.removeprefix('consent_')}"
        assert name in CASES, name
    assert "fold_order_malformed_before_wrong_action" in CASES
    assert "fold_order_malformed_before_consent_missing" in CASES


def test_equal_clock_goes_to_the_greater_feed() -> None:
    case = CASES["fold_equal_clock_breaks_by_feed"]
    puts = case["input"]["events"]
    assert len({(e["clock"]["ms"], e["clock"]["n"]) for e in puts}) == 1
    winner = max(puts, key=lambda e: e["feed"])
    owner = next(
        d for d, m in case["input"]["members"].items() if winner["feed"] in m["feeds"]
    )
    listed = case["expect"]["ok"]["connections"]
    assert [c["event"] for c in listed] == [winner["id"]]
    assert listed[0]["record"]["name"] == _label(owner)


def test_consent_window_is_ten_minutes_by_the_event_clock() -> None:
    assert connection.CONSENT_WINDOW_MS == 600000
    old = CASES["fold_consent_10_minutes_old_accepted"]["input"]["events"][0]
    assert old["clock"]["ms"] - old["body"]["consent"]["clock"]["ms"] == 600000
    stale = CASES["fold_consent_older_than_10_minutes"]["input"]["events"][0]
    assert stale["clock"]["ms"] - stale["body"]["consent"]["clock"]["ms"] == 600001
    ahead = CASES["fold_consent_after_event_clock"]["input"]["events"][0]
    assert ahead["body"]["consent"]["clock"]["ms"] - ahead["clock"]["ms"] == 1


def test_consent_digest_is_the_object_id_of_the_value() -> None:
    put = CASES["fold_valid_put"]["input"]["events"][0]
    assert put["body"]["consent"]["digest"] == crypto.object_id(put["body"]["value"])
    assert put["body"]["consent"]["kind"] == "connection"
    assert put["body"]["consent"]["device"] == DOC["test_devices"]["alice"]["id"]


def test_recarried_value_keeps_the_original_consent_bytes() -> None:
    events = CASES["fold_recarried_older_value_ignored"]["input"]["events"]
    first, _, again = sorted(events, key=fold.order_key)
    assert again["body"] == first["body"]
    assert again["feed"] != first["feed"]


def test_removed_approval_names_a_device_on_the_removed_list() -> None:
    case = CASES["fold_put_consenting_device_removed"]
    approval = case["expect"]["ok"]["removed_approvals"][0]
    assert approval["device"] in case["input"]["removed"]
    assert approval["device"] not in case["input"]["members"]


def test_state_tools_reach_the_view() -> None:
    entry = CASES["fold_state_with_tools_from_node"]["expect"]["ok"]["connections"][0]
    node = DOC["test_devices"]["node"]["id"]
    assert entry["record"]["local"] is True and entry["record"]["device"] == node
    assert entry["record"]["tools"] == []
    assert [t["name"] for t in entry["states"][node]["tools"]] == ["lookup", "search"]


def test_deployed_folds_agree_outside_the_reserved_keys() -> None:
    case = CASES["coexistence_deployed_folds"]
    events = case["input"]["events"]
    keys = case["input"]["presence_keys"]
    plain = [e for e in events if not connection.is_connection_event(e)]
    assert len(plain) < len(events)
    with_settings = fold.settings(events)
    without_settings = fold.settings(plain)
    outside = {k: v for k, v in with_settings.items() if not connection.reserved_key(k)}
    assert outside == without_settings
    assert any(connection.reserved_key(k) for k in with_settings)
    assert fold.authority(events, keys) == fold.authority(plain, keys)
    assert case["expect"]["ok"] == {
        "authority": fold.authority(events, keys),
        "settings_with": with_settings,
        "settings_without": without_settings,
    }


def test_egress_entries_map_onto_exec_hosts_where_exec_allows_them() -> None:
    for name in ("exec_bare_entry_is_443", "exec_port_80"):
        host = CASES[name]["expect"]["ok"]
        assert exec_policy._checked_host(dict(host), "hosts[0]") == host
    for name in ("exec_other_port", "exec_loopback"):
        with pytest.raises(exec_policy.PolicyError):
            exec_policy._checked_host(CASES[name]["expect"]["ok"], "hosts[0]")


def test_ask_tools_fit_the_exposed_name_budget() -> None:
    record_id = "f" * 32
    for name, case in CASES.items():
        if case["kind"] == "connection_ask_slug":
            tool = case["expect"]["ok"]["tool"]
            exposed = connection.exposed_name(record_id, tool)
            assert len(exposed) <= 64, name
    assert connection.server_id("0123abcd" + "0" * 24) == "c0123abcd"


def test_reserved_keys_cover_both_prefixes() -> None:
    assert connection.reserved_key("connection/" + "0" * 32)
    assert connection.reserved_key("connection.anything")
    assert not connection.reserved_key("connections/x")
    assert not connection.reserved_key("connection")


def test_published_test_devices_are_the_public_labels() -> None:
    for name, device in DOC["test_devices"].items():
        label = device["label"]
        assert device["id"] == testkit.TestDevice(label).id, name
        assert int(device["presence_scalar_hex"], 16) == testkit.scalar(
            label + "/presence"
        )


def test_challenge_parser_never_raises() -> None:
    for header in ['Bearer "', "Bearer a=", "=", ",,,", "Bearer ,", 'Bearer a="\\']:
        parsed = connection_oauth.bearer_params(header)
        assert parsed is None or isinstance(parsed, dict)
