-- Biscuit Database Triggers & Stored Procedures (PostgreSQL 16+)
-- Reference: docs/STORAGE_AND_RETRIEVAL.md

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Semantic edge evidence enforcement
CREATE OR REPLACE FUNCTION enforce_discussion_edge_evidence()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    ev_role TEXT;
    ev_stance TEXT;
    ev_status TEXT;
    ev_author UUID;
    decision_project TEXT;
BEGIN
    IF NEW.edge_type IN ('PROPOSES', 'SUPPORTS', 'OBJECTS', 'REBUTS', 'RESOLVES') THEN
        -- The schema CHECK guarantees NOT NULL targets; this join additionally
        -- binds evidence to the exact alternative, message version, and thread so
        -- a span from alternative A can never authorize an edge on alternative B.
        SELECT es.evidence_role, es.stance, es.verification_status, sm.author_id, st.project_context
          INTO ev_role, ev_stance, ev_status, ev_author, decision_project
          FROM evidence_spans es
          JOIN source_message_versions smv ON smv.id = es.source_message_version_id
          JOIN source_messages sm ON sm.id = smv.message_id
          JOIN decision_alternatives da ON da.id = NEW.target_alternative_id
          JOIN decisions d ON d.id = da.decision_id
          JOIN source_threads st ON st.id = d.source_thread_id
         WHERE es.id = NEW.evidence_span_id
           AND es.alternative_id = NEW.target_alternative_id
           AND es.source_message_version_id = NEW.source_message_version_id
           AND smv.message_id = NEW.from_message_id
           AND sm.thread_id = NEW.source_thread_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Semantic edge needs evidence matching the target alternative, version, and thread'; END IF;
        IF NEW.edge_type = 'PROPOSES' AND ev_role <> 'PROPOSAL' THEN
            RAISE EXCEPTION 'PROPOSES requires PROPOSAL evidence';
        ELSIF NEW.edge_type = 'SUPPORTS' AND ev_stance <> 'SUPPORT' THEN
            RAISE EXCEPTION 'SUPPORTS requires SUPPORT evidence';
        ELSIF NEW.edge_type = 'OBJECTS' AND ev_stance <> 'OBJECT' THEN
            RAISE EXCEPTION 'OBJECTS requires OBJECT evidence';
        ELSIF NEW.edge_type = 'REBUTS' AND ev_role <> 'REBUTTAL' THEN
            RAISE EXCEPTION 'REBUTS requires REBUTTAL evidence';
        ELSIF NEW.edge_type = 'RESOLVES'
          AND (ev_role <> 'RULING' OR ev_status <> 'VERIFIED_EXACT'
               OR NEW.edge_provenance = 'MODEL_INFERRED'
               OR NOT EXISTS (
                   SELECT 1 FROM contributor_project_role_grants g
                    WHERE g.contributor_id = ev_author AND g.project_context = decision_project
                      AND g.role IN ('BDFL', 'Core Maintainer', 'Working Group Chair')
                      AND g.valid_from <= NOW() AND (g.valid_until IS NULL OR g.valid_until > NOW())
                      AND NOT EXISTS (SELECT 1 FROM contributor_project_role_revocations r WHERE r.grant_id = g.id)
               )) THEN
            RAISE EXCEPTION 'RESOLVES needs verified human RULING from this project';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_discussion_edge_evidence ON discussion_edges;
CREATE TRIGGER trg_discussion_edge_evidence
BEFORE INSERT OR UPDATE ON discussion_edges
FOR EACH ROW EXECUTE FUNCTION enforce_discussion_edge_evidence();

-- 2. Ruling signoff enforcement
CREATE OR REPLACE FUNCTION enforce_ruling_signoff()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    ev_role TEXT;
    ev_status TEXT;
    ev_author UUID;
    decision_project TEXT;
