-- Biscuit Database Triggers & Stored Procedures (PostgreSQL 16+)
-- Reference: docs/STORAGE_AND_RETRIEVAL.md

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Semantic edge evidence enforcement
CREATE OR REPLACE FUNCTION enforce_discussion_edge_evidence()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    ev_role evidence_role;
    ev_stance evidence_stance;
    ev_status verification_status;
    ev_author UUID;
    decision_project TEXT;
BEGIN
    IF NEW.edge_type IN ('PROPOSES', 'SUPPORTS', 'OBJECTS', 'REBUTS', 'RESOLVES') THEN
        SELECT es.evidence_role, es.stance, es.verification_status, sm.author_id, st.project_context
          INTO ev_role, ev_stance, ev_status, ev_author, decision_project
          FROM evidence_spans es
          JOIN source_message_versions smv ON smv.id = es.source_message_version_id
          JOIN source_messages sm ON sm.id = smv.message_id
          JOIN decision_alternatives da ON da.id = NEW.target_alternative_id
          JOIN decisions d ON d.id = da.decision_id
          JOIN source_threads st ON st.id = d.source_thread_id
         WHERE es.id = NEW.evidence_span_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Semantic edge needs evidence for a known alternative'; END IF;
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
    ev_role evidence_role;
    ev_status verification_status;
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
    ev_role evidence_role;
    ev_status verification_status;
    ev_alternative UUID;
    ev_author UUID;
    decision_project TEXT;
    actor_role contributor_project_role;
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
