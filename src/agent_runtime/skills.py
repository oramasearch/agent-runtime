"""The skills a person grants a user-built agent, and what each one lets it reach.

A skill names capabilities; it never carries a schema. Every tool's input schema comes from an
OpenAPI document at run time, so a skill grants only what Lake actually publishes:

- ``engine``: the workspace engine's own names, reached through Lake's filtered workspace OpenAPI
  (``/workspaces/<customer>/zetta/v1/``, lake#235). The five original skills grant these, and the
  runtime calls them as before (``tools.workspace_tools``).
- ``lake``: Lake's own capabilities (``/v1/<name>``, Lake's OpenAPI). What a call does comes from
  the operation's ``x-lake-action``: a ``read`` is a tool that calls Lake; anything else is a
  **proposal**, the exact capability and input returned for a person to confirm in Lake, never sent
  by this runtime (``tools.lake_tools``).

When an engine tool and a Lake tool have the same name, the engine tool is the one the agent gets,
so the five original skills behave as they did.

Every Lake capability is accounted for: it belongs to a skill, to the App and build capabilities
every agent has (``apps.py``), or to ``NEVER_FOR_AGENTS``. ``tests/test_skills.py`` fails when Lake
publishes a capability that is in none of them, so a new Lake capability is a decision, not an
accident.

Lake's Agent Factory, the agent pages and the workspace engine's agent record accept exactly the
names in ``SKILLS`` (lake ``docs/api/agent-factory.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

# Solution reads are useful to agents that inspect, train, summarize, or call a deployed API.
# Mutations belong only to call-api, and lake's filtered OpenAPI must also grant each one.
SOLUTION_READ_CAPABILITIES = frozenset({
    "list_solutions",
    "get_solution",
    "get_policy",
    "list_ledger",
    "get_experiment",
    "get_monitor",
    "get_roi_readout",
})
SOLUTION_ACTION_CAPABILITIES = frozenset({
    "decide",
    "record_action",
    "record_outcome",
    "approve_decision",
})

# The same solution reads under Lake's names (lake#650: Lake says get_holdout_experiment where the
# engine says get_experiment).
LAKE_SOLUTION_READS = frozenset({
    "list_solutions", "get_solution", "get_policy", "list_ledger", "get_holdout_experiment",
    "get_monitor", "get_roi_readout",
})


@dataclass(frozen=True)
class Skill:
    name: str
    label: str
    engine: frozenset[str]
    lake: frozenset[str]


def _skill(name: str, label: str, engine: set[str] | frozenset[str] = frozenset(),
           lake: set[str] | frozenset[str] = frozenset()) -> Skill:
    return Skill(name, label, frozenset(engine), frozenset(lake))


SKILLS: dict[str, Skill] = {skill.name: skill for skill in [
    # -- the original five (Studio's skills, orama-design studioAgents.js) ----------------------
    _skill(
        "search", "Search data",
        engine={"list_datasets", "get_dataset", "list_connections", "list_source_tables",
                "list_indexes", "get_index", "search"} | SOLUTION_READ_CAPABILITIES,
        lake={
            # Lake-wide search across repositories, and the Docs.
            "search", "list_docs", "get_doc",
            # Search indexes: reads are tools, creating and exposing one is a proposal.
            "list_indexes", "get_index", "search_index", "search_workspace_index",
            "create_index", "expose_index", "unexpose_index",
            "list_datasets", "get_dataset", "list_connections", "list_source_tables",
        } | LAKE_SOLUTION_READS,
    ),
    _skill(
        "generate-data", "Generate a dataset",
        engine={"list_datasets", "list_dataset_generators", "generate_dataset"},
        lake={"list_datasets", "list_dataset_generators", "generate_dataset"},
    ),
    _skill(
        "train", "Train a Model",
        engine={"list_datasets", "get_dataset", "list_projects", "get_project", "get_graph",
                "list_runs", "get_run", "get_run_report", "list_models", "get_model", "gate",
                "run"} | SOLUTION_READ_CAPABILITIES,
        lake={
            "list_datasets", "get_dataset", "list_projects", "get_project", "list_runs",
            "get_run", "get_run_report", "get_run_metrics", "compare_runs", "list_jobs",
            "get_job", "list_models", "get_model", "list_methodologies",
            # Experiments (the engine's studies).
            "list_experiments", "get_experiment", "list_experiment_trials",
            "get_experiment_analysis", "validate_experiment", "tail_experiment_events",
            "start_experiment", "stop_experiment", "stop_experiment_trial", "promote_trial",
            "agent_training_status",
        } | LAKE_SOLUTION_READS,
    ),
    _skill(
        "summarize", "Summarize and explain",
        engine={"list_projects", "get_project", "list_runs", "get_run", "get_run_report",
                "list_models", "get_model", "model_health"} | SOLUTION_READ_CAPABILITIES,
        lake={
            "list_projects", "get_project", "list_runs", "get_run", "get_run_report",
            "list_models", "get_model", "model_health", "model_traffic",
            "workspace_engine_info", "workspace_status", "get_workspace", "list_traces",
            "get_trace",
        } | LAKE_SOLUTION_READS,
    ),
    _skill(
        "call-api", "Call the API",
        engine={"list_models", "get_model", "score", "explain", "list_indexes", "search"}
        | SOLUTION_READ_CAPABILITIES | SOLUTION_ACTION_CAPABILITIES,
        lake={
            "list_models", "get_model", "score", "explain", "predict", "resolve_model",
            "list_indexes", "expose_model", "unexpose_model",
            "decide", "record_outcome", "approve_decision",
        } | LAKE_SOLUTION_READS,
    ),
    # -- Lake's own areas (agent-runtime#7) -------------------------------------------------------
    _skill(
        "drives", "Use drives",
        lake={
            "list_drives", "get_drive", "list_drive_entries", "get_drive_entry",
            "read_drive_file", "recent_drive_entries", "search_drives", "drive_browse",
            "drive_preview", "drive_history", "diff_drive_versions", "drive_conflict",
            "download_drive_file", "drive_shared_with_me",
            "create_drive", "rename_drive", "delete_drive", "create_drive_folder",
            "create_drive_folder_entry", "create_drive_shortcut", "move_drive_entry",
            "move_drive_path", "delete_drive_entry", "delete_drive_path", "upload_drive_entries",
            "upload_drive_files", "begin_drive_upload", "append_drive_upload",
            "finish_drive_upload", "restore_drive_version",
        },
    ),
    _skill(
        "transcripts", "Use transcripts",
        lake={"list_transcripts", "get_transcript", "create_transcript", "update_transcript",
              "delete_transcript", "transcribe"},
    ),
    _skill(
        "repositories", "Work in repositories",
        lake={
            "list_repositories", "get_repository", "repository_tree", "repository_file",
            "repository_refs", "refs", "list_changes", "get_change", "change_diff",
            "list_conflicts", "list_issues", "list_issue_comments", "list_reviews",
            "list_review_comments", "list_releases", "list_artifacts", "get_artifact",
            "resolve_dataset_file", "resolve_model_file", "list_environments", "get_environment",
            "environment_logs", "list_deployments", "list_branch_protections",
            "create_issue", "create_issue_comment", "update_issue", "create_review",
            "create_review_comment", "update_review", "map_review_anchors",
            "stage_repository_file", "commit_repository_files", "resolve_conflict",
            "create_repository", "rename_repository", "delete_repository", "create_release",
            "import_repository", "import_github_repository",
        },
    ),
    _skill(
        "datasets", "Load datasets",
        lake={
            "list_datasets", "get_dataset", "analyze_dataset", "plan_features",
            "list_connections", "list_source_tables", "list_dataset_generators",
            "load_drive_dataset", "upload_dataset", "create_dataset_from_query",
            "promote_dataset", "create_connection", "delete_connection",
        },
    ),
    _skill(
        "streams", "Read and publish streams",
        lake={"list_streams", "get_stream", "browse_stream", "publish_stream_message",
              "create_stream", "update_stream", "update_stream_message", "delete_stream",
              "delete_stream_messages"},
    ),
    _skill(
        "pipelines", "Design pipelines",
        lake={
            "list_projects", "get_project", "get_pipeline", "render_pipeline",
            "list_methodologies", "list_runs", "get_run", "get_run_report", "list_opportunities",
            "latest_agent_task", "list_solutions", "get_solution",
            "create_project", "delete_project", "capture_intent", "compile_intent",
            "compile_pipeline", "create_pipeline", "edit_pipeline", "save_pipeline",
            "import_n8n", "compile_n8n", "compile_python", "check_pipeline", "approve_pipeline",
            "run_pipeline", "start_run", "deploy_pipeline", "agent_run",
            "create_projects_from_opportunities", "dismiss_opportunity",
            "compose_solution", "create_solution", "update_solution", "duplicate_solution",
            "delete_solution", "confirm_solution", "start_solution", "run_solution",
            "update_policy",
        },
    ),
    _skill(
        "usage", "Read usage and billing",
        lake={"metering_usage", "usage_over_time", "usage_limits", "billing_account",
              "bill_preview", "price_list", "list_invoices", "get_invoice"},
    ),
]}

# Lake capabilities no user-built agent is ever given, by why. A person does these, Lake does them
# for itself, or another credential (an App's, a build's, the operator's) does them.
NEVER_FOR_AGENTS: dict[str, frozenset[str]] = {
    "sign-in, sessions and MFA": frozenset({
        "sign_in_password", "sign_up_password", "sign_in_passkey", "sign_in_saml",
        "sign_in_sso", "sign_in_taap", "start_oauth", "start_sso_oidc",
        "begin_passkey_sign_in", "begin_passkey_registration", "register_passkey",
        "enroll_mfa", "verify_mfa", "disable_mfa", "list_sessions", "revoke_session",
        "revoke_user_sessions", "list_devices", "revoke_device", "viewer", "identity",
        "my_permissions", "mcp_setup",
    }),
    "account and repository recovery": frozenset({
        "begin_email_recovery", "complete_recovery", "recover_with_code",
        "begin_admin_recovery", "approve_admin_recovery", "begin_root_recovery",
        "complete_root_recovery", "claim_recovery_deliveries", "list_recovery_notifications",
        "configure_recovery_contact", "recover_repository_key", "repository_recovery_status",
    }),
    "key, token and certificate minting": frozenset({
        "create_token", "mint_agent_session_key", "revoke_agent_session",
        "verify_agent_session_drip", "issue_agent_certificate", "rotate_agent_certificate",
        "revoke_agent_certificate", "check_agent_certificate", "issue_client_certificate",
        "issue_device_certificate", "issue_jwt_svid", "issue_x509_svid",
        "issue_ssh_host_certificate", "issue_ssh_user_certificate", "create_pki_authority",
        "exchange_cloud_attestation", "exchange_jwt_attestation",
        "exchange_kubernetes_attestation", "issue_offline_grant", "list_offline_grants",
        "revoke_offline_grant", "check_cached_read", "create_federation_issuer",
        "list_federation_issuers",
    }),
    "secrets and encryption keys": frozenset({
        "read_secret", "put_secret", "list_secrets", "secret_metadata", "delete_secret_versions",
        "destroy_secret_versions", "undelete_secret_versions", "mint_sealed_secret",
        "lease_dynamic_secret", "renew_dynamic_secret", "revoke_dynamic_secret",
        "create_secret_mount", "get_secret_mount", "list_secret_mounts", "create_encryption_key",
        "get_encryption_key", "list_encryption_keys", "rotate_encryption_key",
        "derive_encryption_key", "rotate_customer_kek", "encrypt", "decrypt", "rewrap", "sign",
        "verify", "hmac", "set_environment_env", "unset_environment_env", "service_connection",
        "store_connection",
    }),
    "people, organizations and access": frozenset({
        "create_customer", "get_customer", "list_customers", "create_user", "get_user",
        "list_users", "create_organization", "get_organization", "list_organizations",
        "create_organization_space", "join_organization", "convert_personal_space",
        "create_team", "get_team", "list_teams", "create_membership", "get_membership",
        "list_memberships", "set_member_role", "remove_member", "create_invitation",
        "list_invitations", "accept_invitation", "create_grant", "list_grants",
        "create_object", "get_object", "list_objects", "effective_access",
        "entity_effective_access", "set_customer_credential_policy", "issue_assignees",
        "share_drive_folder", "drive_share_people", "drive_effective_access",
        "create_drive_share_link", "revoke_drive_share_link", "preview_drive_share_link",
        "download_drive_share_link", "resolve_drive_share_link", "set_repository_visibility",
        "transfer_repository", "set_branch_protection",
    }),
    "single sign-on and directory sync": frozenset({
        "register_sso_provider", "list_sso_providers", "test_sso_provider",
        "list_sso_provisioning_logs", "register_oauth_provider", "upsert_scim_user",
        "upsert_scim_group", "scim_get_user", "scim_list_users", "scim_upsert_user",
        "scim_patch_user", "scim_delete_user", "scim_get_group", "scim_list_groups",
        "scim_upsert_group", "scim_patch_group", "scim_delete_group",
    }),
    "sign-in for a person's own applications": frozenset({
        "register_application", "get_application", "list_applications", "application_jwks",
        "rotate_application_signing_key", "update_application_theme",
        "begin_application_authorization", "exchange_application_authorization_code",
        "issue_application_jwt", "list_application_users", "export_application_user",
        "suspend_application_user", "delete_application_user",
        "create_application_organization", "list_application_organizations",
        "update_application_organization", "delete_application_organization",
        "create_application_webhook", "list_application_webhooks",
        "list_application_webhook_events", "retry_application_webhook",
    }),
    "audit and the transparency log": frozenset({
        "audit", "my_audit", "export_audit", "verify_audit", "set_audit_retention",
        "append_transparency_entries", "transparency_checkpoint",
        "transparency_consistency_proof", "transparency_identity_scan",
        "transparency_inclusion_proof", "transparency_log_configuration",
        "transparency_proof_bundle", "transparency_tile", "publish_transparency_checkpoint",
        "replicate_transparency_log", "export_transparency_replication_batch",
        "timestamp_checkpoint", "timestamp_evidence", "renew_timestamp_evidence",
        "initialize_tuf", "publish_tuf_metadata", "rotate_tuf_root", "tuf_metadata",
        "rotate_identity", "attach_model_provenance", "verify_model_provenance",
    }),
    "billing changes and limits": frozenset({
        "set_price_list", "issue_invoice", "record_invoice_payment", "void_invoice",
        "set_usage_limit", "set_storage_quota",
    }),
    "workspaces, stores and infrastructure": frozenset({
        "create_workspace", "update_workspace", "delete_workspace", "start_workspace",
        "list_workspaces", "set_workspace_storage", "move_workspace_store",
        "remove_space_store", "copy_workspace_records", "list_stores", "list_store_providers",
        "create_store", "delete_store", "list_services", "list_service_kinds",
        "create_service", "delete_service", "connect_service", "request_volume_snapshot",
        "volume_snapshots", "create_domain", "delete_domain", "list_image_repositories",
        "create_image_repository", "unroute_image_repository", "list_packages",
        "reindex_artifacts", "replicate_repository", "export_repository", "list_exports",
        "prepare_repository_upload", "server_info",
    }),
    "webhooks": frozenset({
        "create_webhook", "list_webhooks", "list_webhook_deliveries", "deliver_webhook",
        "retry_webhook_delivery",
    }),
    "the generic workspace surface (it would bypass a skill's names)": frozenset({
        "workspace_query", "workspace_act", "workspace_sql", "workspace_openapi",
    }),
    "agents, Ask Orama and the agent bus": frozenset({
        "list_agents", "get_agent", "create_agent", "update_agent", "delete_agent",
        "set_training_schedule", "chat_with_agent", "agent_turn", "launch_agent",
        "list_agent_launches", "stop_agent_launch", "schedule_agent", "unschedule_agent",
        "list_agent_schedules", "list_agent_library", "approve_agent_proposal",
        "create_agent_from_proposal", "send_ask_message", "settle_ask_turn",
        "get_ask_conversation", "confirm_ask_proposal", "get_agent_task", "receive_agent_events",
        "receive_agent_tasks", "claim_agent_task", "publish_agent_event", "record_model_usage",
    }),
    "events only an App, a build or the operator publishes": frozenset({
        "publish_app_event", "publish_build_event",
    }),
}


def never_for_agents() -> frozenset[str]:
    names: set[str] = set()
    for group in NEVER_FOR_AGENTS.values():
        names |= group
    return frozenset(names)


def engine_grant(skills: list[str]) -> set[str]:
    """The workspace engine capabilities the agent's skills grant (engine names)."""
    granted: set[str] = set()
    for name in skills:
        skill = SKILLS.get(name)
        if skill is not None:
            granted |= skill.engine
    return granted


def lake_grant(skills: list[str]) -> set[str]:
    """The Lake capabilities the agent's skills grant, never one on the never list."""
    granted: set[str] = set()
    for name in skills:
        skill = SKILLS.get(name)
        if skill is not None:
            granted |= skill.lake
    return granted - never_for_agents()