BEGIN
    SELECT es.evidence_role, es.verification_status, sm.author_id, st.project_context
      INTO ev_role, ev_status, ev_author, decision_project
      FROM evidence_spans es
      JOIN source_message_versions smv ON smv.id = es.source_message_version_id
      JOIN source_messages sm ON sm.id = smv.message_id
      JOIN decision_alternatives da ON da.id = NEW.alternative_id
      JOIN decisions d ON d.id = da.decision_id
      JOIN source_threads st ON st.id = d.source_thread_id
     WHERE es.id = NEW.evidence_span_id AND es.alternative_id = NEW.alternative_id;
    IF NOT FOUND OR ev_role <> 'RULING' OR ev_status <> 'VERIFIED_EXACT'
       OR ev_author IS DISTINCT FROM NEW.contributor_id THEN
        RAISE EXCEPTION 'Sign-off requires own VERIFIED_EXACT RULING evidence for this alternative';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM contributor_project_role_grants g
         WHERE g.id = NEW.project_role_grant_id AND g.contributor_id = NEW.contributor_id
           AND g.project_context = decision_project
           AND g.role IN ('Core Maintainer', 'Working Group Chair', 'Working Group Member')
           AND g.valid_from <= NOW() AND (g.valid_until IS NULL OR g.valid_until > NOW())
           AND NOT EXISTS (SELECT 1 FROM contributor_project_role_revocations r WHERE r.grant_id = g.id)
    ) THEN
        RAISE EXCEPTION 'Sign-off needs active verified project-specific role grant';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_ruling_signoff ON decision_alternative_ruling_signoffs;
CREATE TRIGGER trg_enforce_ruling_signoff
BEFORE INSERT ON decision_alternative_ruling_signoffs
FOR EACH ROW EXECUTE FUNCTION enforce_ruling_signoff();

-- 3. Authoritative alternative rejection enforcement
CREATE OR REPLACE FUNCTION enforce_authoritative_alternative_rejection()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    ev_role TEXT;
    ev_status TEXT;
    ev_alternative UUID;
    ev_author UUID;
    decision_project TEXT;
    actor_role TEXT;
    signoff_count INT;
