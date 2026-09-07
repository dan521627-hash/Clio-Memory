"""Versioned SQLite schema for the isolated nursery database."""

from __future__ import annotations

import hashlib


SCHEMA_VERSION = 11

MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE nursery_modules (
        account_id TEXT PRIMARY KEY,
        child_id TEXT UNIQUE,
        module_state TEXT NOT NULL CHECK(module_state IN (
            'never_enabled', 'draft', 'active', 'paused', 'deletion_pending'
        )),
        state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
        pre_delete_state TEXT CHECK(pre_delete_state IS NULL OR pre_delete_state IN (
            'active', 'paused'
        )),
        recycle_deadline TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE nursery_children (
        child_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE,
        stage_id TEXT NOT NULL DEFAULT 'infancy',
        state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_caregivers (
        caregiver_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN (
            'user_guardian', 'external_ai_guardian', 'companion', 'system_event'
        )),
        permission_status TEXT NOT NULL DEFAULT 'active'
            CHECK(permission_status IN ('active', 'disabled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(account_id, caregiver_id)
    )
    """,
    """
    CREATE TABLE nursery_operations (
        sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        expected_state_version INTEGER,
        before_state_version INTEGER,
        status TEXT NOT NULL CHECK(status IN (
            'pending', 'processing', 'completed', 'failed_retryable',
            'rejected', 'cancelled'
        )),
        result_json TEXT,
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        lease_owner TEXT,
        lease_expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT,
        UNIQUE(account_id, child_id, caregiver_id, idempotency_key)
    )
    """,
    """
    CREATE INDEX idx_nursery_operations_fifo
    ON nursery_operations(child_id, sequence_no)
    """,
    """
    CREATE INDEX idx_nursery_operations_status
    ON nursery_operations(status, lease_expires_at)
    """,
    """
    CREATE TABLE nursery_pause_markers (
        marker_id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        added_at TEXT NOT NULL,
        released_at TEXT,
        source_operation_id TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX uq_nursery_active_pause_marker
    ON nursery_pause_markers(child_id, caregiver_id)
    WHERE released_at IS NULL
    """,
    """
    CREATE TABLE nursery_confirmations (
        confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        subject_version TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        confirmation_status TEXT NOT NULL
            CHECK(confirmation_status IN ('confirmed', 'withdrawn')),
        source_operation_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(subject_type, subject_id, subject_version, caregiver_id)
    )
    """,
    """
    CREATE TABLE nursery_state_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('before', 'after')),
        state_version INTEGER NOT NULL CHECK(state_version >= 0),
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(operation_id, snapshot_kind)
    )
    """,
    """
    CREATE TABLE nursery_source_events (
        source_event_key INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        specification_ref TEXT NOT NULL DEFAULT '',
        processing_status TEXT NOT NULL CHECK(processing_status IN (
            'accepted', 'applied', 'rejected', 'revoked', 'superseded'
        )),
        revoked_at TEXT,
        superseded_by_key INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(superseded_by_key) REFERENCES nursery_source_events(source_event_key),
        UNIQUE(source_type, source_id, source_version)
    )
    """,
    """
    CREATE TABLE nursery_health_evaluations (
        evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluation_key TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        operation_id TEXT,
        rule_version TEXT NOT NULL,
        trigger_type TEXT NOT NULL,
        condition_hash TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        child_state_version INTEGER NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN (
            'no_change', 'observe', 'mild_illness_candidate',
            'mild_discomfort_candidate', 'minor_injury_candidate',
            'care', 'improving', 'recovered'
        )),
        replayed INTEGER NOT NULL DEFAULT 0 CHECK(replayed IN (0, 1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX idx_nursery_health_child_time
    ON nursery_health_evaluations(child_id, created_at)
    """,
    """
    CREATE TABLE nursery_safety_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        account_id TEXT NOT NULL,
        child_id TEXT,
        caregiver_id TEXT,
        operation_id TEXT,
        error_code TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
)


MIGRATION_2_STATEMENTS = (
    """
    CREATE TABLE nursery_creation_drafts (
        child_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE,
        draft_version INTEGER NOT NULL DEFAULT 1 CHECK(draft_version >= 1),
        child_kind TEXT NOT NULL DEFAULT 'human' CHECK(child_kind = 'human'),
        sex_status TEXT NOT NULL DEFAULT 'undecided' CHECK(sex_status IN (
            'boy', 'girl', 'neutral', 'undecided'
        )),
        stage_id TEXT NOT NULL DEFAULT 'infancy',
        selected_candidate_id TEXT,
        official_name TEXT,
        nickname TEXT,
        address_terms_json TEXT NOT NULL DEFAULT '{}',
        independent_temperament_json TEXT NOT NULL,
        temperament_formula_version TEXT NOT NULL,
        initial_space_json TEXT NOT NULL DEFAULT '{}',
        initial_space_completed INTEGER NOT NULL DEFAULT 0
            CHECK(initial_space_completed IN (0, 1)),
        draft_status TEXT NOT NULL DEFAULT 'open'
            CHECK(draft_status IN ('open', 'sealed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sealed_at TEXT,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_caregiver_profiles (
        caregiver_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        founding_guardian INTEGER NOT NULL DEFAULT 0
            CHECK(founding_guardian IN (0, 1)),
        declared_initial_style_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_name_candidates (
        candidate_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 3),
        proposed_name TEXT NOT NULL,
        meaning_text TEXT NOT NULL DEFAULT '',
        sound_notes TEXT NOT NULL DEFAULT '',
        avoid_notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(child_id, caregiver_id, ordinal)
    )
    """,
    """
    CREATE INDEX idx_nursery_name_candidates_child
    ON nursery_name_candidates(child_id, caregiver_id, ordinal)
    """,
    """
    CREATE TABLE nursery_name_preferences (
        child_id TEXT NOT NULL,
        reviewer_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        preference TEXT NOT NULL CHECK(preference IN (
            'like', 'acceptable', 'reject'
        )),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(child_id, reviewer_id, candidate_id),
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(reviewer_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(candidate_id) REFERENCES nursery_name_candidates(candidate_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_private_submissions (
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        questionnaire_kind TEXT NOT NULL CHECK(questionnaire_kind IN (
            'temperament', 'initial_style'
        )),
        questionnaire_version TEXT NOT NULL,
        answer_digest TEXT NOT NULL,
        private_ref TEXT NOT NULL,
        normalized_json TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(child_id, caregiver_id, questionnaire_kind),
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_nursery_private_submissions_child
    ON nursery_private_submissions(child_id, questionnaire_kind)
    """,
    """
    CREATE TABLE nursery_temperament_profiles (
        child_id TEXT PRIMARY KEY,
        formula_version TEXT NOT NULL,
        final_vector_json TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        calculated_at TEXT NOT NULL,
        sealed_at TEXT,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_child_identity (
        child_id TEXT PRIMARY KEY,
        child_kind TEXT NOT NULL CHECK(child_kind = 'human'),
        sex_status TEXT NOT NULL CHECK(sex_status IN (
            'boy', 'girl', 'neutral', 'undecided'
        )),
        official_name TEXT NOT NULL,
        nickname TEXT,
        address_terms_json TEXT NOT NULL DEFAULT '{}',
        temperament_json TEXT NOT NULL,
        temperament_formula_version TEXT NOT NULL,
        locked_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_name_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        child_id TEXT NOT NULL,
        name_kind TEXT NOT NULL CHECK(name_kind IN ('official', 'nickname')),
        name_value TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT,
        reason_code TEXT NOT NULL DEFAULT 'initial_creation',
        source_operation_id TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_name_history_child
    ON nursery_name_history(child_id, valid_from)
    """,
    """
    CREATE TABLE nursery_model_configs (
        account_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        base_url TEXT NOT NULL,
        model_name TEXT NOT NULL,
        credential_ref TEXT NOT NULL,
        credential_suffix TEXT NOT NULL,
        connection_status TEXT NOT NULL CHECK(connection_status IN (
            'ready', 'invalid', 'deleted'
        )),
        capabilities_json TEXT NOT NULL,
        config_version INTEGER NOT NULL CHECK(config_version >= 1),
        tested_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_relationships (
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        trust REAL NOT NULL CHECK(trust BETWEEN 0 AND 1),
        familiarity REAL NOT NULL CHECK(familiarity BETWEEN 0 AND 1),
        closeness REAL NOT NULL CHECK(closeness BETWEEN 0 AND 1),
        repair REAL NOT NULL CHECK(repair BETWEEN 0 AND 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(child_id, caregiver_id),
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_lifecycle_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('child_created')),
        operation_id TEXT NOT NULL UNIQUE,
        event_json TEXT NOT NULL,
        sync_status TEXT NOT NULL DEFAULT 'pending'
            CHECK(sync_status IN ('pending', 'synced', 'failed_retryable')),
        created_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_lifecycle_child_time
    ON nursery_lifecycle_events(child_id, created_at)
    """,
)


MIGRATION_3_STATEMENTS = (
    """
    CREATE TABLE nursery_child_runtime_state (
        child_id TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        state_updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_child_short_events (
        event_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        caregiver_id TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK(event_kind = 'interaction'),
        caregiver_message TEXT NOT NULL,
        child_reply TEXT NOT NULL,
        intent TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE RESTRICT,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_short_events_expiry
    ON nursery_child_short_events(expires_at, child_id)
    """,
)


MIGRATION_4_STATEMENTS = (
    """
    CREATE TABLE nursery_anima_child_contexts (
        context_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        source_key TEXT NOT NULL,
        source_version TEXT NOT NULL,
        category TEXT NOT NULL,
        summary TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        disposition_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(child_id, source_key, source_version)
    )
    """,
    """
    CREATE INDEX idx_nursery_anima_context_expiry
    ON nursery_anima_child_contexts(expires_at, child_id)
    """,
)


MIGRATION_5_STATEMENTS = (
    """
    CREATE TABLE nursery_external_mcp_grants (
        grant_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        permissions_json TEXT NOT NULL,
        issued_by_caregiver_id TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        FOREIGN KEY(account_id) REFERENCES nursery_modules(account_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT,
        FOREIGN KEY(issued_by_caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_external_mcp_grants_lookup
    ON nursery_external_mcp_grants(grant_id, expires_at, revoked_at)
    """,
)


MIGRATION_6_STATEMENTS = (
    """
    CREATE TABLE nursery_body_clocks (
        child_id TEXT PRIMARY KEY,
        last_fed_at TEXT,
        last_hydrated_at TEXT,
        sleep_started_at TEXT,
        last_woke_at TEXT,
        last_care_at TEXT,
        settled_through TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        frozen_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_conversations (
        conversation_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'private'
            CHECK(visibility IN ('private', 'required_shared')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_conversation_turns (
        turn_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        initiator_source TEXT NOT NULL CHECK(initiator_source IN ('user', 'external_ai')),
        caregiver_text TEXT NOT NULL,
        child_response_json TEXT NOT NULL,
        sharing_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES nursery_conversations(conversation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_nursery_conversation_turns_recent
    ON nursery_conversation_turns(conversation_id, created_at DESC)
    """,
    """
    CREATE TABLE nursery_pending_decisions (
        decision_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        proposed_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'needs_clarification', 'pending_confirmation', 'resolved', 'cancelled'
        )),
        created_by TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(created_by) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_child_presentations (
        child_id TEXT PRIMARY KEY,
        scene_key TEXT,
        character_variant TEXT,
        time_tone TEXT,
        scene_summary TEXT,
        suggestions_json TEXT NOT NULL DEFAULT '[]',
        first_arrival_event_id TEXT UNIQUE,
        first_arrival_status TEXT NOT NULL DEFAULT 'none'
            CHECK(first_arrival_status IN ('none', 'pending', 'ready')),
        first_arrival_reaction TEXT,
        presentation_version TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_rooms (
        room_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        atmosphere TEXT,
        facts_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_areas (
        area_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        category TEXT NOT NULL,
        label TEXT NOT NULL,
        description TEXT,
        facts_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(room_id) REFERENCES nursery_rooms(room_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_items (
        item_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        area_id TEXT,
        name TEXT NOT NULL,
        emoji TEXT,
        description TEXT,
        current_state TEXT,
        facts_json TEXT NOT NULL DEFAULT '{}',
        source_caregiver_id TEXT NOT NULL,
        last_interacted_at TEXT,
        removed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(area_id) REFERENCES nursery_areas(area_id)
            ON UPDATE CASCADE ON DELETE SET NULL,
        FOREIGN KEY(source_caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_items_child_active
    ON nursery_items(child_id, removed_at, last_interacted_at DESC)
    """,
    """
    CREATE TABLE nursery_experiences (
        experience_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN (
            'family', 'offsite', 'first_experience', 'growth', 'health', 'safety'
        )),
        occurred_at TEXT NOT NULL,
        age_appropriate_summary TEXT NOT NULL,
        child_reaction TEXT NOT NULL DEFAULT '',
        resolution TEXT NOT NULL DEFAULT 'unresolved' CHECK(resolution IN (
            'unresolved', 'soothed', 'repaired', 'recovered', 'passed', 'corrected'
        )),
        requires_attention INTEGER NOT NULL DEFAULT 0 CHECK(requires_attention IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_nursery_experiences_timeline
    ON nursery_experiences(child_id, occurred_at DESC, experience_id DESC)
    """,
    """
    CREATE TABLE nursery_experience_sources (
        experience_id TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        source_validity TEXT NOT NULL DEFAULT 'valid'
            CHECK(source_validity IN ('valid', 'modified', 'merged', 'revoked')),
        PRIMARY KEY(experience_id, source_type, source_id, source_version),
        FOREIGN KEY(experience_id) REFERENCES nursery_experiences(experience_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_experience_ripples (
        experience_id TEXT PRIMARY KEY,
        current_ripple TEXT,
        active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
        recalculation_version TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(experience_id) REFERENCES nursery_experiences(experience_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_growth_observations (
        observation_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        ability_ids_json TEXT NOT NULL,
        observed_behavior TEXT NOT NULL,
        assistance TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        valid INTEGER NOT NULL DEFAULT 1 CHECK(valid IN (0, 1)),
        observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(child_id, caregiver_id, session_id, observed_behavior)
    )
    """,
    """
    CREATE TABLE nursery_proposals (
        proposal_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        proposal_type TEXT NOT NULL CHECK(proposal_type IN ('stage', 'name', 'delete')),
        proposal_version INTEGER NOT NULL CHECK(proposal_version >= 1),
        proposed_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open', 'confirmed', 'withdrawn', 'cancelled')),
        proposed_by TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(proposed_by) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_nursery_proposals_open
    ON nursery_proposals(child_id, proposal_type, status, updated_at DESC)
    """,
    """
    CREATE TABLE nursery_health_events (
        health_event_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'monitoring', 'caring', 'improving', 'recovered', 'corrected'
        )),
        non_diagnostic_summary TEXT NOT NULL,
        observed_effects_json TEXT NOT NULL DEFAULT '{}',
        condition_summary_json TEXT NOT NULL DEFAULT '[]',
        started_at TEXT NOT NULL,
        recovered_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_health_observations (
        observation_id TEXT PRIMARY KEY,
        health_event_id TEXT,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        observed_behavior TEXT NOT NULL,
        context_summary TEXT,
        observed_at TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(health_event_id) REFERENCES nursery_health_events(health_event_id)
            ON UPDATE CASCADE ON DELETE SET NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nursery_care_plans (
        care_plan_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        health_event_id TEXT NOT NULL,
        plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
        user_confirmed_summary TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'withdrawn')),
        confirmed_by_user_at TEXT,
        source_operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(health_event_id) REFERENCES nursery_health_events(health_event_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(care_plan_id, plan_version)
    )
    """,
    """
    CREATE TABLE nursery_care_executions (
        execution_id TEXT PRIMARY KEY,
        care_plan_id TEXT NOT NULL,
        care_plan_version INTEGER NOT NULL,
        health_event_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        executed_at TEXT NOT NULL,
        FOREIGN KEY(care_plan_id) REFERENCES nursery_care_plans(care_plan_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(health_event_id) REFERENCES nursery_health_events(health_event_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(care_plan_id, care_plan_version, health_event_id)
    )
    """,
    """
    CREATE TABLE nursery_linkage_preferences (
        child_id TEXT PRIMARY KEY,
        child_snapshot_reference_authorized INTEGER NOT NULL DEFAULT 0
            CHECK(child_snapshot_reference_authorized IN (0, 1)),
        updated_by_caregiver_id TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(updated_by_caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE SET NULL
    )
    """,
)


MIGRATION_7_STATEMENTS = (
    """
    CREATE TABLE nursery_entry_preferences (
        account_id TEXT PRIMARY KEY,
        preference TEXT NOT NULL CHECK(preference IN ('visible', 'hidden')),
        updated_at TEXT NOT NULL
    )
    """,
)


# Care plans are immutable versions.  The original stage-6 primary key made a
# second confirmed version of the same user plan impossible, so rebuild the
# two small dependent tables rather than overwriting the original agreement.
MIGRATION_8_STATEMENTS = (
    """
    CREATE TABLE nursery_care_plans_v2 (
        care_plan_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        health_event_id TEXT NOT NULL,
        plan_version INTEGER NOT NULL CHECK(plan_version >= 1),
        user_confirmed_summary TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'withdrawn')),
        confirmed_by_user_at TEXT,
        source_operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(care_plan_id, plan_version),
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(health_event_id) REFERENCES nursery_health_events(health_event_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO nursery_care_plans_v2 (
        care_plan_id, child_id, health_event_id, plan_version,
        user_confirmed_summary, status, confirmed_by_user_at,
        source_operation_id, created_at, updated_at
    )
    SELECT care_plan_id, child_id, health_event_id, plan_version,
           user_confirmed_summary, status, confirmed_by_user_at,
           source_operation_id, created_at, updated_at
    FROM nursery_care_plans
    """,
    """
    CREATE TABLE nursery_care_executions_v2 (
        execution_id TEXT PRIMARY KEY,
        care_plan_id TEXT NOT NULL,
        care_plan_version INTEGER NOT NULL,
        health_event_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        caregiver_id TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        executed_at TEXT NOT NULL,
        FOREIGN KEY(care_plan_id, care_plan_version)
            REFERENCES nursery_care_plans_v2(care_plan_id, plan_version)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(health_event_id) REFERENCES nursery_health_events(health_event_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        UNIQUE(care_plan_id, care_plan_version, health_event_id)
    )
    """,
    """
    INSERT INTO nursery_care_executions_v2 (
        execution_id, care_plan_id, care_plan_version, health_event_id,
        child_id, caregiver_id, source_operation_id, executed_at
    )
    SELECT execution_id, care_plan_id, care_plan_version, health_event_id,
           child_id, caregiver_id, source_operation_id, executed_at
    FROM nursery_care_executions
    """,
    "DROP TABLE nursery_care_executions",
    "DROP TABLE nursery_care_plans",
    "ALTER TABLE nursery_care_plans_v2 RENAME TO nursery_care_plans",
    "ALTER TABLE nursery_care_executions_v2 RENAME TO nursery_care_executions",
    """
    CREATE INDEX idx_nursery_care_plans_current
    ON nursery_care_plans(child_id, health_event_id, status, plan_version DESC)
    """,
)


# Family-event context originally had no validity marker.  Keep the existing
# child-safe envelope and add only the provenance needed to exclude a revised
# or deleted source from future conversations.
MIGRATION_9_STATEMENTS = (
    """
    ALTER TABLE nursery_anima_child_contexts
    ADD COLUMN source_validity TEXT NOT NULL DEFAULT 'valid'
        CHECK(source_validity IN ('valid', 'superseded', 'revoked'))
    """,
)


# Short-lived conversations need a channel so a child never confuses a human
# parent with an external AI caregiver.  Durable care facts live separately:
# they retain the fact and its state trace, not the 30-day raw dialogue.
MIGRATION_10_STATEMENTS = (
    """
    ALTER TABLE nursery_child_short_events
    ADD COLUMN conversation_channel TEXT NOT NULL DEFAULT 'legacy'
        CHECK(conversation_channel IN ('legacy', 'user_child', 'external_ai_child', 'shared_family'))
    """,
    """
    UPDATE nursery_caregiver_profiles
    SET display_name='外部 AI 养育者'
    WHERE caregiver_id='anima-external-ai-guardian'
      AND display_name='Anima 共同养育 AI'
    """,
    """
    CREATE TABLE nursery_shared_care_events (
        care_event_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE,
        caregiver_id TEXT NOT NULL,
        conversation_channel TEXT NOT NULL
            CHECK(conversation_channel IN ('user_child', 'external_ai_child', 'shared_family')),
        category TEXT NOT NULL,
        object_name TEXT NOT NULL,
        summary TEXT NOT NULL,
        world_item_id TEXT,
        state_before_json TEXT NOT NULL,
        state_after_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id)
            ON UPDATE CASCADE ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_nursery_shared_care_events_child_time
    ON nursery_shared_care_events(child_id, created_at DESC, care_event_id DESC)
    """,
)


MIGRATION_11_STATEMENTS = (
    """ALTER TABLE nursery_shared_care_events ADD COLUMN clock_before_json TEXT NOT NULL DEFAULT '{}'""",
    """ALTER TABLE nursery_shared_care_events ADD COLUMN correction_json TEXT""",
    """CREATE TABLE nursery_family_threads (
        thread_id TEXT PRIMARY KEY, child_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('activity','promise','handoff')),
        title TEXT NOT NULL, summary TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','paused','discuss','completed','cancelled')),
        version INTEGER NOT NULL CHECK(version>0), created_by TEXT NOT NULL,
        updated_by TEXT NOT NULL, item_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id) ON DELETE CASCADE,
        FOREIGN KEY(created_by) REFERENCES nursery_caregivers(caregiver_id),
        FOREIGN KEY(updated_by) REFERENCES nursery_caregivers(caregiver_id)
    )""",
    """CREATE TABLE nursery_family_events (
        event_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, thread_id TEXT,
        caregiver_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        source_operation_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        FOREIGN KEY(child_id) REFERENCES nursery_children(child_id) ON DELETE CASCADE,
        FOREIGN KEY(thread_id) REFERENCES nursery_family_threads(thread_id) ON DELETE CASCADE,
        FOREIGN KEY(caregiver_id) REFERENCES nursery_caregivers(caregiver_id),
        FOREIGN KEY(source_operation_id) REFERENCES nursery_operations(operation_id) ON DELETE CASCADE
    )""",
    """CREATE INDEX idx_nursery_family_child_time ON nursery_family_threads(child_id,updated_at DESC)""",
    """CREATE INDEX idx_nursery_family_events_time ON nursery_family_events(child_id,created_at DESC)""",
)
MIGRATION_11_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_11_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_1_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_1_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_2_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_2_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_3_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_3_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_4_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_4_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_5_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_5_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_6_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_6_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_7_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_7_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_8_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_8_STATEMENTS).encode("utf-8")
).hexdigest()

MIGRATION_9_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_9_STATEMENTS).encode("utf-8")
).hexdigest()
MIGRATION_10_CHECKSUM = hashlib.sha256(
    "\n".join(statement.strip() for statement in MIGRATION_10_STATEMENTS).encode("utf-8")
).hexdigest()


