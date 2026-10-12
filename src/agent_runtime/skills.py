"""The skills a person grants a user-built agent, and what each one lets it reach.

A skill names Lake capabilities (``/v1/<name>``, Lake's OpenAPI); it never carries a schema. Every
tool's input schema comes from Lake's OpenAPI at run time, so a skill grants only what Lake
actually publishes. What a call does comes from the operation's ``x-lake-action``:

- a ``read`` is a tool that calls Lake;
- any other is a **proposal**, the exact capability and input returned for a person to confirm in
  Lake, never sent by this runtime (``tools.lake_tools``);
- except a skill's ``direct`` actions: the few workspace engine actions the runtime calls itself,
  because the engine's own policy governs each one (gate checks, holdouts, policy tiers, the
  append-only ledger). Which actions are direct, and why, is ``docs/skills/README.md`` (*Direct
  actions*, agent-runtime#11).

The five original skills used to reach the engine under its own names through Lake's workspace
Zetta route (``/workspaces/<customer>/zetta/v1/``). Since lake#674 every engine capability has a
Lake name (``check_pipeline`` for ``gate``, ``start_run`` for ``run``, ``get_pipeline`` for
``get_graph``, ``search_workspace_index`` for ``search``, ``get_holdout_experiment`` for the
solution ``get_experiment``), so every skill grants Lake names only.

Every Lake capability is accounted for: it belongs to a skill, to the App and build capabilities
every agent has (``apps.py``), or to ``NEVER_FOR_AGENTS``. ``tests/test_skills.py`` fails when Lake
publishes a capability that is in none of them, so a new Lake capability is a decision, not an
accident.

Lake's Agent Factory, the agent pages and the workspace engine's agent record accept exactly the
names in ``SKILLS`` (lake ``docs/api/agent-factory.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

# The solution registry's reads, under Lake's names (lake#650, lake#674: Lake says
# get_holdout_experiment where the engine says get_experiment). Useful to agents that inspect,
# train, summarize, or call a deployed API.
SOLUTION_READS = frozenset({
    "list_solutions", "get_solution", "get_policy", "list_ledger", "get_holdout_experiment",
    "get_monitor", "get_roi_readout",
})


@dataclass(frozen=True)
class Skill:
    name: str
    label: str
    # Every Lake capability the skill grants: reads are tools, changes are proposals.
    lake: frozenset[str]
    # The changes among them the runtime calls itself, governed by the engine's own policy.
    direct: frozenset[str] = frozenset()


def _skill(name: str, label: str, lake: set[str] | frozenset[str],
           direct: set[str] | frozenset[str] = frozenset()) -> Skill:
    return Skill(name, label, frozenset(lake) | frozenset(direct), frozenset(direct))


SKILLS: dict[str, Skill] = {skill.name: skill for skill in [
    # -- the original five (Studio's skills, orama-design studioAgents.js) ----------------------
    _skill(
        "search", "Search data",
        lake={
            # Lake-wide search across repositories, and the Docs.
            "search", "list_docs", "get_doc",
            # Search indexes: reads are tools (search_workspace_index is the engine's search),
            # creating and exposing one is a proposal.
            "list_indexes", "get_index", "search_index", "search_workspace_index",
            "create_index", "expose_index", "unexpose_index",
            "list_datasets", "get_dataset", "list_connections", "list_source_tables",
        } | SOLUTION_READS,
    ),
    _skill(
        "generate-data", "Generate a dataset",
        lake={"list_datasets", "list_dataset_generators"},
        # The engine admits only gated cases (references pass, negative controls fail).
        direct={"generate_dataset"},
    ),
    _skill(
        "train", "Train a Model",
        lake={
            "list_datasets", "get_dataset", "list_projects", "get_project", "get_pipeline",
            "list_runs", "get_run", "get_run_report", "get_run_metrics", "compare_runs",
            "list_jobs", "get_job", "list_models", "get_model", "list_methodologies",
            # Experiments (the engine's studies). get_experiment is Lake's training experiment.
            "list_experiments", "get_experiment", "list_experiment_trials",
            "get_experiment_analysis", "validate_experiment", "tail_experiment_events",
            "start_experiment", "stop_experiment", "stop_experiment_trial", "promote_trial",
            "agent_training_status",
        } | SOLUTION_READS,
        # The engine's gate and run: check_pipeline changes nothing, and start_run runs only a
        # stored pipeline that passes the gate.
        direct={"check_pipeline", "start_run"},
    ),
    _skill(
        "summarize", "Summarize and explain",
        lake={
            "list_projects", "get_project", "list_runs", "get_run", "get_run_report",
            "list_models", "get_model", "model_health", "model_traffic",
            "workspace_engine_info", "workspace_status", "get_workspace", "list_traces",
            "get_trace",
        } | SOLUTION_READS,
    ),
    _skill(
        "call-api", "Call the API",
        lake={
            "list_models", "get_model", "score", "explain", "predict", "resolve_model",
            "list_indexes", "search_workspace_index", "expose_model", "unexpose_model",
            # A1 approval is a person's: an agent proposes it (docs/skills/README.md).
            "approve_decision",
        } | SOLUTION_READS,
        # The engine's policy decides what a decision allows; its ledger is append-only.
        direct={"decide", "record_outcome"},
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
            "delete_dataset",
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
            "train_model", "retrain_model", "register_model", "load_drive_model",
            "move_model_to_project", "delete_model", "delete_run", "delete_index",
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
        "delete_secret_mount",
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
        "delete_organization", "revoke_grant", "revoke_invitation",
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
        "delete_ask_conversation", "list_agent_revisions",
        # The model gateway is reached through the OpenAI base URL, never as a tool.
        "model_completion",
    }),
    "MCP connections (an agent reaches a connection's tools only through its own mcp:<prefix> skill)": frozenset({
        "list_mcp_connections", "get_mcp_connection", "create_mcp_connection",
        "update_mcp_connection", "delete_mcp_connection", "refresh_mcp_connection",
        "test_mcp_connection", "list_mcp_connection_tools", "list_mcp_connection_calls",
        "call_mcp_tool", "read_mcp_tool", "set_mcp_tool_auto_approve",
        "begin_mcp_connection_sign_in", "complete_mcp_connection_sign_in",
        "create_mcp_connection_agent",
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


def lake_grant(skills: list[str]) -> set[str]:
    """The Lake capabilities the agent's skills grant, never one on the never list."""
    granted: set[str] = set()
    for name in skills:
        skill = SKILLS.get(name)
        if skill is not None:
            granted |= skill.lake
    return granted - never_for_agents()


def direct_grant(skills: list[str]) -> set[str]:
    """The engine actions the agent's skills let the runtime call itself (Lake names)."""
    granted: set[str] = set()
    for name in skills:
        skill = SKILLS.get(name)
        if skill is not None:
            granted |= skill.direct
    return granted - never_for_agents()