BEGIN
    IF NEW.lifecycle_status = 'REJECTED' THEN
        SELECT es.evidence_role, es.verification_status, es.alternative_id,
               sm.author_id, st.project_context
          INTO ev_role, ev_status, ev_alternative, ev_author, decision_project
          FROM evidence_spans es
          JOIN source_message_versions smv ON smv.id = es.source_message_version_id
          JOIN source_messages sm ON sm.id = smv.message_id
          JOIN decisions d ON d.id = NEW.decision_id
          JOIN source_threads st ON st.id = d.source_thread_id
         WHERE es.id = NEW.ruling_evidence_id;
        IF NOT FOUND OR ev_role <> 'RULING' OR ev_status <> 'VERIFIED_EXACT'
           OR ev_alternative IS DISTINCT FROM NEW.id OR ev_author IS DISTINCT FROM NEW.ruling_actor_id THEN
            RAISE EXCEPTION 'REJECTED needs verified RULING by the exact alternative ruling actor';
        END IF;
        SELECT g.role INTO actor_role
          FROM contributor_project_role_grants g
         WHERE g.id = NEW.ruling_actor_grant_id AND g.contributor_id = NEW.ruling_actor_id
           AND g.project_context = decision_project
           AND g.valid_from <= NOW() AND (g.valid_until IS NULL OR g.valid_until > NOW())
           AND NOT EXISTS (SELECT 1 FROM contributor_project_role_revocations r WHERE r.grant_id = g.id);
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Ruling actor lacks active role grant for this decision project';
        END IF;
        IF NEW.ruling_authority = 'BDFL' THEN
            IF actor_role <> 'BDFL' THEN
                RAISE EXCEPTION 'BDFL ruling requires this project''s verified BDFL';
            END IF;
        ELSIF NEW.ruling_authority = 'CORE_MAINTAINER_CONSENSUS' THEN
            IF actor_role <> 'Core Maintainer' THEN
                RAISE EXCEPTION 'Consensus ruling author must be Core Maintainer in this project';
            END IF;
            SELECT COUNT(*) INTO signoff_count
              FROM decision_alternative_ruling_signoffs s
              JOIN contributor_project_role_grants g ON g.id = s.project_role_grant_id
             WHERE s.alternative_id = NEW.id AND g.project_context = decision_project
               AND g.role = 'Core Maintainer' AND g.contributor_id = s.contributor_id
               AND g.valid_from <= NOW() AND (g.valid_until IS NULL OR g.valid_until > NOW())
               AND NOT EXISTS (SELECT 1 FROM contributor_project_role_revocations r WHERE r.grant_id = g.id);
            IF signoff_count < 2 OR NOT EXISTS (
                SELECT 1 FROM decision_alternative_ruling_signoffs s
                WHERE s.alternative_id = NEW.id AND s.contributor_id = NEW.ruling_actor_id
                  AND s.project_role_grant_id = NEW.ruling_actor_grant_id
            ) THEN
                RAISE EXCEPTION 'Consensus requires ruling author plus another verified project Core Maintainer';
            END IF;
        ELSIF NEW.ruling_authority = 'WORKING_GROUP' THEN
            IF actor_role <> 'Working Group Chair' THEN
                RAISE EXCEPTION 'Working-group ruling requires verified Chair for this project';
            END IF;
            SELECT COUNT(*) INTO signoff_count
              FROM decision_alternative_ruling_signoffs s
              JOIN contributor_project_role_grants g ON g.id = s.project_role_grant_id
             WHERE s.alternative_id = NEW.id AND s.contributor_id <> NEW.ruling_actor_id
               AND g.contributor_id = s.contributor_id AND g.project_context = decision_project
               AND g.role = 'Working Group Member'
               AND g.valid_from <= NOW() AND (g.valid_until IS NULL OR g.valid_until > NOW())
               AND NOT EXISTS (SELECT 1 FROM contributor_project_role_revocations r WHERE r.grant_id = g.id);
            IF signoff_count < 1 OR NOT EXISTS (
                SELECT 1 FROM decision_alternative_ruling_signoffs s
                WHERE s.alternative_id = NEW.id AND s.contributor_id = NEW.ruling_actor_id
                  AND s.project_role_grant_id = NEW.ruling_actor_grant_id
            ) THEN
                RAISE EXCEPTION 'Working-group ruling needs Chair and distinct verified Member signoffs';
            END IF;
        ELSE
            RAISE EXCEPTION 'Unknown or model-inferred authority cannot reject an alternative';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_authoritative_alternative_rejection ON decision_alternatives;
CREATE TRIGGER trg_authoritative_alternative_rejection
BEFORE INSERT OR UPDATE OF lifecycle_status, ruling_evidence_id, ruling_authority, ruling_actor_id, ruling_actor_grant_id
ON decision_alternatives
FOR EACH ROW EXECUTE FUNCTION enforce_authoritative_alternative_rejection();

-- 4. Source message version hash verification
CREATE OR REPLACE FUNCTION verify_source_message_version_hash()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF encode(digest(convert_to(NEW.raw_content, 'UTF8'), 'sha256'), 'hex') <> lower(NEW.content_hash) THEN
        RAISE EXCEPTION 'content_hash must be SHA-256 of exact raw_content UTF-8 bytes';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_verify_source_message_version_hash ON source_message_versions;
CREATE TRIGGER trg_verify_source_message_version_hash
BEFORE INSERT OR UPDATE OF raw_content, content_hash ON source_message_versions
FOR EACH ROW EXECUTE FUNCTION verify_source_message_version_hash();

-- 5. Exactly one current message version
CREATE OR REPLACE FUNCTION enforce_exactly_one_current_message_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    target_message_id UUID;
    current_count INT;
BEGIN
    IF TG_TABLE_NAME = 'source_messages' THEN
        target_message_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_message_id := OLD.message_id;
    ELSE
        target_message_id := NEW.message_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM source_messages WHERE id = target_message_id) THEN
        RETURN NULL;
    END IF;
    SELECT COUNT(*) INTO current_count FROM source_message_versions
     WHERE message_id = target_message_id AND is_current = TRUE;
    IF current_count <> 1 THEN
        RAISE EXCEPTION 'Each source message must have exactly one current version at transaction commit';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_source_message_requires_current_version ON source_messages;