EXPECTED_TABLES = frozenset(
    {
        "nursery_schema_migrations",
        "nursery_modules",
        "nursery_children",
        "nursery_caregivers",
        "nursery_pause_markers",
        "nursery_confirmations",
        "nursery_operations",
        "nursery_state_snapshots",
        "nursery_source_events",
        "nursery_health_evaluations",
        "nursery_safety_audit",
        "nursery_creation_drafts",
        "nursery_caregiver_profiles",
        "nursery_name_candidates",
        "nursery_name_preferences",
        "nursery_private_submissions",
        "nursery_temperament_profiles",
        "nursery_child_identity",
        "nursery_name_history",
        "nursery_model_configs",
        "nursery_relationships",
        "nursery_lifecycle_events",
        "nursery_child_runtime_state",
        "nursery_child_short_events",
        "nursery_anima_child_contexts",
        "nursery_external_mcp_grants",
        "nursery_body_clocks",
        "nursery_conversations",
        "nursery_conversation_turns",
        "nursery_pending_decisions",
        "nursery_child_presentations",
        "nursery_rooms",
        "nursery_areas",
        "nursery_items",
        "nursery_experiences",
        "nursery_experience_sources",
        "nursery_experience_ripples",
        "nursery_growth_observations",
        "nursery_proposals",
        "nursery_health_events",
        "nursery_health_observations",
        "nursery_care_plans",
        "nursery_care_executions",
        "nursery_linkage_preferences",
        "nursery_entry_preferences",
        "nursery_shared_care_events",
        "nursery_family_threads",
        "nursery_family_events",
    }
)

EXPECTED_INDEXES = frozenset(
    {
        "idx_nursery_operations_fifo",
        "idx_nursery_operations_status",
        "uq_nursery_active_pause_marker",
        "idx_nursery_health_child_time",
        "idx_nursery_name_candidates_child",
        "idx_nursery_private_submissions_child",
        "idx_nursery_lifecycle_child_time",
        "idx_nursery_name_history_child",
        "idx_nursery_short_events_expiry",
        "idx_nursery_anima_context_expiry",
        "idx_nursery_external_mcp_grants_lookup",
        "idx_nursery_conversation_turns_recent",
        "idx_nursery_items_child_active",
        "idx_nursery_experiences_timeline",
        "idx_nursery_proposals_open",
        "idx_nursery_care_plans_current",
        "idx_nursery_shared_care_events_child_time",
        "idx_nursery_family_child_time",
        "idx_nursery_family_events_time",
    }
)