CREATE CONSTRAINT TRIGGER trg_source_message_requires_current_version
AFTER INSERT ON source_messages DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_exactly_one_current_message_version();

DROP TRIGGER IF EXISTS trg_source_message_version_current_count ON source_message_versions;
CREATE CONSTRAINT TRIGGER trg_source_message_version_current_count
AFTER INSERT OR UPDATE OR DELETE ON source_message_versions DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_exactly_one_current_message_version();

-- 6. Evidence span exactness verification
CREATE OR REPLACE FUNCTION verify_evidence_span_exactness()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    source_text TEXT;
    source_slice TEXT;
BEGIN
    IF NEW.verification_status = 'VERIFIED_EXACT' THEN
        SELECT raw_content INTO source_text FROM source_message_versions WHERE id = NEW.source_message_version_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Evidence source version does not exist'; END IF;
        IF NEW.start_char < 0 OR NEW.end_char <= NEW.start_char OR NEW.end_char > char_length(source_text) THEN
            RAISE EXCEPTION 'Evidence offsets exceed exact raw_content bounds';
        END IF;
        source_slice := substring(source_text FROM (NEW.start_char + 1) FOR (NEW.end_char - NEW.start_char));
        IF source_slice IS DISTINCT FROM NEW.exact_quote THEN
            RAISE EXCEPTION 'Evidence quote does not equal raw_content[start_char:end_char]';
        END IF;
        IF encode(digest(convert_to(NEW.exact_quote, 'UTF8'), 'sha256'), 'hex') <> lower(NEW.content_hash) THEN
            RAISE EXCEPTION 'Evidence SHA-256 does not match exact_quote UTF-8 bytes';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_verify_evidence_span_exactness ON evidence_spans;
CREATE TRIGGER trg_verify_evidence_span_exactness
BEFORE INSERT OR UPDATE OF verification_status, source_message_version_id, exact_quote, start_char, end_char, content_hash
ON evidence_spans
FOR EACH ROW EXECUTE FUNCTION verify_evidence_span_exactness();

-- 7. Protect evidence span mutation
CREATE OR REPLACE FUNCTION protect_evidence_span_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Evidence spans are retained permanently'; END IF;
    IF (to_jsonb(NEW) - 'verification_status') IS DISTINCT FROM (to_jsonb(OLD) - 'verification_status') THEN
        RAISE EXCEPTION 'Evidence quote, locator, target and provenance are immutable';
    END IF;
    IF OLD.verification_status = 'VERIFIED_EXACT' AND NEW.verification_status <> 'VERIFIED_EXACT' THEN
        RAISE EXCEPTION 'A verified span cannot be downgraded; source versions are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_evidence_span_mutation ON evidence_spans;
CREATE TRIGGER trg_protect_evidence_span_mutation
BEFORE UPDATE OR DELETE ON evidence_spans
FOR EACH ROW EXECUTE FUNCTION protect_evidence_span_mutation();

-- 8. Protect source message version mutation
CREATE OR REPLACE FUNCTION protect_source_message_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Source message versions cannot be deleted'; END IF;
    IF (to_jsonb(NEW) - 'is_current') IS DISTINCT FROM (to_jsonb(OLD) - 'is_current') THEN
        RAISE EXCEPTION 'Source content and provenance are immutable; only is_current may rotate';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_source_message_versions ON source_message_versions;
CREATE TRIGGER trg_protect_source_message_versions
BEFORE UPDATE OR DELETE ON source_message_versions
FOR EACH ROW EXECUTE FUNCTION protect_source_message_version();

-- 9. Append-only audit table protection
CREATE OR REPLACE FUNCTION reject_audit_row_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Audit records are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_project_role_grants_append_only ON contributor_project_role_grants;
CREATE TRIGGER trg_project_role_grants_append_only
BEFORE UPDATE OR DELETE ON contributor_project_role_grants
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

DROP TRIGGER IF EXISTS trg_project_role_revocations_append_only ON contributor_project_role_revocations;
CREATE TRIGGER trg_project_role_revocations_append_only
BEFORE UPDATE OR DELETE ON contributor_project_role_revocations
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

DROP TRIGGER IF EXISTS trg_decision_event_log_append_only ON decision_event_log;
CREATE TRIGGER trg_decision_event_log_append_only
BEFORE UPDATE OR DELETE ON decision_event_log
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

DROP TRIGGER IF EXISTS trg_hitl_approval_events_append_only ON hitl_approval_events;
CREATE TRIGGER trg_hitl_approval_events_append_only
BEFORE UPDATE OR DELETE ON hitl_approval_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

DROP TRIGGER IF EXISTS trg_pr_watchdog_audit_append_only ON pr_watchdog_audit_log;
CREATE TRIGGER trg_pr_watchdog_audit_append_only
BEFORE UPDATE OR DELETE ON pr_watchdog_audit_log
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

DROP TRIGGER IF EXISTS trg_ruling_signoffs_append_only ON decision_alternative_ruling_signoffs;
CREATE TRIGGER trg_ruling_signoffs_append_only
BEFORE UPDATE OR DELETE ON decision_alternative_ruling_signoffs
FOR EACH ROW EXECUTE FUNCTION reject_audit_row_mutation();

-- 10. Atomic audited lifecycle transitions (canonical DDL §15).
-- Audit every lifecycle transition in the same transaction as its state change.
-- The application DB role has NO direct UPDATE/DELETE on decision_alternatives;
-- only the trusted transition service may use the protected transition entrypoint.
CREATE OR REPLACE FUNCTION record_alternative_status_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    acting_contributor UUID;
    explanation TEXT;
    discussion_thread UUID;
    snapshot JSONB := '{}'::jsonb;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.lifecycle_status IS NOT DISTINCT FROM OLD.lifecycle_status THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.lifecycle_status = 'PROPOSED' THEN
        RETURN NEW;
    END IF;
    acting_contributor := NULLIF(current_setting('biscuit.transition_actor_id', TRUE), '')::UUID;
    explanation := NULLIF(trim(current_setting('biscuit.transition_reason', TRUE)), '');
    IF acting_contributor IS NULL OR explanation IS NULL THEN
        RAISE EXCEPTION 'Status changes require authenticated actor and reason in transaction';
    END IF;
    SELECT source_thread_id INTO discussion_thread FROM decisions WHERE id = NEW.decision_id;
    IF NEW.lifecycle_status = 'REJECTED' THEN
        snapshot := jsonb_build_object(
            'ruling_authority', NEW.ruling_authority::text,
            'ruling_actor_id', NEW.ruling_actor_id,
            'ruling_actor_grant_id', NEW.ruling_actor_grant_id,
            'ruling_evidence_id', NEW.ruling_evidence_id,
            'signoffs', COALESCE((
                SELECT jsonb_agg(jsonb_build_object('contributor_id', s.contributor_id,
                    'role_grant_id', s.project_role_grant_id, 'evidence_span_id', s.evidence_span_id)
                    ORDER BY s.contributor_id)
                  FROM decision_alternative_ruling_signoffs s
                 WHERE s.alternative_id = NEW.id
            ), '[]'::jsonb)
        );
    END IF;
    -- Raw PL/pgSQL INSERTs do not invoke Python-side SQLModel default
    -- factories. Supply all non-null values that lack server-side defaults.
    INSERT INTO decision_event_log (
        id, alternative_id, source_thread_id, recurrence_number,
        event_type, previous_status, new_status, reason,
        evidence_span_id, actor_contributor_id, authority_snapshot, recorded_at
    ) VALUES (
        gen_random_uuid(), NEW.id, discussion_thread, 1,
        'STATUS_CHANGE',
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.lifecycle_status ELSE NULL END,
        NEW.lifecycle_status, explanation,
        CASE WHEN NEW.lifecycle_status = 'REJECTED' THEN NEW.ruling_evidence_id ELSE NULL END,
        acting_contributor, snapshot, NOW()
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_alternative_status_audit ON decision_alternatives;
CREATE TRIGGER trg_alternative_status_audit
AFTER INSERT OR UPDATE OF lifecycle_status ON decision_alternatives
FOR EACH ROW EXECUTE FUNCTION record_alternative_status_change();

-- SECURITY BOUNDARY: only a trusted service that has *already authenticated*
-- and bound p_actor_id to the caller may EXECUTE this function. An arbitrary
-- actor UUID supplied through a public API is never accepted as identity proof.
-- In deployment, REVOKE EXECUTE ... FROM PUBLIC, GRANT only to that service, and
-- REVOKE direct lifecycle-table writes from runtime/LLM/MCP/ingestion DB roles.
CREATE OR REPLACE FUNCTION transition_alternative_lifecycle(
    p_alternative_id UUID, p_new_status TEXT, p_actor_id UUID,
    p_reason TEXT, p_ruling_evidence_id UUID DEFAULT NULL,
    p_ruling_authority TEXT DEFAULT NULL,
    p_ruling_actor_id UUID DEFAULT NULL, p_ruling_actor_grant_id UUID DEFAULT NULL
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF p_actor_id IS NULL OR p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'Verified actor and reason required';
    END IF;
    PERFORM set_config('biscuit.transition_actor_id', p_actor_id::text, TRUE);
    PERFORM set_config('biscuit.transition_reason', p_reason, TRUE);
    UPDATE decision_alternatives SET
        lifecycle_status = p_new_status,
        ruling_evidence_id = CASE WHEN p_new_status = 'REJECTED' THEN p_ruling_evidence_id ELSE NULL END,
        ruling_authority = CASE WHEN p_new_status = 'REJECTED' THEN p_ruling_authority ELSE NULL END,
        ruling_actor_id = CASE WHEN p_new_status = 'REJECTED' THEN p_ruling_actor_id ELSE NULL END,
        ruling_actor_grant_id = CASE WHEN p_new_status = 'REJECTED' THEN p_ruling_actor_grant_id ELSE NULL END
    WHERE id = p_alternative_id AND lifecycle_status IS DISTINCT FROM p_new_status;
    IF NOT FOUND THEN RAISE EXCEPTION 'Alternative not found or status unchanged'; END IF;
END;
$$;

-- Privilege boundary for the SECURITY DEFINER transition entrypoint.
-- Only a separately authenticated, trusted transition service should be granted
-- membership in this NOLOGIN group by the deployment administrator. Untrusted
-- runtime/LLM/ingestion database roles MUST NOT be members.
-- Role provisioning is intentionally idempotent for reapplying bootstrap SQL.
DO $transition_roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'biscuit_transition_executor'
    ) THEN
        CREATE ROLE biscuit_transition_executor NOLOGIN;
    END IF;
END;
$transition_roles$;
ALTER ROLE biscuit_transition_executor NOLOGIN;

-- PostgreSQL normally grants function EXECUTE to PUBLIC at creation; revoke
-- that default so SECURITY DEFINER cannot be called by arbitrary DB users.
REVOKE ALL ON FUNCTION public.transition_alternative_lifecycle(
    UUID, TEXT, UUID, TEXT, UUID, TEXT, UUID, UUID
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.transition_alternative_lifecycle(
    UUID, TEXT, UUID, TEXT, UUID, TEXT, UUID, UUID
) TO biscuit_transition_executor;

-- Never let PUBLIC bypass the audited entrypoint with a direct table UPDATE.
-- Deployments must also ensure no application/runtime role has a direct grant,
-- and must never connect application traffic with the schema-owner identity.
REVOKE UPDATE, DELETE ON TABLE public.decision_alternatives FROM PUBLIC;

-- 11. Canonical HNSW retrieval index (STORAGE_AND_RETRIEVAL.md §4).
-- Idempotent; safe to re-run on every bootstrap. Postgres-only.
CREATE INDEX IF NOT EXISTS idx_alternatives_embedding
ON decision_alternatives
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
