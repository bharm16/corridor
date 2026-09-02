--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: block_held_intermediary_delete(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.block_held_intermediary_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if exists (
                select 1 from retention_holds
                where project_id = old.project_id and lifted_at is null
            ) then
                raise exception 'active retention hold blocks intermediary deletion'
                    using errcode = '23514';
            end if;
            return old;
        end;
        $$;


ALTER FUNCTION public.block_held_intermediary_delete() OWNER TO corridor;

--
-- Name: create_initial_dependency_event_scope_decision(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.create_initial_dependency_event_scope_decision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if not is_attributable_statement_scope_actor(new.created_by) then
                raise exception 'Commitment Scope decision needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            insert into dependency_event_scope_decisions
                (event_id, scope_mode, decided_by)
            values (new.id, new.scope_mode, new.created_by);
            return new;
        end;
        $$;


ALTER FUNCTION public.create_initial_dependency_event_scope_decision() OWNER TO corridor;

--
-- Name: enforce_active_run_declaration(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_active_run_declaration() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            demo_document boolean;
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            -- The one sanctioned escape, identical to Candidate lineage:
            -- the isolated demo project resets itself, and only itself.
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from documents
                      join projects on projects.id = documents.project_id
                     where documents.id = old.document_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_document;
                if demo_document then
                    return old;
                end if;
            end if;
            raise exception 'Active Run declarations are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_active_run_declaration() OWNER TO corridor;

--
-- Name: enforce_assignment_dispatch_identity(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_assignment_dispatch_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'assignment notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'assignment notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;


ALTER FUNCTION public.enforce_assignment_dispatch_identity() OWNER TO corridor;

--
-- Name: enforce_assignment_notification_immutable(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_assignment_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification occurrences are immutable';
        end
        $$;


ALTER FUNCTION public.enforce_assignment_notification_immutable() OWNER TO corridor;

--
-- Name: enforce_automatic_carry_forward_outcome(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_automatic_carry_forward_outcome() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from policy_runs run
                          join dependencies dependency
                            on dependency.id = new.dependency_id
                          left join revision_comparison_runs comparison
                            on comparison.id = new.comparison_id
                          left join revision_comparison_findings finding
                            on finding.id = new.finding_id
                          left join candidates predecessor
                            on predecessor.id = new.predecessor_candidate_id
                          left join candidates successor
                            on successor.id = new.successor_candidate_id
                          left join automatic_carry_forward_receipts receipt
                            on receipt.audit_log_id = new.receipt_audit_log_id
                         where run.id = new.run_id
                           and run.family = 'automatic-carry-forward'
                           and run.project_id = new.project_id
                           and run.policy_approval_id is null
                           and new.policy_approval_id is null
                           and dependency.project_id = new.project_id
                           and (
                               new.comparison_id is null
                               or comparison.project_id = new.project_id
                           )
                           and (
                               new.finding_id is null
                               or (
                                   new.comparison_id is not null
                                   and finding.revision_comparison_run_id =
                                       new.comparison_id
                               )
                           )
                           and (
                               new.predecessor_candidate_id is null
                               or predecessor.project_id = new.project_id
                           )
                           and (
                               new.successor_candidate_id is null
                               or successor.project_id = new.project_id
                           )
                           and (
                               (
                                   new.outcome = 'carried'
                                   and new.receipt_audit_log_id is not null
                                   and receipt.project_id = new.project_id
                                   and receipt.policy_approval_id is null
                                   and receipt.policy_version = run.policy_version
                                   and receipt.policy_sha256 = run.policy_sha256
                                   and receipt.dependency_id = new.dependency_id
                                   and receipt.comparison_id is not distinct from
                                       new.comparison_id
                                   and receipt.finding_id is not distinct from
                                       new.finding_id
                                   and receipt.predecessor_candidate_id is not distinct from
                                       new.predecessor_candidate_id
                                   and receipt.successor_candidate_id is not distinct from
                                       new.successor_candidate_id
                               )
                               or (
                                   new.outcome = 'abstained'
                                   and new.receipt_audit_log_id is null
                                   and new.reason = any (
                                       array[
                                           'comparison_policy_unapproved',
                                           'review_stale',
                                           'dependency_unavailable',
                                           'comparison_not_one_to_one',
                                           'comparison_integrity_failure',
                                           'predecessor_finding_unavailable',
                                           'comparison_changed',
                                           'comparison_dropped',
                                           'comparison_ambiguous',
                                           'comparison_unmatched',
                                           'comparison_input_unavailable',
                                           'successor_fields_not_exact',
                                           'successor_not_actionable',
                                           'successor_candidate_changed',
                                           'successor_provenance_unsafe',
                                           'unsupported_operative_support_role',
                                           'readiness_source_changed',
                                           'readiness_history_untrusted',
                                           'supersession_registry_unavailable',
                                           'multi_hop_supersession',
                                           'successor_active_run_invalid',
                                           'predecessor_active_run_unavailable',
                                           'multiple_exact_comparisons',
                                           'admission_scope_identity_mismatch',
                                           'partial_scope_transfer',
                                           'support_scope_changed',
                                           'predecessor_support_provenance_unsafe',
                                           'admission_run_mismatch',
                                           'admission_fields_changed',
                                           'admission_history_corrupt',
                                           'admission_link_unavailable',
                                           'admission_link_ambiguous',
                                           'admission_document_mismatch',
                                           'admission_project_mismatch',
                                           'admission_not_attributable',
                                           'admission_state_inconsistent',
                                           'reconfirmation_history_corrupt',
                                           'reconfirmation_lineage_cycle',
                                           'reconfirmation_lineage_chronology_invalid',
                                           'reconfirmation_identity_mismatch',
                                           'reconfirmation_lineage_mismatch',
                                           'reconfirmation_lineage_changed',
                                           'reconfirmation_candidate_changed',
                                           'reconfirmation_provenance_unsafe',
                                           'reconfirmation_evidence_mismatch',
                                           'successor_candidate_already_reconfirmed',
                                           'successor_candidate_link_ambiguous',
                                           'awaiting_extraction',
                                           'extraction_failed',
                                           'awaiting_active_run',
                                           'awaiting_comparison',
                                           'comparison_selection_ambiguous',
                                           'comparison_ready',
                                           'blocked',
                                           'unclassified_unsafe'
                                       ]::text[]
                                   )
                                   and new.reason_version =
                                       'automatic-carry-forward-abstentions-v1'
                               )
                           )
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Automatic Carry-Forward outcome binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Automatic Carry-Forward outcomes are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_automatic_carry_forward_outcome() OWNER TO corridor;

--
-- Name: enforce_automatic_carry_forward_receipt(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_automatic_carry_forward_receipt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from audit_log act
                          join dependencies dependency
                            on dependency.id = new.dependency_id
                          join revision_comparison_runs comparison
                            on comparison.id = new.comparison_id
                           and comparison.project_id = new.project_id
                          join revision_comparison_findings finding
                            on finding.id = new.finding_id
                           and finding.revision_comparison_run_id = comparison.id
                          join candidates predecessor
                            on predecessor.id = new.predecessor_candidate_id
                           and predecessor.project_id = new.project_id
                          join candidates successor
                            on successor.id = new.successor_candidate_id
                           and successor.project_id = new.project_id
                          join evidence_links evidence
                            on evidence.id = new.new_evidence_link_id
                           and evidence.dependency_id = new.dependency_id
                          join audit_log admission
                            on admission.id = new.origin_admission_audit_id
                          left join audit_log prior_transfer
                            on prior_transfer.id =
                               new.predecessor_support_transfer_audit_id
                         where act.id = new.audit_log_id
                           and dependency.project_id = new.project_id
                           and act.action = 'automatic_carry_forward'
                           and act.entity_type = 'dependency'
                           and act.entity_id = new.dependency_id
                           and act.actor = 'corridor:automatic-carry-forward'
                           and act.human_principal is null
                           and act.before_json = new.before_json
                           and act.after_json = new.after_json
                           and comparison.predecessor_document_id =
                               predecessor.source_document_id
                           and comparison.successor_document_id =
                               successor.source_document_id
                           and comparison.predecessor_extraction_run_id =
                               predecessor.extraction_run_id
                           and comparison.successor_extraction_run_id =
                               successor.extraction_run_id
                           and new.predecessor_candidate_id = any(
                               finding.predecessor_candidate_ids
                           )
                           and new.successor_candidate_id = any(
                               finding.successor_candidate_ids
                           )
                           and finding.state = 'unchanged'
                           and finding.predecessor_candidate_ids =
                               array[new.predecessor_candidate_id]::bigint[]
                           and finding.successor_candidate_ids =
                               array[new.successor_candidate_id]::bigint[]
                           and evidence.document_id = successor.source_document_id
                           and evidence.verified
                           and successor.citations_verified
                           and jsonb_typeof(
                               successor.payload_json -> 'citations'
                           ) = 'array'
                           and jsonb_array_length(
                               successor.payload_json -> 'citations'
                           ) = 1
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0
                           ) = 'object'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->
                                   'verified'
                           ) = 'true'::jsonb
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0 ->
                                   'document_id'
                           ) = 'number'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'document_id'
                           )::bigint = successor.source_document_id
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0 ->
                                   'page'
                           ) = 'number'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'page'
                           )::integer = evidence.page_no
                           and evidence.page_no > 0
                           and nullif(
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'quote',
                               ''
                           ) is not null
                           and evidence.quote = (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'quote'
                           )
                           and 1 = (
                               select count(*)
                                 from jsonb_array_elements(
                                     comparison.successor_inputs_json
                                 ) as exact_input(value)
                                where jsonb_typeof(
                                    exact_input.value -> 'candidate_id'
                                ) = 'number'
                                  and (
                                      exact_input.value ->> 'candidate_id'
                                  )::bigint = new.successor_candidate_id
                           )
                           and exists (
                               select 1
                                 from jsonb_array_elements(
                                     comparison.successor_inputs_json
                                 ) as exact_input(value)
                                where (
                                    exact_input.value ->> 'candidate_id'
                                )::bigint = new.successor_candidate_id
                                  and (
                                      exact_input.value -> 'citations_verified'
                                  ) = 'true'::jsonb
                                  and jsonb_typeof(
                                      exact_input.value -> 'payload_json' ->
                                          'citations'
                                  ) = 'array'
                                  and jsonb_array_length(
                                      exact_input.value -> 'payload_json' ->
                                          'citations'
                                  ) = 1
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 -> 'verified'
                                  ) = 'true'::jsonb
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'document_id'
                                  )::bigint = evidence.document_id
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'page'
                                  )::integer = evidence.page_no
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'quote'
                                  ) = evidence.quote
                           )
                           and admission.entity_type = 'dependency'
                           and admission.entity_id = new.dependency_id
                           and admission.action in (
                               'accept_candidate', 'merge_candidate'
                           )
                           and admission.human_principal is not null
                           and admission.actor = admission.human_principal
                           and admission.id < act.id
                           and jsonb_typeof(
                               admission.after_json -> 'candidate_id'
                           ) = 'number'
                           and (
                               (
                                   new.predecessor_support_transfer_audit_id
                                       is null
                                   and (
                                       admission.after_json ->> 'candidate_id'
                                   )::bigint = new.predecessor_candidate_id
                               )
                               or (
                                   new.predecessor_support_transfer_audit_id
                                       is not null
                                   and prior_transfer.entity_type = 'dependency'
                                   and prior_transfer.entity_id = new.dependency_id
                                   and prior_transfer.action in (
                                       'reconfirm_operative_support',
                                       'automatic_carry_forward'
                                   )
                                   and admission.id < prior_transfer.id
                                   and prior_transfer.id < act.id
                                   and jsonb_typeof(
                                       prior_transfer.after_json ->
                                           'successor_candidate_id'
                                   ) = 'number'
                                   and (
                                       prior_transfer.after_json ->>
                                           'successor_candidate_id'
                                   )::bigint = new.predecessor_candidate_id
                                   and jsonb_typeof(
                                       prior_transfer.after_json ->
                                           'origin_admission_audit_id'
                                   ) = 'number'
                                   and (
                                       prior_transfer.after_json ->>
                                           'origin_admission_audit_id'
                                   )::bigint = new.origin_admission_audit_id
                                   and (
                                       (
                                           prior_transfer.action =
                                               'reconfirm_operative_support'
                                           and exists (
                                               select 1
                                                 from reconfirmation_receipts prior
                                                where prior.audit_log_id =
                                                    prior_transfer.id
                                                  and prior.dependency_id =
                                                    new.dependency_id
                                                  and prior.successor_candidate_id =
                                                    new.predecessor_candidate_id
                                                  and prior.before_json =
                                                    prior_transfer.before_json
                                                  and prior.after_json =
                                                    prior_transfer.after_json
                                           )
                                       )
                                       or (
                                           prior_transfer.action =
                                               'automatic_carry_forward'
                                           and exists (
                                               select 1
                                                 from automatic_carry_forward_receipts prior
                                                where prior.audit_log_id =
                                                    prior_transfer.id
                                                  and prior.project_id = new.project_id
                                                  and prior.dependency_id =
                                                    new.dependency_id
                                                  and prior.successor_candidate_id =
                                                    new.predecessor_candidate_id
                                                  and prior.origin_admission_audit_id =
                                                    new.origin_admission_audit_id
                                                  and prior.before_json =
                                                    prior_transfer.before_json
                                                  and prior.after_json =
                                                    prior_transfer.after_json
                                           )
                                       )
                                   )
                               )
                           )
                           and new.policy_approval_id is null
                           and not (act.after_json ? 'policy_approval_id')
                           and act.after_json ->> 'policy_version' =
                               new.policy_version
                           and act.after_json ->> 'policy_sha256' =
                               new.policy_sha256
                           and jsonb_typeof(
                               act.after_json -> 'comparison_id'
                           ) = 'number'
                           and (act.after_json ->> 'comparison_id')::bigint =
                               new.comparison_id
                           and jsonb_typeof(act.after_json -> 'finding_id') =
                               'number'
                           and (act.after_json ->> 'finding_id')::bigint =
                               new.finding_id
                           and jsonb_typeof(
                               act.after_json -> 'predecessor_candidate_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'predecessor_candidate_id')::bigint =
                               new.predecessor_candidate_id
                           and jsonb_typeof(
                               act.after_json -> 'successor_candidate_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'successor_candidate_id')::bigint =
                               new.successor_candidate_id
                           and jsonb_typeof(
                               act.after_json -> 'new_evidence_link_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'new_evidence_link_id')::bigint =
                               new.new_evidence_link_id
                           and jsonb_typeof(
                               act.after_json -> 'origin_admission_audit_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'origin_admission_audit_id')::bigint =
                               new.origin_admission_audit_id
                           and (
                               (
                                   new.predecessor_support_transfer_audit_id
                                   is null
                                   and act.after_json ?
                                       'predecessor_support_transfer_audit_id'
                                   and act.after_json ->
                                       'predecessor_support_transfer_audit_id'
                                       = 'null'::jsonb
                               )
                               or (
                                   jsonb_typeof(
                                       act.after_json ->
                                       'predecessor_support_transfer_audit_id'
                                   ) = 'number'
                                   and (act.after_json ->>
                                        'predecessor_support_transfer_audit_id'
                                       )::bigint =
                                       new.predecessor_support_transfer_audit_id
                               )
                           )
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Automatic Carry-Forward receipt binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Automatic Carry-Forward receipts are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_automatic_carry_forward_receipt() OWNER TO corridor;

--
-- Name: enforce_candidate_run_lineage(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_candidate_run_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            demo_project boolean;
            owned_by_run boolean;
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if tg_op = 'DELETE' then
                select exists (
                    select 1 from projects where projects.id = old.project_id
                    and projects.is_synthetic and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then return old; end if;
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if tg_op = 'INSERT' then
                if new.extraction_run_id is not null then
                    raise exception 'Candidate must attach to its run after input capture'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.id is distinct from old.id
               or new.project_id is distinct from old.project_id
               or new.source_document_id is distinct from old.source_document_id
               or new.kind is distinct from old.kind
               or new.source_pages is distinct from old.source_pages
               or new.confidence is distinct from old.confidence
               or new.prompt_version is distinct from old.prompt_version
               or new.model is distinct from old.model
               or new.created_at is distinct from old.created_at then
                raise exception 'Candidate run lineage is immutable' using errcode = '23514';
            end if;
            if new.extraction_run_id is distinct from old.extraction_run_id then
                if old.extraction_run_id is not null or new.extraction_run_id is null then
                    raise exception 'Candidate run lineage is immutable' using errcode = '23514';
                end if;
                select exists (
                    select 1 from extraction_runs
                    join documents on documents.id = extraction_runs.document_id
                    where extraction_runs.id = new.extraction_run_id
                    and extraction_runs.document_id = new.source_document_id
                    and documents.project_id = new.project_id
                    and extraction_runs.prompt_version = new.prompt_version
                    and extraction_runs.model is not distinct from new.model
                    and (
                        extraction_runs.candidate_inputs_json @> jsonb_build_array(
                            jsonb_build_object(
                                'candidate_id', new.id,
                                'project_id', new.project_id,
                                'source_document_id', new.source_document_id,
                                'prompt_version', new.prompt_version,
                                'model', new.model
                            )
                        )
                        or (
                            (extraction_runs.candidate_inputs_json is null
                             or jsonb_typeof(extraction_runs.candidate_inputs_json) = 'null')
                            and exists (
                                select 1 from extraction_run_candidates
                                where extraction_run_candidates.extraction_run_id = new.extraction_run_id
                                and extraction_run_candidates.candidate_id = new.id
                            )
                        )
                    )
                ) into owned_by_run;
                if not owned_by_run then
                    raise exception 'Candidate is absent from its immutable run inputs'
                        using errcode = '23514';
                end if;
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_candidate_run_lineage() OWNER TO corridor;

--
-- Name: enforce_cohort_receipt(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_cohort_receipt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Cohort receipts are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_cohort_receipt() OWNER TO corridor;

--
-- Name: enforce_dependency_admission_outcomes(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_dependency_admission_outcomes() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception 'dependency_admission_outcomes are immutable'
                    using errcode = '23514';
            end;
            $$;


ALTER FUNCTION public.enforce_dependency_admission_outcomes() OWNER TO corridor;

--
-- Name: enforce_document_dispatch_identity(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_document_dispatch_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'document notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'document notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;


ALTER FUNCTION public.enforce_document_dispatch_identity() OWNER TO corridor;

--
-- Name: enforce_document_notification_immutable(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_document_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification occurrences are immutable';
        end
        $$;


ALTER FUNCTION public.enforce_document_notification_immutable() OWNER TO corridor;

--
-- Name: enforce_document_supersession_registry(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_document_supersession_registry() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            successor_registry text;
            source_registry text;
        begin
            if tg_op = 'UPDATE'
               and old.registry_id is not null
               and new.registry_id is distinct from old.registry_id then
                raise exception
                    'documents.registry_id is immutable once set (project %, document %)',
                    new.project_id,
                    new.id
                    using errcode = '23514';
            end if;

            if new.superseded_by is null then
                return new;
            end if;

            select documents.registry_id
              into successor_registry
              from documents
             where documents.project_id = new.project_id
               and documents.id = new.superseded_by;
            if successor_registry is null then
                raise exception
                    'supersession successor must already have a registry_id (project %, predecessor %, successor %)',
                    new.project_id,
                    new.id,
                    new.superseded_by
                    using errcode = '23514';
            end if;

            select documents.registry_id
              into source_registry
              from documents
             where documents.project_id = new.project_id
               and documents.id = new.supersession_source_document_id;
            if source_registry is null then
                raise exception
                    'supersession source must already have a registry_id (project %, predecessor %, source %)',
                    new.project_id,
                    new.id,
                    new.supersession_source_document_id
                    using errcode = '23514';
            end if;

            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_document_supersession_registry() OWNER TO corridor;

--
-- Name: enforce_due_action_dispatch_identity(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_due_action_dispatch_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'due action notification dispatches cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.notification_id <> old.notification_id
               or new.channel <> old.channel
               or new.idempotency_key <> old.idempotency_key
               or new.created_at <> old.created_at then
                raise exception 'due action notification dispatch identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;


ALTER FUNCTION public.enforce_due_action_dispatch_identity() OWNER TO corridor;

--
-- Name: enforce_due_action_notification_immutable(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_due_action_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification occurrences are immutable';
        end
        $$;


ALTER FUNCTION public.enforce_due_action_notification_immutable() OWNER TO corridor;

--
-- Name: enforce_due_work_occurrence_identity(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_due_work_occurrence_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'Due Work occurrences cannot be deleted';
            end if;
            if new.id <> old.id or new.public_id <> old.public_id
               or new.scheduled_job_id <> old.scheduled_job_id
               or new.occurrence_key <> old.occurrence_key
               or new.due_at <> old.due_at or new.created_at <> old.created_at then
                raise exception 'Due Work occurrence identity is immutable';
            end if;
            new.updated_at = now();
            return new;
        end
        $$;


ALTER FUNCTION public.enforce_due_work_occurrence_identity() OWNER TO corridor;

--
-- Name: enforce_due_work_schedule_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_due_work_schedule_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'DELETE' then
                raise exception 'Due Work schedule declarations are append-preserving';
            end if;
            if old.disabled_at is null and new.disabled_at is not null
               and (to_jsonb(new) - 'disabled_at') = (to_jsonb(old) - 'disabled_at') then
                return new;
            end if;
            raise exception 'Due Work schedule declarations are immutable';
        end
        $$;


ALTER FUNCTION public.enforce_due_work_schedule_mutation() OWNER TO corridor;

--
-- Name: enforce_event_admission_outcomes(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_event_admission_outcomes() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception 'event_admission_outcomes are immutable'
                    using errcode = '23514';
            end;
            $$;


ALTER FUNCTION public.enforce_event_admission_outcomes() OWNER TO corridor;

--
-- Name: enforce_event_cohort_receipt(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_event_cohort_receipt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Event cohort receipts are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_event_cohort_receipt() OWNER TO corridor;

--
-- Name: enforce_external_report_release_content_owner(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_external_report_release_content_owner() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if new.content_storage <> 'artifact'
               or new.artifact_id is null
               or new.pdf_bytes is not null
               or new.evaluation_context_json is not null
               or new.record_context_json is not null then
                raise exception
                    'new External Report release requires one artifact content owner without copied bytes or context'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_external_report_release_content_owner() OWNER TO corridor;

--
-- Name: enforce_fact_decision_write(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_fact_decision_write() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare valid_binding boolean;
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Fact decision requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op = 'DELETE' then
                raise exception 'Fact decisions are append-preserving'
                    using errcode='23514';
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.fact_id is distinct from old.fact_id
                   or new.subject_key is distinct from old.subject_key
                   or new.fact_type is distinct from old.fact_type
                   or new.revision_id is distinct from old.revision_id
                   or new.decided_at is distinct from old.decided_at
                   or new.disposition is distinct from old.disposition
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'Fact decision may only become superseded once'
                        using errcode='23514';
                end if;
                return new;
            end if;
            select exists (
                select 1 from facts
                join project_record_revisions
                  on project_record_revisions.id = new.revision_id
                where facts.id = new.fact_id
                  and facts.project_id = new.project_id
                  and facts.subject_key = new.subject_key
                  and facts.fact_type = new.fact_type
                  and project_record_revisions.project_id = new.project_id
                  and (
                      (project_record_revisions.released_policy is not null
                       and project_record_revisions.human_principal is null)
                      or
                      (project_record_revisions.human_principal is not null
                       and project_record_revisions.released_policy is null)
                  )
            ) into valid_binding;
            if not valid_binding then
                raise exception 'Fact decision binding is invalid' using errcode='23514';
            end if;
            return new;
        end; $$;


ALTER FUNCTION public.enforce_fact_decision_write() OWNER TO corridor;

--
-- Name: enforce_fact_sources_append_only(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_fact_sources_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'fact sources are append-only'; end; $$;


ALTER FUNCTION public.enforce_fact_sources_append_only() OWNER TO corridor;

--
-- Name: enforce_facts_append_only(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_facts_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'facts are append-only'; end; $$;


ALTER FUNCTION public.enforce_facts_append_only() OWNER TO corridor;

--
-- Name: enforce_immutable_proposal_spine(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_immutable_proposal_spine() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'Extracted Proposal spine is immutable'; end; $$;


ALTER FUNCTION public.enforce_immutable_proposal_spine() OWNER TO corridor;

--
-- Name: enforce_manifested_class_b_expiry(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_manifested_class_b_expiry() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            manifest bigint;
            family_name text;
            allowed text[];
        begin
            if tg_op <> 'UPDATE' then
                raise exception '% are append-only', tg_table_name;
            end if;
            manifest := nullif(current_setting('corridor.retention_manifest_id', true), '')::bigint;
            family_name := case tg_table_name
                when 'coordination_summary_requests' then 'coordination_summary'
                when 'production_run_explanation_requests' then 'production_run_explanation'
                when 'extraction_failure_diagnosis_requests' then 'extraction_failure_diagnosis'
                when 'revision_change_explanation_requests' then 'revision_change_explanation'
                when 'source_intake_draft_requests' then 'source_intake_draft'
            end;
            allowed := case tg_table_name
                when 'coordination_summary_requests' then array[
                    'project_reading_json', 'summary_markdown',
                    'retention_content_sha256', 'retention_deleted_at']
                when 'production_run_explanation_requests' then array[
                    'competing_run_ids_json', 'comparison_json', 'explanation_json',
                    'execution_lineage_json', 'budget_json', 'usage_json',
                    'retention_content_sha256', 'retention_deleted_at']
                when 'extraction_failure_diagnosis_requests' then array[
                    'source_context_json', 'diagnosis_json', 'execution_lineage_json',
                    'budget_json', 'usage_json', 'retention_content_sha256',
                    'retention_deleted_at']
                when 'revision_change_explanation_requests' then array[
                    'comparison_json', 'explanation_json', 'execution_lineage_json',
                    'budget_json', 'usage_json', 'retention_content_sha256',
                    'retention_deleted_at']
                when 'source_intake_draft_requests' then array[
                    'permitted_pages_json', 'source_json', 'proposals_json',
                    'execution_lineage_json', 'budget_json', 'usage_json',
                    'retention_content_sha256', 'retention_deleted_at']
            end;
            if manifest is null
               or new.retention_class <> 'class_b'
               or new.retention_content_sha256 is null
               or new.retention_deleted_at is null
               or (to_jsonb(new) - allowed) <> (to_jsonb(old) - allowed)
               or not exists (
                    select 1 from retention_manifest_items item
                    join retention_manifests manifest_row on manifest_row.id = item.manifest_id
                    where item.manifest_id = manifest
                      and manifest_row.status = 'dry_run'
                      and item.family = family_name
                      and item.source_row_id = old.id
                      and item.content_sha256 = new.retention_content_sha256
               ) then
                raise exception '% are append-only outside manifested retention', tg_table_name;
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_manifested_class_b_expiry() OWNER TO corridor;

--
-- Name: enforce_milestone_registration(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_milestone_registration() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'INSERT' then return new; end if;
            raise exception 'Milestone Registrations are immutable'
                using errcode = '23514';
        end
        $$;


ALTER FUNCTION public.enforce_milestone_registration() OWNER TO corridor;

--
-- Name: enforce_policy_approvals(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_policy_approvals() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception 'policy_approvals are immutable'
                    using errcode = '23514';
            end;
            $$;


ALTER FUNCTION public.enforce_policy_approvals() OWNER TO corridor;

--
-- Name: enforce_policy_runs(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_policy_runs() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception 'policy_runs are immutable'
                    using errcode = '23514';
            end;
            $$;


ALTER FUNCTION public.enforce_policy_runs() OWNER TO corridor;

--
-- Name: enforce_project_record_revision_write(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_project_record_revision_write() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Project Record revisions are append-only' using errcode='23514';
            end if;
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Project Record revision requires the typed decision command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;


ALTER FUNCTION public.enforce_project_record_revision_write() OWNER TO corridor;

--
-- Name: enforce_prose_segment_non_overlap(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_prose_segment_non_overlap() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if new.kind = 'prose_span' and exists (
                select 1 from source_segments existing
                where existing.document_id = new.document_id
                  and existing.kind = 'prose_span'
                  and existing.page_no = new.page_no
                  and int4range(existing.start_offset, existing.end_offset, '[)')
                      && int4range(new.start_offset, new.end_offset, '[)')
            ) then
                raise exception 'prose source segments cannot overlap';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_prose_segment_non_overlap() OWNER TO corridor;

--
-- Name: enforce_reconfirmation_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_reconfirmation_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            valid_binding boolean;
            demo_project boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from audit_log
                          join dependencies
                            on dependencies.id = new.dependency_id
                          join candidates
                            on candidates.id = new.successor_candidate_id
                         where audit_log.id = new.audit_log_id
                           and audit_log.action =
                               'reconfirm_operative_support'
                           and audit_log.entity_type = 'dependency'
                           and audit_log.entity_id = new.dependency_id
                           and audit_log.before_json = new.before_json
                           and audit_log.after_json = new.after_json
                           and candidates.project_id = dependencies.project_id
                           and jsonb_typeof(
                               audit_log.after_json ->
                               'successor_candidate_id'
                           ) = 'number'
                           and (audit_log.after_json ->>
                                'successor_candidate_id')::bigint =
                               new.successor_candidate_id
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Reconfirmation receipt binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from dependencies
                      join projects
                        on projects.id = dependencies.project_id
                     where dependencies.id = old.dependency_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
            end if;

            raise exception 'Reconfirmation receipts are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_reconfirmation_receipt_mutation() OWNER TO corridor;

--
-- Name: enforce_revision_comparison_finding_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_revision_comparison_finding_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            expected_count integer;
            sealed timestamptz;
            existing_count integer;
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Revision Comparison findings are append-only'
                    using errcode = '23514';
            end if;

            select finding_count, sealed_at into expected_count, sealed
              from revision_comparison_runs
             where id = new.revision_comparison_run_id;
            select count(*) into existing_count
              from revision_comparison_findings
             where revision_comparison_run_id = new.revision_comparison_run_id;
            if expected_count is null or sealed is not null
               or existing_count >= expected_count then
                raise exception 'Revision Comparison finding set is sealed'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.enforce_revision_comparison_finding_mutation() OWNER TO corridor;

--
-- Name: enforce_revision_comparison_run_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_revision_comparison_run_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            existing_count integer;
        begin
            if tg_op = 'INSERT' then
                if new.sealed_at is not null then
                    raise exception
                        'Revision Comparison must begin unsealed'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            if tg_op = 'DELETE' then
                raise exception 'Revision Comparison receipts are append-only'
                    using errcode = '23514';
            end if;

            if old.sealed_at is null
               and new.sealed_at is not null
               and (to_jsonb(new) - 'sealed_at') = (to_jsonb(old) - 'sealed_at') then
                select count(*) into existing_count
                  from revision_comparison_findings
                 where revision_comparison_run_id = old.id;
                if existing_count <> old.finding_count then
                    raise exception
                        'Revision Comparison cannot seal with % of % findings',
                        existing_count,
                        old.finding_count
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Revision Comparison receipts are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_revision_comparison_run_mutation() OWNER TO corridor;

--
-- Name: enforce_source_fact_append_receipts_immutable(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_source_fact_append_receipts_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'source Fact append receipts are immutable'; end; $$;


ALTER FUNCTION public.enforce_source_fact_append_receipts_immutable() OWNER TO corridor;

--
-- Name: enforce_source_segments_append_only(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_source_segments_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'source segments are append-only';
        end;
        $$;


ALTER FUNCTION public.enforce_source_segments_append_only() OWNER TO corridor;

--
-- Name: enforce_structured_fact_satellite_append_only(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_structured_fact_satellite_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'structured Fact satellites are append-only';
        end; $$;


ALTER FUNCTION public.enforce_structured_fact_satellite_append_only() OWNER TO corridor;

--
-- Name: enforce_subject_resolution_append_only(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_subject_resolution_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Subject resolution registry rows are append-only'
                    using errcode='23514';
            end if;
            return new;
        end; $$;


ALTER FUNCTION public.enforce_subject_resolution_append_only() OWNER TO corridor;

--
-- Name: enforce_subject_resolution_decision_write(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_subject_resolution_decision_write() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare valid_binding boolean;
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Subject alias decision requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'Subject alias decisions are append-only'
                    using errcode='23514';
            end if;
            select exists (
                select 1
                from subject_resolution_attempts attempts
                join project_record_revisions revisions
                  on revisions.id = new.revision_id
                where attempts.id = new.attempt_id
                  and attempts.project_id = new.project_id
                  and attempts.source_document_id = new.source_document_id
                  and attempts.source_segment_id = new.source_segment_id
                  and attempts.reference_kind = new.reference_kind
                  and attempts.raw_reference = new.raw_reference
                  and attempts.normalized_reference = new.normalized_reference
                  and attempts.expected_subject_type = new.subject_type
                  and attempts.state not in ('resolved', 'actor_boundary')
                  and revisions.project_id = new.project_id
                  and revisions.command_type = 'register_subject_alias'
                  and revisions.human_principal = new.recorded_by
                  and revisions.released_policy is null
            ) into valid_binding;
            if not valid_binding then
                raise exception 'Subject alias decision binding is invalid'
                    using errcode='23514';
            end if;
            return new;
        end; $$;


ALTER FUNCTION public.enforce_subject_resolution_decision_write() OWNER TO corridor;

--
-- Name: enforce_work_decision(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.enforce_work_decision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            demo_dependency boolean;
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            -- The one sanctioned escape, identical to Candidate lineage
            -- and Active Run declarations: the isolated demo project
            -- resets itself, and only itself.
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from dependencies
                      join projects on projects.id = dependencies.project_id
                     where dependencies.id = old.dependency_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_dependency;
                if demo_dependency then
                    return old;
                end if;
            end if;
            raise exception 'Work Decision receipts are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.enforce_work_decision() OWNER TO corridor;

--
-- Name: extraction_token_usage_membership_is_valid(bigint, jsonb); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.extraction_token_usage_membership_is_valid(run_document_id bigint, usage jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT
    AS $_$
            select case
                when jsonb_typeof(usage -> 'document_ids') is distinct from 'array'
                then false
                else
                    not exists (
                        select 1
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                        where jsonb_typeof(value) <> 'number'
                           or value #>> '{}' !~ '^[1-9][0-9]*$'
                    )
                    and (
                        select count(*) = count(distinct value)
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                    )
                    and exists (
                        select 1
                        from jsonb_array_elements(usage -> 'document_ids') member(value)
                        where case
                            when value #>> '{}' ~ '^[1-9][0-9]*$'
                            then (value #>> '{}')::numeric = run_document_id
                            else false
                        end
                    )
            end
        $_$;


ALTER FUNCTION public.extraction_token_usage_membership_is_valid(run_document_id bigint, usage jsonb) OWNER TO corridor;

--
-- Name: include_structured_cell_fact_decision(bigint, bigint, text, character varying, character varying, character varying); Type: FUNCTION; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            current_decision bigint;
            current_fact bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            eligible boolean;
        begin
            if p_policy <> 'structured-cell-record-inclusion-v1'
               or p_fact_type not in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', 'action_due_date', 'need_date', 'applies_to', 'closure_result') then
                raise exception 'unrecognized structured-cell inclusion policy'
                    using errcode='23514';
            end if;
            select exists (
                select 1 from facts
                join active_extraction_runs
                  on active_extraction_runs.document_id = facts.document_id
                 and active_extraction_runs.extraction_run_id = facts.extraction_run_id
                join extracted_proposal_facts
                  on extracted_proposal_facts.fact_id = facts.id
                join extracted_proposals
                  on extracted_proposals.id = extracted_proposal_facts.proposal_id
                join candidates on candidates.id = extracted_proposals.candidate_id
                where facts.id = p_fact_id
                  and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
                  and candidates.state in ('accepted', 'merged')
                  and candidates.merged_into is not null
                  and (
                      facts.fact_type <> 'external_org'
                      or facts.external_org_value_id is not null
                  )
                  and (
                      facts.fact_type <> 'applies_to'
                      or exists (
                          select 1 from fact_applies_to
                          where fact_applies_to.fact_id = facts.id
                      )
                  )
                  and (
                      facts.fact_type <> 'closure_result'
                      or (
                          exists (
                              select 1 from fact_closure_results
                              where fact_closure_results.fact_id = facts.id
                                and fact_closure_results.closure_kind =
                                    'source_marked_resolved'
                          )
                          and exists (
                              select 1 from fact_closure_sources
                              where fact_closure_sources.fact_id = facts.id
                          )
                      )
                  )
                  and exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind = 'spreadsheet_cell'
                  )
                  and not exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind <> 'spreadsheet_cell'
                  )
            ) into eligible;
            if not eligible then
                raise exception 'Fact is not eligible for released structured-cell inclusion'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select id, fact_id into current_decision, existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if current_decision is null or existing_fact <> p_fact_id then
                    raise exception 'Record Inclusion key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select id, fact_id into current_decision, current_fact from fact_decisions
             where project_id = p_project_id and subject_key = p_subject_key
               and fact_type = p_fact_type and superseded_by is null;
            if current_decision is not null and current_fact = p_fact_id then
                select revision_id into existing_revision from fact_decisions
                 where id = current_decision;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'include_structured_cell_fact',
                null, p_policy, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if current_decision is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = current_decision;
            end if;
            insert into fact_decisions (
                id, project_id, fact_id, subject_key, fact_type, revision_id, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;


ALTER FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) OWNER TO corridor_fact_decision_writer;

--
-- Name: is_attributable_statement_scope_actor(text); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.is_attributable_statement_scope_actor(actor text) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $_$
            select actor in (
                'corridor:event-admission',
                'corridor:statement-migration-v1'
            )
            or (
                actor ~ '^[a-z][a-z0-9._-]{1,31}:[^[:space:]]+$'
                and lower(substring(actor from '^[^:]+:(.*)$')) not in (
                    'agent', 'demo', 'extractor', 'reviewer', 'system'
                )
            );
        $_$;


ALTER FUNCTION public.is_attributable_statement_scope_actor(actor text) OWNER TO corridor;

--
-- Name: prevent_external_report_artifact_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.prevent_external_report_artifact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;


ALTER FUNCTION public.prevent_external_report_artifact_mutation() OWNER TO corridor;

--
-- Name: prevent_external_report_release_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.prevent_external_report_release_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;


ALTER FUNCTION public.prevent_external_report_release_mutation() OWNER TO corridor;

--
-- Name: purge_external_party_statement_rows(bigint, text); Type: FUNCTION; Schema: public; Owner: corridor_statement_retirement
--

CREATE FUNCTION public.purge_external_party_statement_rows(target_project_id bigint, target_purpose text) RETURNS void
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public'
    AS $$
        begin
            if target_purpose = 'retirement' then
                perform 1 from public.legacy_ledger_archives
                where project_id = target_project_id;
                if not found then
                    raise exception 'statement retirement requires a sealed Legacy Ledger archive'
                        using errcode = '23514';
                end if;
            elsif target_purpose = 'demo_reset' then
                perform 1 from public.projects
                where id = target_project_id
                  and slug = 'corridor-demo'
                  and is_synthetic is true;
                if not found then
                    raise exception 'statement reset is allowed only for the synthetic corridor-demo project'
                        using errcode = '23514';
                end if;
            else
                raise exception 'unrecognized statement retirement purpose'
                    using errcode = '23514';
            end if;

            -- The External Party fact may retire only while no internal
            -- Coordination Plan depends on its durable lineage.  Deleting
            -- the plan would violate immutable Work Decision history; leaving
            -- it would leave a subject with no accepted statement.  The
            -- caller must use the dependent-lineage workflow instead.
            if exists (
                select 1
                from public.work_decisions decision
                join public.commitment_lineages lineage
                  on lineage.id = decision.commitment_lineage_id
                where lineage.project_id = target_project_id
            ) then
                raise exception 'statement retirement refuses a dependent Coordination Plan'
                    using errcode = '23514';
            end if;

            with event_evidence as (
                delete from public.dependency_event_evidence
                where event_id in (
                    select id from public.dependency_events
                    where project_id = target_project_id
                )
                returning evidence_link_id
            )
            delete from public.evidence_links link
            using event_evidence mapping
            where link.id = mapping.evidence_link_id;
            delete from public.dependency_event_timings where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_event_scopes where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_event_scope_decisions where event_id in (
                select id from public.dependency_events where project_id = target_project_id
            );
            delete from public.dependency_events where project_id = target_project_id;
            delete from public.commitment_lineages where project_id = target_project_id;
        end;
        $$;


ALTER FUNCTION public.purge_external_party_statement_rows(target_project_id bigint, target_purpose text) OWNER TO corridor_statement_retirement;

--
-- Name: record_human_fact_decision(bigint, bigint, text, character varying, character varying, character varying, text, character varying, bigint); Type: FUNCTION; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE FUNCTION public.record_human_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_command_type character varying, p_disposition character varying, p_human_principal text, p_idempotency_key character varying, p_expected_predecessor bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            live_predecessor bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
        begin
            if p_human_principal is null or length(trim(p_human_principal)) = 0 then
                raise exception 'Human Record Decision requires an attributed principal'
                    using errcode='23514';
            end if;
            if p_command_type not in ('record_verbal_statement', 'coordinate_statement', 'correct_statement_scope', 'correct_statement_facts', 'mark_do_not_add', 'restore_do_not_add', 'resolve_discrepancy', 'designate_support', 'resolve_support') then
                raise exception 'unrecognized human record decision command'
                    using errcode='23514';
            end if;
            if p_disposition not in ('include', 'do_not_add', 'restore') then
                raise exception 'unrecognized human record decision disposition'
                    using errcode='23514';
            end if;
            if not exists (
                select 1 from facts
                where facts.id = p_fact_id and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
            ) then
                raise exception 'Human Record Decision names a fact that does not exist'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select fact_id into existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if existing_fact is null or existing_fact <> p_fact_id then
                    raise exception 'Human Record Decision key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id',
                    (select id from fact_decisions where revision_id = existing_revision),
                    'created', false
                );
            end if;
            if p_expected_predecessor is not null then
                select id into live_predecessor from fact_decisions
                 where id = p_expected_predecessor and project_id = p_project_id
                   and superseded_by is null;
                if live_predecessor is null then
                    raise exception 'Human Record Decision predecessor is stale'
                        using errcode='23514';
                end if;
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, p_command_type,
                p_human_principal, null, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if p_expected_predecessor is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = p_expected_predecessor;
            end if;
            insert into fact_decisions (
                id, project_id, fact_id, subject_key, fact_type,
                revision_id, disposition, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, p_disposition, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;


ALTER FUNCTION public.record_human_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_command_type character varying, p_disposition character varying, p_human_principal text, p_idempotency_key character varying, p_expected_predecessor bigint) OWNER TO corridor_fact_decision_writer;

--
-- Name: record_subject_alias_decision(bigint, bigint, character varying, bigint, character varying, character varying); Type: FUNCTION; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE FUNCTION public.record_subject_alias_decision(p_project_id bigint, p_attempt_id bigint, p_subject_type character varying, p_subject_id bigint, p_human_principal character varying, p_idempotency_key character varying) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
        declare
            source_attempt subject_resolution_attempts%rowtype;
            existing_revision bigint;
            existing_decision bigint;
            existing_subject_type varchar;
            existing_subject_id bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            target_is_active boolean;
        begin
            if length(trim(p_human_principal)) = 0
               or length(trim(p_idempotency_key)) = 0 then
                raise exception 'Subject alias decision requires human and idempotency identity'
                    using errcode='23514';
            end if;
            select * into source_attempt from subject_resolution_attempts
             where id = p_attempt_id and project_id = p_project_id;
            if source_attempt.id is null
               or source_attempt.state in ('resolved', 'actor_boundary')
               or source_attempt.expected_subject_type <> p_subject_type then
                raise exception 'Subject alias decision attempt is ineligible'
                    using errcode='23514';
            end if;
            if p_subject_type = 'external_org' then
                select exists (
                    select 1 from external_orgs where id = p_subject_id
                ) into target_is_active;
            elsif p_subject_type = 'person' then
                select exists (
                    select 1 from stated_by_people people
                    where people.id = p_subject_id
                      and people.project_id = p_project_id
                ) into target_is_active;
            elsif p_subject_type = 'constraint' then
                select exists (
                    select 1 from dependencies
                    where id = p_subject_id and project_id = p_project_id
                      and dismissed_at is null
                ) into target_is_active;
            elsif p_subject_type = 'document' then
                select exists (
                    select 1 from documents
                    where id = p_subject_id and project_id = p_project_id
                      and superseded_by is null
                ) into target_is_active;
            else
                target_is_active := false;
            end if;
            if not target_is_active then
                raise exception 'Subject alias decision target is absent, stale, or out of scope'
                    using errcode='23514';
            end if;

            select revisions.id into existing_revision
              from project_record_revisions revisions
             where revisions.project_id = p_project_id
               and revisions.idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select decisions.id, decisions.subject_type,
                    case decisions.subject_type
                        when 'external_org' then decisions.external_org_id
                        when 'person' then decisions.stated_by_person_id
                        when 'constraint' then decisions.dependency_id
                        when 'document' then decisions.document_id
                    end
                  into existing_decision, existing_subject_type, existing_subject_id
                  from subject_resolution_decisions decisions
                 where decisions.revision_id = existing_revision;
                if existing_decision is null
                   or existing_subject_type <> p_subject_type
                   or existing_subject_id <> p_subject_id then
                    raise exception 'Subject alias decision key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', existing_decision,
                    'created', false
                );
            end if;

            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'register_subject_alias',
                p_human_principal, null, p_idempotency_key
            ) returning id into new_revision;

            insert into subject_resolution_decisions (
                project_id, attempt_id, revision_id, source_document_id,
                source_segment_id, reference_kind, raw_reference,
                normalized_reference, decision_kind, subject_type,
                external_org_id, stated_by_person_id, dependency_id, document_id,
                recorded_by
            ) values (
                p_project_id, source_attempt.id, new_revision,
                source_attempt.source_document_id, source_attempt.source_segment_id,
                source_attempt.reference_kind, source_attempt.raw_reference,
                source_attempt.normalized_reference, 'human_alias_registration',
                p_subject_type,
                case when p_subject_type = 'external_org' then p_subject_id end,
                case when p_subject_type = 'person' then p_subject_id end,
                case when p_subject_type = 'constraint' then p_subject_id end,
                case when p_subject_type = 'document' then p_subject_id end,
                p_human_principal
            ) returning id into new_decision;
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;


ALTER FUNCTION public.record_subject_alias_decision(p_project_id bigint, p_attempt_id bigint, p_subject_type character varying, p_subject_id bigint, p_human_principal character varying, p_idempotency_key character varying) OWNER TO corridor_fact_decision_writer;

--
-- Name: refuse_assignment_attempt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_assignment_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification attempts are append-only';
        end
        $$;


ALTER FUNCTION public.refuse_assignment_attempt_mutation() OWNER TO corridor;

--
-- Name: refuse_assignment_feedback_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_assignment_feedback_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification feedback is append-only';
        end
        $$;


ALTER FUNCTION public.refuse_assignment_feedback_mutation() OWNER TO corridor;

--
-- Name: refuse_document_attempt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_document_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification attempts are append-only';
        end
        $$;


ALTER FUNCTION public.refuse_document_attempt_mutation() OWNER TO corridor;

--
-- Name: refuse_due_action_attempt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_due_action_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification attempts are append-only';
        end
        $$;


ALTER FUNCTION public.refuse_due_action_attempt_mutation() OWNER TO corridor;

--
-- Name: refuse_due_work_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_due_work_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Due Work receipts are append-only';
        end
        $$;


ALTER FUNCTION public.refuse_due_work_receipt_mutation() OWNER TO corridor;

--
-- Name: refuse_event_admission_acceptance_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_event_admission_acceptance_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Event Admission acceptance history is immutable';
        end
        $$;


ALTER FUNCTION public.refuse_event_admission_acceptance_mutation() OWNER TO corridor;

--
-- Name: refuse_evidence_investigation_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_evidence_investigation_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
          raise exception 'Evidence Investigation receipts are append-only';
        end $$;


ALTER FUNCTION public.refuse_evidence_investigation_receipt_mutation() OWNER TO corridor;

--
-- Name: refuse_key_date_draft_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.refuse_key_date_draft_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
          raise exception 'Key date draft receipts are append-only';
        end $$;


ALTER FUNCTION public.refuse_key_date_draft_receipt_mutation() OWNER TO corridor;

--
-- Name: reject_assignment_notification_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_assignment_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification history cannot be truncated';
        end
        $$;


ALTER FUNCTION public.reject_assignment_notification_truncate() OWNER TO corridor;

--
-- Name: reject_condition_resolutions_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_condition_resolutions_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'condition_resolutions are append-only'; end; $$;


ALTER FUNCTION public.reject_condition_resolutions_mutation() OWNER TO corridor;

--
-- Name: reject_coordination_summary_configurations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_coordination_summary_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'coordination_summary_configurations are append-only'; end; $$;


ALTER FUNCTION public.reject_coordination_summary_configurations_mutation() OWNER TO corridor;

--
-- Name: reject_coordination_summary_requests_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_coordination_summary_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'coordination_summary_requests are append-only'; end; $$;


ALTER FUNCTION public.reject_coordination_summary_requests_mutation() OWNER TO corridor;

--
-- Name: reject_dependency_dismissal_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dependency_dismissal_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dependency_dismissals is append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_dependency_dismissal_mutation() OWNER TO corridor;

--
-- Name: reject_dependency_event_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dependency_event_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'External Party statement Evidence is append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_dependency_event_evidence_mutation() OWNER TO corridor;

--
-- Name: reject_dependency_event_migration_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dependency_event_migration_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user <> 'corridor_statement_retirement'
               and not (tg_op = 'DELETE' and pg_trigger_depth() > 1) then
                raise exception 'statement migration receipts are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_dependency_event_migration_receipt_mutation() OWNER TO corridor;

--
-- Name: reject_dependency_event_scope_decision_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dependency_event_scope_decision_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'Commitment Scope decisions are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_dependency_event_scope_decision_mutation() OWNER TO corridor;

--
-- Name: reject_dependency_event_scope_link_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dependency_event_scope_link_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                raise exception 'Commitment Scope links are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_dependency_event_scope_link_mutation() OWNER TO corridor;

--
-- Name: reject_dispute_history_resolution_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dispute_history_resolution_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dispute_history_resolutions is append-only';
        end;
        $$;


ALTER FUNCTION public.reject_dispute_history_resolution_mutation() OWNER TO corridor;

--
-- Name: reject_dispute_settlement_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_dispute_settlement_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dispute_settlements is append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_dispute_settlement_mutation() OWNER TO corridor;

--
-- Name: reject_document_notification_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_document_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification history cannot be truncated';
        end
        $$;


ALTER FUNCTION public.reject_document_notification_truncate() OWNER TO corridor;

--
-- Name: reject_document_rendition_derivation_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_document_rendition_derivation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document rendition derivations are append-only';
        end;
        $$;


ALTER FUNCTION public.reject_document_rendition_derivation_mutation() OWNER TO corridor;

--
-- Name: reject_documentation_confirmation_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_documentation_confirmation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'documentation field confirmations are append-only';
        end;
        $$;


ALTER FUNCTION public.reject_documentation_confirmation_mutation() OWNER TO corridor;

--
-- Name: reject_due_action_notification_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_due_action_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification history cannot be truncated';
        end
        $$;


ALTER FUNCTION public.reject_due_action_notification_truncate() OWNER TO corridor;

--
-- Name: reject_due_work_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_due_work_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Due Work operational history cannot be truncated';
        end
        $$;


ALTER FUNCTION public.reject_due_work_truncate() OWNER TO corridor;

--
-- Name: reject_evidence_investigation_capture_contracts_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'evidence_investigation_capture_contracts are append-only'; end; $$;


ALTER FUNCTION public.reject_evidence_investigation_capture_contracts_mutation() OWNER TO corridor;

--
-- Name: reject_evidence_investigation_capture_results_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_evidence_investigation_capture_results_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'evidence_investigation_capture_results are append-only'; end; $$;


ALTER FUNCTION public.reject_evidence_investigation_capture_results_mutation() OWNER TO corridor;

--
-- Name: reject_external_party_statement_child_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_party_statement_child_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_event_id bigint;
            target_source text;
        begin
            if current_user <> 'corridor_statement_retirement' then
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
                select source_kind into target_source
                from dependency_events where id = target_event_id;
                if target_source = 'verbal' then
                    raise exception 'verbal dependency events are append-only'
                        using errcode = '23514';
                end if;
                raise exception 'External Party statements are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_external_party_statement_child_mutation() OWNER TO corridor;

--
-- Name: reject_external_party_statement_evidence_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_party_statement_evidence_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_evidence_link_id bigint;
        begin
            target_evidence_link_id := case when tg_op = 'DELETE'
                then old.id else coalesce(old.id, new.id) end;
            if current_user <> 'corridor_statement_retirement'
               and exists (
                   select 1 from dependency_event_evidence mapping
                   where mapping.evidence_link_id = target_evidence_link_id
               ) then
                raise exception 'External Party statement Evidence is append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_external_party_statement_evidence_mutation() OWNER TO corridor;

--
-- Name: reject_external_party_statement_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_party_statement_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user <> 'corridor_statement_retirement' then
                if (tg_op = 'DELETE' and old.source_kind = 'verbal')
                   or (tg_op = 'UPDATE' and (
                       old.source_kind = 'verbal' or new.source_kind = 'verbal'
                   )) then
                    raise exception 'verbal dependency events are append-only'
                        using errcode = '23514';
                end if;
                raise exception 'External Party statements are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_external_party_statement_mutation() OWNER TO corridor;

--
-- Name: reject_external_party_statement_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_party_statement_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'External Party statements are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_external_party_statement_truncate() OWNER TO corridor;

--
-- Name: reject_external_report_artifact_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_report_artifact_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;


ALTER FUNCTION public.reject_external_report_artifact_truncate() OWNER TO corridor;

--
-- Name: reject_external_report_release_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_external_report_release_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;


ALTER FUNCTION public.reject_external_report_release_truncate() OWNER TO corridor;

--
-- Name: reject_extraction_failure_diagnosis_configurations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'extraction_failure_diagnosis_configurations are append-only'; end; $$;


ALTER FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation() OWNER TO corridor;

--
-- Name: reject_extraction_failure_diagnosis_requests_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'extraction_failure_diagnosis_requests are append-only'; end; $$;


ALTER FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation() OWNER TO corridor;

--
-- Name: reject_extraction_measurement_case_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_extraction_measurement_case_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Extraction Measurement case states are append-only';
        end;
        $$;


ALTER FUNCTION public.reject_extraction_measurement_case_mutation() OWNER TO corridor;

--
-- Name: reject_extraction_run_receipt_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_extraction_run_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            demo_project boolean;
        begin
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from documents
                      join projects on projects.id = documents.project_id
                     where documents.id = old.document_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
            end if;
            raise exception 'Extraction Run receipts are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_extraction_run_receipt_mutation() OWNER TO corridor;

--
-- Name: reject_legacy_ledger_archive_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_legacy_ledger_archive_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Legacy Ledger archives are immutable'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_legacy_ledger_archive_mutation() OWNER TO corridor;

--
-- Name: reject_organization_identity_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_organization_identity_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'organization identity history is append-only';
        end;
        $$;


ALTER FUNCTION public.reject_organization_identity_mutation() OWNER TO corridor;

--
-- Name: reject_production_run_explanation_configurations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_production_run_explanation_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'production_run_explanation_configurations are append-only'; end; $$;


ALTER FUNCTION public.reject_production_run_explanation_configurations_mutation() OWNER TO corridor;

--
-- Name: reject_production_run_explanation_requests_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_production_run_explanation_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'production_run_explanation_requests are append-only'; end; $$;


ALTER FUNCTION public.reject_production_run_explanation_requests_mutation() OWNER TO corridor;

--
-- Name: reject_project_check_configuration_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_project_check_configuration_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'project check configurations are append-only';
        end;
        $$;


ALTER FUNCTION public.reject_project_check_configuration_mutation() OWNER TO corridor;

--
-- Name: reject_revision_change_explanation_configurations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_revision_change_explanation_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'revision_change_explanation_configurations are append-only'; end; $$;


ALTER FUNCTION public.reject_revision_change_explanation_configurations_mutation() OWNER TO corridor;

--
-- Name: reject_revision_change_explanation_requests_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_revision_change_explanation_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'revision_change_explanation_requests are append-only'; end; $$;


ALTER FUNCTION public.reject_revision_change_explanation_requests_mutation() OWNER TO corridor;

--
-- Name: reject_revision_comparison_truncate(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_revision_comparison_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Revision Comparison receipts are append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_revision_comparison_truncate() OWNER TO corridor;

--
-- Name: reject_schedule_linking_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_schedule_linking_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'schedule linking records are append-only';
        end;
        $$;


ALTER FUNCTION public.reject_schedule_linking_mutation() OWNER TO corridor;

--
-- Name: reject_scheduled_report_publications_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_scheduled_report_publications_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'scheduled_report_publications are append-only'; end; $$;


ALTER FUNCTION public.reject_scheduled_report_publications_mutation() OWNER TO corridor;

--
-- Name: reject_source_intake_draft_configurations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_source_intake_draft_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'source_intake_draft_configurations are append-only'; end; $$;


ALTER FUNCTION public.reject_source_intake_draft_configurations_mutation() OWNER TO corridor;

--
-- Name: reject_source_intake_draft_requests_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_source_intake_draft_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'source_intake_draft_requests are append-only'; end; $$;


ALTER FUNCTION public.reject_source_intake_draft_requests_mutation() OWNER TO corridor;

--
-- Name: reject_statement_lifecycle_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_statement_lifecycle_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Guided statement lifecycle history is append-only'
                using errcode = '23514';
        end;
        $$;


ALTER FUNCTION public.reject_statement_lifecycle_mutation() OWNER TO corridor;

--
-- Name: reject_statement_suggestion_protection_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_statement_suggestion_protection_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'statement suggestion protection history is append-only';
        end;
        $$;


ALTER FUNCTION public.reject_statement_suggestion_protection_mutation() OWNER TO corridor;

--
-- Name: reject_unreadable_cell_admission_activations_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_unreadable_cell_admission_activations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_admission_activations are append-only'; end; $$;


ALTER FUNCTION public.reject_unreadable_cell_admission_activations_mutation() OWNER TO corridor;

--
-- Name: reject_unreadable_cell_reading_profiles_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_profiles are append-only'; end; $$;


ALTER FUNCTION public.reject_unreadable_cell_reading_profiles_mutation() OWNER TO corridor;

--
-- Name: reject_unreadable_cell_reading_runs_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_unreadable_cell_reading_runs_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_runs are append-only'; end; $$;


ALTER FUNCTION public.reject_unreadable_cell_reading_runs_mutation() OWNER TO corridor;

--
-- Name: reject_unreadable_cell_reading_steps_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_unreadable_cell_reading_steps_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_steps are append-only'; end; $$;


ALTER FUNCTION public.reject_unreadable_cell_reading_steps_mutation() OWNER TO corridor;

--
-- Name: reject_unreadable_cell_resolutions_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_unreadable_cell_resolutions_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_resolutions are append-only'; end; $$;


ALTER FUNCTION public.reject_unreadable_cell_resolutions_mutation() OWNER TO corridor;

--
-- Name: reject_verbal_dependency_event_mutation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.reject_verbal_dependency_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if current_user = 'corridor_statement_retirement' then
                if tg_op = 'TRUNCATE' then
                    return null;
                end if;
                return case when tg_op = 'DELETE' then old else new end;
            end if;
            if tg_op = 'TRUNCATE' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'DELETE' and old.source_kind = 'verbal' then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'UPDATE' and (
                old.source_kind = 'verbal' or new.source_kind = 'verbal'
            ) then
                raise exception 'verbal dependency events are append-only'
                    using errcode = '23514';
            end if;
            return case when tg_op = 'DELETE' then old else new end;
        end;
        $$;


ALTER FUNCTION public.reject_verbal_dependency_event_mutation() OWNER TO corridor;

--
-- Name: require_dependency_admission_abstention_eligibility(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_dependency_admission_abstention_eligibility() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if new.outcome = 'abstained' and
               (new.eligibility_json is null or new.eligibility_sha256 is null) then
                raise exception 'new Dependency Admission Abstention requires exact eligibility';
            end if;
            return new;
        end
        $$;


ALTER FUNCTION public.require_dependency_admission_abstention_eligibility() OWNER TO corridor;

--
-- Name: require_fact_value_source(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_fact_value_source() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if new.fact_type = 'supporting_documentation_in_use' then
                return null;
            end if;
            if not exists (
                select 1 from fact_sources
                where fact_id = new.id and role = 'value_source'
            ) then
                raise exception 'source-backed Fact requires a value source';
            end if;
            return null;
        end; $$;


ALTER FUNCTION public.require_fact_value_source() OWNER TO corridor;

--
-- Name: require_passing_event_admission_activation(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_passing_event_admission_activation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if not exists (
                select 1
                from event_admission_acceptance_receipts receipt
                where receipt.id = new.acceptance_receipt_id
                  and receipt.project_id = new.project_id
                  and receipt.policy_version = new.policy_version
                  and receipt.status = 'passed'
            ) then
                raise exception 'Event Admission activation requires its passing exact receipt';
            end if;
            return new;
        end
        $$;


ALTER FUNCTION public.require_passing_event_admission_activation() OWNER TO corridor;

--
-- Name: require_policy_outcome_counts_by_policy_run_id(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_policy_outcome_counts_by_policy_run_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.policy_run_id);
            return null;
        end;
        $$;


ALTER FUNCTION public.require_policy_outcome_counts_by_policy_run_id() OWNER TO corridor;

--
-- Name: require_policy_outcome_counts_by_run_id(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_policy_outcome_counts_by_run_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.run_id);
            return null;
        end;
        $$;


ALTER FUNCTION public.require_policy_outcome_counts_by_run_id() OWNER TO corridor;

--
-- Name: require_policy_run_counts(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_policy_run_counts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.id);
            return null;
        end;
        $$;


ALTER FUNCTION public.require_policy_run_counts() OWNER TO corridor;

--
-- Name: require_sealed_revision_comparison(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.require_sealed_revision_comparison() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            sealed timestamptz;
            expected_count integer;
            existing_count integer;
        begin
            select sealed_at, finding_count into sealed, expected_count
              from revision_comparison_runs
             where id = new.id;
            select count(*) into existing_count
              from revision_comparison_findings
             where revision_comparison_run_id = new.id;
            if sealed is null or existing_count <> expected_count then
                raise exception 'Revision Comparison must commit sealed and complete'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.require_sealed_revision_comparison() OWNER TO corridor;

--
-- Name: validate_commitment_closure_link(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_commitment_closure_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            lineage_project_id bigint;
            lineage_affected_party_id bigint;
            lineage_stated_party_id bigint;
        begin
            if new.closes_commitment_lineage_id is null then
                return new;
            end if;
            if new.event_type <> 'closure' then
                raise exception 'only an External Party closure may close a Commitment Lineage'
                    using errcode = '23514';
            end if;
            select lineage.project_id,
                   statement.affected_external_org_id,
                   statement.stated_external_org_id
              into lineage_project_id,
                   lineage_affected_party_id,
                   lineage_stated_party_id
            from commitment_lineages lineage
            join dependency_events statement
              on statement.commitment_lineage_id = lineage.id
            where lineage.id = new.closes_commitment_lineage_id
              and statement.event_type in ('commitment', 'committed_date_change')
              and statement.attribution_state = 'resolved'
              and statement.stated_external_org_id is not null
            order by statement.id desc limit 1;
            if lineage_project_id is null
               or lineage_project_id is distinct from new.project_id
               or lineage_affected_party_id is distinct from new.affected_external_org_id
               or lineage_stated_party_id is distinct from new.stated_external_org_id
               or new.attribution_state <> 'resolved' then
                raise exception 'closure must name an attributable Commitment from the same External Party and project'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_commitment_closure_link() OWNER TO corridor;

--
-- Name: validate_commitment_lineage(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_commitment_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_lineage_id bigint;
            current_count integer;
        begin
            if tg_table_name = 'commitment_lineages' then
                target_lineage_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_lineage_id := case when tg_op = 'DELETE'
                    then old.commitment_lineage_id else new.commitment_lineage_id end;
            end if;
            if target_lineage_id is null then
                return null;
            end if;
            if not exists (
                select 1 from commitment_lineages where id = target_lineage_id
            ) then
                return null;
            end if;
            select count(*) into current_count
            from dependency_events event
            where event.commitment_lineage_id = target_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_count <> 1 then
                raise exception 'Commitment Lineage needs exactly one current accepted statement'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_commitment_lineage() OWNER TO corridor;

--
-- Name: validate_commitment_lineage_event(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_commitment_lineage_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_lineage_id bigint;
            target_project_id bigint;
            event_project_id bigint;
            event_type_value text;
            attribution_value text;
            stated_party_id bigint;
            predecessor_lineage_id bigint;
            predecessor_affected_party_id bigint;
            predecessor_stated_party_id bigint;
            current_count integer;
        begin
            target_lineage_id := case when tg_op = 'DELETE'
                then old.commitment_lineage_id else new.commitment_lineage_id end;
            if target_lineage_id is null then
                return null;
            end if;
            select project_id into target_project_id
            from commitment_lineages where id = target_lineage_id;
            if target_project_id is null then
                return null;
            end if;
            if tg_op <> 'DELETE' then
                select project_id, event_type, attribution_state, stated_external_org_id
                  into event_project_id, event_type_value, attribution_value, stated_party_id
                from dependency_events where id = new.id;
                if event_project_id is null
                   or event_project_id is distinct from target_project_id
                   or event_type_value not in ('commitment', 'committed_date_change')
                   or attribution_value <> 'resolved'
                   or stated_party_id is null then
                    raise exception 'Commitment Lineage needs one accepted attributable statement'
                        using errcode = '23514';
                end if;
            end if;
            if tg_op <> 'DELETE' and new.supersedes_event_id is not null then
                select
                    commitment_lineage_id,
                    affected_external_org_id,
                    stated_external_org_id
                  into
                    predecessor_lineage_id,
                    predecessor_affected_party_id,
                    predecessor_stated_party_id
                from dependency_events where id = new.supersedes_event_id;
                if predecessor_lineage_id is distinct from target_lineage_id then
                    raise exception 'statement correction must remain in one Commitment Lineage'
                        using errcode = '23514';
                end if;
                if predecessor_affected_party_id is distinct from new.affected_external_org_id
                   or predecessor_stated_party_id is distinct from new.stated_external_org_id then
                    raise exception 'statement correction must keep the same External Parties'
                        using errcode = '23514';
                end if;
            end if;
            select count(*) into current_count
            from dependency_events event
            where event.commitment_lineage_id = target_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_count <> 1 then
                raise exception 'Commitment Lineage needs exactly one current accepted statement'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_commitment_lineage_event() OWNER TO corridor;

--
-- Name: validate_dependency_event_evidence(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_dependency_event_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            direct_dependency_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            if direct_dependency_id is not null then
                raise exception 'event Evidence cannot carry direct Dependency ownership'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_dependency_event_evidence() OWNER TO corridor;

--
-- Name: validate_dependency_event_scope_decision(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_dependency_event_scope_decision() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_decision_id bigint;
            target_event_id bigint;
            target_mode text;
            target_source text;
            link_count integer;
            active_count integer;
            selected_count integer;
            predecessor_event_id bigint;
        begin
            if tg_table_name = 'dependency_event_scope_decisions' then
                target_decision_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_decision_id := case when tg_op = 'DELETE'
                    then old.scope_decision_id else new.scope_decision_id end;
            end if;
            select decision.event_id, decision.scope_mode, event.source_kind
              into target_event_id, target_mode, target_source
              from dependency_event_scope_decisions decision
              join dependency_events event on event.id = decision.event_id
             where decision.id = target_decision_id;
            if target_event_id is null then return null; end if;
            if tg_table_name = 'dependency_event_scope_decisions' then
                if tg_op <> 'DELETE' and new.supersedes_scope_decision_id is not null then
                    select event_id into predecessor_event_id
                      from dependency_event_scope_decisions
                     where id = new.supersedes_scope_decision_id;
                    if predecessor_event_id is distinct from new.event_id then
                        raise exception 'scope correction must supersede a decision on the same statement'
                            using errcode = '23514';
                    end if;
                end if;
            end if;
            select count(*) into link_count from dependency_event_scopes
             where scope_decision_id = target_decision_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links'
                    using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active', 'carried_forward') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links'
                    using errcode = '23514';
            end if;
            if target_mode = 'all_active' then
                select count(*) into active_count
                  from dependencies dependency
                  join dependency_events event on event.id = target_event_id
                 where dependency.project_id = event.project_id
                   and dependency.external_org_id = event.affected_external_org_id
                   and dependency.dismissed_at is null;
                select count(*) into selected_count
                  from dependency_event_scopes scope
                  join dependencies dependency on dependency.id = scope.dependency_id
                  join dependency_events event on event.id = target_event_id
                 where scope.scope_decision_id = target_decision_id
                   and dependency.project_id = event.project_id
                   and dependency.external_org_id = event.affected_external_org_id
                   and dependency.dismissed_at is null;
                if active_count <> selected_count or link_count <> active_count then
                    raise exception 'all-active scope must record the exact eligible Dependency snapshot'
                        using errcode = '23514';
                end if;
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_dependency_event_scope_decision() OWNER TO corridor;

--
-- Name: validate_dependency_event_scope_decision_actor(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_dependency_event_scope_decision_actor() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if not is_attributable_statement_scope_actor(new.decided_by) then
                raise exception 'Commitment Scope decision needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_dependency_event_scope_decision_actor() OWNER TO corridor;

--
-- Name: validate_dependency_event_scope_decision_link(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_dependency_event_scope_decision_link() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            decision_event_id bigint;
            decision_mode text;
            decision_actor text;
            event_project bigint;
            event_party bigint;
            dependency_project bigint;
            dependency_party bigint;
            dependency_dismissed timestamp with time zone;
        begin
            if new.scope_decision_id is null then
                select decision.id, decision.decided_by
                  into new.scope_decision_id, decision_actor
                from dependency_event_scope_decisions decision
                where decision.event_id = new.event_id
                  and not exists (
                      select 1 from dependency_event_scope_decisions later
                      where later.supersedes_scope_decision_id = decision.id
                  )
                order by decision.id;
            end if;
            select decision.event_id, decision.scope_mode, decision.decided_by,
                   event.project_id, event.affected_external_org_id
              into decision_event_id, decision_mode, decision_actor,
                   event_project, event_party
            from dependency_event_scope_decisions decision
            join dependency_events event on event.id = decision.event_id
            where decision.id = new.scope_decision_id;
            if decision_event_id is null or decision_event_id <> new.event_id then
                raise exception 'statement scope link must belong to its event decision'
                    using errcode = '23514';
            end if;
            if new.recorded_by is null then
                new.recorded_by := decision_actor;
            end if;
            if not is_attributable_statement_scope_actor(new.recorded_by) then
                raise exception 'statement scope link needs a named human or deployed policy actor'
                    using errcode = '23514';
            end if;
            if decision_mode = 'unknown' then
                raise exception 'unknown statement scope cannot link a Dependency'
                    using errcode = '23514';
            end if;
            select project_id, external_org_id, dismissed_at
              into dependency_project, dependency_party, dependency_dismissed
            from dependencies where id = new.dependency_id;
            if event_project is distinct from dependency_project then
                raise exception 'statement scope cannot cross projects'
                    using errcode = '23514';
            end if;
            if event_party is distinct from dependency_party then
                raise exception 'statement scope names another External Party'
                    using errcode = '23514';
            end if;
            if dependency_dismissed is not null then
                raise exception 'statement scope cannot include a dismissed Dependency'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_dependency_event_scope_decision_link() OWNER TO corridor;

--
-- Name: validate_dependency_evidence_sufficiency_scope_role(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_dependency_evidence_sufficiency_scope_role() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            direct_dependency_id bigint;
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null then
                if new.scope_link_id is not null
                   or direct_dependency_id is distinct from new.dependency_id then
                    raise exception 'direct Evidence role must name its direct Dependency'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.scope_link_id is null then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            if scope_event_id is distinct from evidence_event_id
               or scope_dependency_id is distinct from new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_dependency_evidence_sufficiency_scope_role() OWNER TO corridor;

--
-- Name: validate_evidence_link_ownership(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_evidence_link_ownership() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_evidence_link_id bigint;
            direct_dependency_id bigint;
            mapping_count integer;
        begin
            target_evidence_link_id := case when tg_table_name = 'evidence_links'
                then coalesce(
                    (to_jsonb(old)->>'id')::bigint,
                    (to_jsonb(new)->>'id')::bigint
                )
                else coalesce(
                    (to_jsonb(old)->>'evidence_link_id')::bigint,
                    (to_jsonb(new)->>'evidence_link_id')::bigint
                )
            end;
            select dependency_id into direct_dependency_id
            from evidence_links where id = target_evidence_link_id;
            if not found then
                return null;
            end if;
            select count(*) into mapping_count
            from dependency_event_evidence
            where evidence_link_id = target_evidence_link_id;
            if direct_dependency_id is null and mapping_count <> 1 then
                raise exception 'statement Evidence needs exactly one event owner'
                    using errcode = '23514';
            end if;
            if direct_dependency_id is not null and mapping_count <> 0 then
                raise exception 'direct Evidence cannot carry an event owner'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from dependency_evidence_sufficiencies sufficiency
                where sufficiency.evidence_link_id = target_evidence_link_id
                  and (
                      direct_dependency_id is null
                      or sufficiency.scope_link_id is not null
                      or sufficiency.dependency_id is distinct from direct_dependency_id
                  )
            ) then
                raise exception 'direct Evidence roles must retain the direct Dependency owner'
                    using errcode = '23514';
            end if;
            if exists (
                select 1
                from operative_support support
                where support.evidence_link_id = target_evidence_link_id
                  and (
                      direct_dependency_id is null
                      or support.scope_link_id is not null
                      or support.dependency_id is distinct from direct_dependency_id
                  )
            ) then
                raise exception 'direct Evidence support must retain the direct Dependency owner'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_evidence_link_ownership() OWNER TO corridor;

--
-- Name: validate_extraction_measurement_case_state(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_extraction_measurement_case_state() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare predecessor extraction_measurement_case_states%rowtype;
        begin
            if new.predecessor_state_id is null then
                if new.state <> 'active' then
                    raise exception 'an Extraction Measurement case must begin active';
                end if;
                return new;
            end if;
            select * into predecessor
            from extraction_measurement_case_states
            where id = new.predecessor_state_id;
            if not found
               or predecessor.project_id <> new.project_id
               or predecessor.case_key <> new.case_key
               or predecessor.kind <> new.kind then
                raise exception 'Extraction Measurement case predecessor mismatch';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_extraction_measurement_case_state() OWNER TO corridor;

--
-- Name: validate_operative_event_evidence_scope_role(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_operative_event_evidence_scope_role() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            direct_dependency_id bigint;
            scope_event_id bigint;
            scope_dependency_id bigint;
            evidence_event_id bigint;
        begin
            select dependency_id into direct_dependency_id
            from evidence_links where id = new.evidence_link_id;
            select event_id into evidence_event_id
            from dependency_event_evidence
            where evidence_link_id = new.evidence_link_id;
            if evidence_event_id is null then
                if new.scope_link_id is not null
                   or direct_dependency_id is distinct from new.dependency_id then
                    raise exception 'direct Evidence role must name its direct Dependency'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            if new.scope_link_id is null then
                raise exception 'event Evidence publication support needs its exact scope link'
                    using errcode = '23514';
            end if;
            select scope.event_id, scope.dependency_id
              into scope_event_id, scope_dependency_id
            from dependency_event_scopes scope where scope.id = new.scope_link_id;
            if scope_event_id is distinct from evidence_event_id
               or scope_dependency_id is distinct from new.dependency_id then
                raise exception 'event Evidence role must name its exact Dependency scope link'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_operative_event_evidence_scope_role() OWNER TO corridor;

--
-- Name: validate_verbal_statement_shape(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_verbal_statement_shape() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_event_id bigint;
            target_source text;
            target_event_date date;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_event_id := case when tg_op = 'DELETE'
                    then old.event_id else new.event_id end;
            end if;
            select source_kind, event_date
              into target_source, target_event_date
            from dependency_events where id = target_event_id;
            if target_source is distinct from 'verbal' then
                return null;
            end if;
            if target_event_date is null then
                raise exception 'Verbal statements require the conversation date'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_verbal_statement_shape() OWNER TO corridor;

--
-- Name: validate_work_decision_milestone_impact(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_work_decision_milestone_impact() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_decision_id bigint;
            target_field text;
            target_lineage_id bigint;
            target_project_id bigint;
            impact_value text;
            impact_json jsonb;
            impact_state text;
            linked_ids bigint[];
            receipt_ids bigint[];
            invalid_project_link boolean;
            current_owner_value text;
            current_action_value text;
            current_action_reason text;
            current_action_json jsonb;
        begin
            if tg_table_name = 'work_decisions' then
                target_decision_id := case when tg_op = 'DELETE'
                    then (to_jsonb(old) ->> 'id')::bigint
                    else (to_jsonb(new) ->> 'id')::bigint
                end;
            else
                target_decision_id := case when tg_op = 'DELETE'
                    then (to_jsonb(old) ->> 'work_decision_id')::bigint
                    else (to_jsonb(new) ->> 'work_decision_id')::bigint
                end;
            end if;
            select field, commitment_lineage_id, after_value
              into target_field, target_lineage_id, impact_value
            from work_decisions where id = target_decision_id;
            if target_field is null then
                return null;
            end if;
            select project_id into target_project_id
            from commitment_lineages where id = target_lineage_id;
            select coalesce(array_agg(milestone_id order by milestone_id), '{}')
              into linked_ids
            from work_decision_milestone_impacts
            where work_decision_id = target_decision_id;
            if target_field <> 'milestone_impact' then
                if cardinality(linked_ids) <> 0 then
                    raise exception 'only Milestone Impact may link Milestones'
                        using errcode = '23514';
                end if;
                return null;
            end if;
            if impact_value is null then
                raise exception 'Milestone Impact needs an explicit state'
                    using errcode = '23514';
            end if;
            begin
                impact_json := impact_value::jsonb;
            exception when others then
                raise exception 'Milestone Impact receipt must be structured'
                    using errcode = '23514';
            end;
            if jsonb_typeof(impact_json) <> 'object'
               or jsonb_typeof(impact_json -> 'milestone_ids') <> 'array' then
                raise exception 'Milestone Impact receipt must name a state and Milestones'
                    using errcode = '23514';
            end if;
            impact_state := impact_json ->> 'state';
            if impact_state is null
               or impact_state not in ('affects', 'does_not_affect', 'not_yet_known') then
                raise exception 'Milestone Impact has an unknown state'
                    using errcode = '23514';
            end if;
            begin
                select coalesce(array_agg(value::bigint order by value::bigint), '{}')
                  into receipt_ids
                from jsonb_array_elements_text(impact_json -> 'milestone_ids');
            exception when others then
                raise exception 'Milestone Impact receipt has invalid Milestone identity'
                    using errcode = '23514';
            end;
            if cardinality(receipt_ids) <> (
                select count(distinct value::bigint)
                from jsonb_array_elements_text(impact_json -> 'milestone_ids')
            ) then
                raise exception 'Milestone Impact receipt repeats a Milestone'
                    using errcode = '23514';
            end if;
            if impact_state = 'affects' and cardinality(receipt_ids) = 0 then
                raise exception 'an affecting Milestone Impact needs Milestones'
                    using errcode = '23514';
            end if;
            if impact_state <> 'affects' and cardinality(receipt_ids) <> 0 then
                raise exception 'only an affecting Milestone Impact names Milestones'
                    using errcode = '23514';
            end if;
            if receipt_ids is distinct from linked_ids then
                raise exception 'Milestone Impact receipt and exact links disagree'
                    using errcode = '23514';
            end if;
            select exists (
                select 1
                from milestones milestone
                where milestone.id = any(linked_ids)
                  and milestone.project_id is distinct from target_project_id
            ) into invalid_project_link;
            if invalid_project_link then
                raise exception 'Milestone Impact cannot cross projects'
                    using errcode = '23514';
            end if;
            if impact_state = 'not_yet_known' then
                select decision.after_value into current_owner_value
                from work_decisions decision
                where decision.commitment_lineage_id = target_lineage_id
                  and decision.field = 'internal_owner'
                  and not exists (
                      select 1 from work_decisions successor
                      where successor.predecessor_decision_id = decision.id
                  );
                select decision.after_value, decision.action_due_date_reason
                  into current_action_value, current_action_reason
                from work_decisions decision
                where decision.commitment_lineage_id = target_lineage_id
                  and decision.field = 'next_action'
                  and not exists (
                      select 1 from work_decisions successor
                      where successor.predecessor_decision_id = decision.id
                  );
                if current_owner_value is null or current_action_value is null then
                    raise exception 'unknown Milestone Impact needs a current Internal Owner and Next Action'
                        using errcode = '23514';
                end if;
                begin
                    current_action_json := current_action_value::jsonb;
                exception when others then
                    raise exception 'current Next Action receipt must be structured'
                        using errcode = '23514';
                end;
                if (current_action_json ->> 'due_date') is null
                   and current_action_reason not in (
                       'awaiting_external_information',
                       'awaiting_schedule_information',
                       'date_not_yet_known'
                   ) then
                    raise exception 'unknown Milestone Impact needs an Action Due Date or structured unknown-date reason'
                        using errcode = '23514';
                end if;
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.validate_work_decision_milestone_impact() OWNER TO corridor;

--
-- Name: validate_work_decision_subject(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.validate_work_decision_subject() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            dependency_project_id bigint;
            dependency_dismissed_at timestamp with time zone;
            lineage_project_id bigint;
            current_statement_count integer;
        begin
            if new.dependency_id is not null then
                select project_id, dismissed_at
                  into dependency_project_id, dependency_dismissed_at
                from dependencies where id = new.dependency_id;
                if dependency_project_id is null or dependency_dismissed_at is not null then
                    raise exception 'Work Decision needs an open Dependency Coordination Subject'
                        using errcode = '23514';
                end if;
                if new.field = 'milestone_impact' then
                    raise exception 'Milestone Impact belongs to a Committed Date Change'
                        using errcode = '23514';
                end if;
                return new;
            end if;
            select project_id into lineage_project_id
            from commitment_lineages where id = new.commitment_lineage_id;
            if lineage_project_id is null then
                raise exception 'Work Decision needs a Commitment Lineage subject'
                    using errcode = '23514';
            end if;
            select count(*) into current_statement_count
            from dependency_events event
            where event.commitment_lineage_id = new.commitment_lineage_id
              and event.event_type in ('commitment', 'committed_date_change')
              and event.attribution_state = 'resolved'
              and event.stated_external_org_id is not null
              and not exists (
                  select 1 from dependency_events successor
                  where successor.supersedes_event_id = event.id
              );
            if current_statement_count <> 1 then
                raise exception 'Work Decision needs one accepted current statement subject'
                    using errcode = '23514';
            end if;
            if new.field = 'milestone_impact' and not exists (
                select 1
                from dependency_events event
                where event.commitment_lineage_id = new.commitment_lineage_id
                  and event.event_type = 'committed_date_change'
                  and not exists (
                      select 1 from dependency_events successor
                      where successor.supersedes_event_id = event.id
                  )
            ) then
                raise exception 'Milestone Impact belongs to a Committed Date Change'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;


ALTER FUNCTION public.validate_work_decision_subject() OWNER TO corridor;

--
-- Name: verify_dependency_event_scope_shape(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.verify_dependency_event_scope_shape() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_event_id bigint; target_mode text; target_source text;
            target_event_date date; target_event_type text; link_count integer;
            new_precision text; previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            elsif tg_table_name = 'dependency_event_timings' then
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            else
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            end if;
            select scope_mode, source_kind, event_date, event_type
              into target_mode, target_source, target_event_date, target_event_type
            from dependency_events where id = target_event_id;
            if target_mode is null then return null; end if;
            select count(*) into link_count from dependency_event_scopes where event_id = target_event_id;
            if target_mode = 'unknown' and link_count <> 0 then
                raise exception 'unknown statement scope has Dependency links' using errcode = '23514';
            end if;
            if target_mode in ('selected', 'all_active', 'carried_forward') and link_count = 0 then
                raise exception 'known statement scope has no Dependency links' using errcode = '23514';
            end if;
            if target_source = 'verbal' and target_event_type <> 'closure' then
                select precision into new_precision from dependency_event_timings
                where event_id = target_event_id and kind = 'new';
                select count(*) into previous_count from dependency_event_timings
                where event_id = target_event_id and kind = 'previous';
                if target_event_date is null or target_mode <> 'selected'
                   or link_count <> 1 or new_precision is distinct from 'day'
                   or previous_count <> 0 then
                    raise exception 'verbal statements require one exact-day commitment and one Dependency'
                        using errcode = '23514';
                end if;
            end if;
            if target_source = 'verbal' and target_event_type = 'closure'
               and target_event_date is null then
                raise exception 'a Verbal closure requires its conversation date'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.verify_dependency_event_scope_shape() OWNER TO corridor;

--
-- Name: verify_dependency_event_timing_cardinality(); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.verify_dependency_event_timing_cardinality() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            target_event_id bigint;
            target_event_type text;
            new_count integer;
            previous_count integer;
        begin
            if tg_table_name = 'dependency_events' then
                target_event_id := case when tg_op = 'DELETE' then old.id else new.id end;
            else
                target_event_id := case when tg_op = 'DELETE' then old.event_id else new.event_id end;
            end if;
            select event_type into target_event_type
            from dependency_events where id = target_event_id;
            if target_event_type is null then
                return null;
            end if;
            select
                count(*) filter (where kind = 'new'),
                count(*) filter (where kind = 'previous')
            into new_count, previous_count
            from dependency_event_timings where event_id = target_event_id;
            if target_event_type = 'commitment'
               and (new_count <> 1 or previous_count <> 0) then
                raise exception 'Commitment requires exactly one new timing'
                    using errcode = '23514';
            end if;
            if target_event_type = 'committed_date_change'
               and (new_count <> 1 or previous_count <> 1) then
                raise exception 'Committed Date Change requires previous and new timings'
                    using errcode = '23514';
            end if;
            if target_event_type = 'closure'
               and (new_count <> 0 or previous_count <> 0) then
                raise exception 'closure cannot carry a commitment timing'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;


ALTER FUNCTION public.verify_dependency_event_timing_cardinality() OWNER TO corridor;

--
-- Name: verify_policy_run_counts(bigint); Type: FUNCTION; Schema: public; Owner: corridor
--

CREATE FUNCTION public.verify_policy_run_counts(target_run_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
        declare
            run_family text;
            expected_applied integer;
            expected_abstained integer;
            actual_applied integer;
            actual_abstained integer;
        begin
            select family, applied_count, abstained_count
              into run_family, expected_applied, expected_abstained
              from policy_runs
             where id = target_run_id;
            if run_family is null then
                raise exception
                    'policy runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            if run_family = 'automatic-carry-forward' then
                select
                    count(*) filter (where outcome = 'carried'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from automatic_carry_forward_outcomes
                 where run_id = target_run_id;
            elsif run_family = 'event-admission' then
                select
                    count(*) filter (where outcome = 'admitted'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from event_admission_outcomes
                 where policy_run_id = target_run_id;
            else
                select
                    count(*) filter (where outcome = 'admitted'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from dependency_admission_outcomes
                 where policy_run_id = target_run_id;
            end if;
            if actual_applied <> expected_applied
               or actual_abstained <> expected_abstained then
                raise exception
                    'policy runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            return;
        end;
        $$;


ALTER FUNCTION public.verify_policy_run_counts(target_run_id bigint) OWNER TO corridor;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: active_extraction_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.active_extraction_runs (
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.active_extraction_runs OWNER TO corridor;

--
-- Name: active_run_declarations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.active_run_declarations (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    declared_by character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    predecessor_declaration_id bigint
);


ALTER TABLE public.active_run_declarations OWNER TO corridor;

--
-- Name: active_run_declarations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.active_run_declarations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.active_run_declarations_id_seq OWNER TO corridor;

--
-- Name: active_run_declarations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.active_run_declarations_id_seq OWNED BY public.active_run_declarations.id;


--
-- Name: assertions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.assertions (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    field_name character varying(64) NOT NULL,
    asserted_value text,
    evidence_link_id bigint NOT NULL,
    doc_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.assertions OWNER TO corridor;

--
-- Name: assertions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.assertions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.assertions_id_seq OWNER TO corridor;

--
-- Name: assertions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.assertions_id_seq OWNED BY public.assertions.id;


--
-- Name: assignment_notification_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.assignment_notification_attempts (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    dispatch_id bigint NOT NULL,
    project_id bigint NOT NULL,
    attempt_number integer NOT NULL,
    outcome character varying(24) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    error_code character varying(64),
    runtime_owner character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assignment_attempt_outcome CHECK (((outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text, ('skipped'::character varying)::text]))),
    CONSTRAINT ck_assignment_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_assignment_attempt_positive CHECK ((attempt_number > 0))
);


ALTER TABLE public.assignment_notification_attempts OWNER TO corridor;

--
-- Name: assignment_notification_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.assignment_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.assignment_notification_attempts_id_seq OWNER TO corridor;

--
-- Name: assignment_notification_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.assignment_notification_attempts_id_seq OWNED BY public.assignment_notification_attempts.id;


--
-- Name: assignment_notification_dispatches; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.assignment_notification_dispatches (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    notification_id bigint NOT NULL,
    project_id bigint NOT NULL,
    channel character varying(16) NOT NULL,
    delivery_state character varying(16) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error_code character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    idempotency_key character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assignment_dispatch_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_assignment_dispatch_channel CHECK (((channel)::text = 'email'::text)),
    CONSTRAINT ck_assignment_dispatch_idempotency_hex CHECK (((idempotency_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_assignment_dispatch_retry_shape CHECK (((((delivery_state)::text = 'retry_due'::text) AND (next_attempt_at IS NOT NULL)) OR (((delivery_state)::text <> 'retry_due'::text) AND (next_attempt_at IS NULL)))),
    CONSTRAINT ck_assignment_dispatch_state CHECK (((delivery_state)::text = ANY (ARRAY[('queued'::character varying)::text, ('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text])))
);


ALTER TABLE public.assignment_notification_dispatches OWNER TO corridor;

--
-- Name: assignment_notification_dispatches_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.assignment_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.assignment_notification_dispatches_id_seq OWNER TO corridor;

--
-- Name: assignment_notification_dispatches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.assignment_notification_dispatches_id_seq OWNED BY public.assignment_notification_dispatches.id;


--
-- Name: assignment_notification_feedback; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.assignment_notification_feedback (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    notification_id bigint NOT NULL,
    project_id bigint NOT NULL,
    flagged_by character varying(128) NOT NULL,
    feedback_kind character varying(24) NOT NULL,
    note text,
    audit_log_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assignment_feedback_actor CHECK ((length(TRIM(BOTH FROM flagged_by)) > 0)),
    CONSTRAINT ck_assignment_feedback_kind CHECK (((feedback_kind)::text = 'incorrect_assignment'::text))
);


ALTER TABLE public.assignment_notification_feedback OWNER TO corridor;

--
-- Name: assignment_notification_feedback_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.assignment_notification_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.assignment_notification_feedback_id_seq OWNER TO corridor;

--
-- Name: assignment_notification_feedback_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.assignment_notification_feedback_id_seq OWNED BY public.assignment_notification_feedback.id;


--
-- Name: assignment_notifications; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.assignment_notifications (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    project_id bigint NOT NULL,
    category character varying(32) NOT NULL,
    subject_kind character varying(16) NOT NULL,
    dependency_id bigint,
    commitment_lineage_id bigint,
    assignment_decision_id bigint NOT NULL,
    recipient_roster_entry_id bigint NOT NULL,
    recipient_principal_subject character varying(128) NOT NULL,
    occurrence_key character varying(64) NOT NULL,
    registered_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assignment_notification_actor CHECK ((length(TRIM(BOTH FROM registered_by)) > 0)),
    CONSTRAINT ck_assignment_notification_category CHECK (((category)::text = 'new_assignment'::text)),
    CONSTRAINT ck_assignment_notification_key_hex CHECK (((occurrence_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_assignment_notification_recipient CHECK ((length(TRIM(BOTH FROM recipient_principal_subject)) > 0)),
    CONSTRAINT ck_assignment_notification_subject_kind CHECK (((subject_kind)::text = ANY (ARRAY[('constraint'::character varying)::text, ('statement'::character varying)::text]))),
    CONSTRAINT ck_assignment_notification_subject_shape CHECK (((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL))))
);


ALTER TABLE public.assignment_notifications OWNER TO corridor;

--
-- Name: assignment_notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.assignment_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.assignment_notifications_id_seq OWNER TO corridor;

--
-- Name: assignment_notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.assignment_notifications_id_seq OWNED BY public.assignment_notifications.id;


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    actor text NOT NULL,
    action character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    entity_id bigint NOT NULL,
    before_json jsonb,
    after_json jsonb,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    human_principal text
);


ALTER TABLE public.audit_log OWNER TO corridor;

--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.audit_log_id_seq OWNER TO corridor;

--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: automatic_carry_forward_outcomes; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.automatic_carry_forward_outcomes (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    project_id bigint NOT NULL,
    policy_approval_id bigint,
    dependency_id bigint NOT NULL,
    outcome character varying(32) NOT NULL,
    reason character varying(128),
    reason_version character varying(64),
    receipt_audit_log_id bigint,
    comparison_id bigint,
    finding_id bigint,
    predecessor_candidate_id bigint,
    successor_candidate_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'automatic-carry-forward'::character varying NOT NULL,
    CONSTRAINT ck_automatic_carry_forward_outcome_family CHECK (((family)::text = 'automatic-carry-forward'::text)),
    CONSTRAINT ck_automatic_carry_forward_outcome_kind CHECK (((((outcome)::text = 'carried'::text) AND (reason IS NULL) AND (reason_version IS NULL) AND (receipt_audit_log_id IS NOT NULL)) OR (((outcome)::text = 'abstained'::text) AND (reason IS NOT NULL) AND (reason_version IS NOT NULL) AND (receipt_audit_log_id IS NULL)))),
    CONSTRAINT ck_automatic_carry_forward_outcome_value CHECK (((outcome)::text = ANY (ARRAY[('carried'::character varying)::text, ('abstained'::character varying)::text])))
);


ALTER TABLE public.automatic_carry_forward_outcomes OWNER TO corridor;

--
-- Name: automatic_carry_forward_outcomes_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.automatic_carry_forward_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.automatic_carry_forward_outcomes_id_seq OWNER TO corridor;

--
-- Name: automatic_carry_forward_outcomes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.automatic_carry_forward_outcomes_id_seq OWNED BY public.automatic_carry_forward_outcomes.id;


--
-- Name: automatic_carry_forward_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.automatic_carry_forward_receipts (
    audit_log_id bigint NOT NULL,
    project_id bigint NOT NULL,
    policy_approval_id bigint,
    dependency_id bigint NOT NULL,
    comparison_id bigint NOT NULL,
    finding_id bigint NOT NULL,
    predecessor_candidate_id bigint NOT NULL,
    successor_candidate_id bigint NOT NULL,
    new_evidence_link_id bigint NOT NULL,
    origin_admission_audit_id bigint NOT NULL,
    predecessor_support_transfer_audit_id bigint,
    before_json jsonb NOT NULL,
    after_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'automatic-carry-forward'::character varying NOT NULL,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_automatic_carry_forward_receipt_after_object CHECK ((jsonb_typeof(after_json) = 'object'::text)),
    CONSTRAINT ck_automatic_carry_forward_receipt_before_object CHECK ((jsonb_typeof(before_json) = 'object'::text)),
    CONSTRAINT ck_automatic_carry_forward_receipt_family CHECK (((family)::text = 'automatic-carry-forward'::text)),
    CONSTRAINT ck_automatic_carry_forward_receipt_policy_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.automatic_carry_forward_receipts OWNER TO corridor;

--
-- Name: candidate_dispositions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.candidate_dispositions (
    id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    disposition character varying(32) NOT NULL,
    reason character varying(64),
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_candidate_dispositions_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_candidate_dispositions_kind CHECK (((disposition)::text = ANY (ARRAY[('accepted'::character varying)::text, ('not_relevant'::character varying)::text]))),
    CONSTRAINT ck_candidate_dispositions_reason CHECK (((((disposition)::text = 'accepted'::text) AND (reason IS NULL)) OR (((disposition)::text = 'not_relevant'::text) AND (reason IS NOT NULL))))
);


ALTER TABLE public.candidate_dispositions OWNER TO corridor;

--
-- Name: candidate_dispositions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.candidate_dispositions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.candidate_dispositions_id_seq OWNER TO corridor;

--
-- Name: candidate_dispositions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.candidate_dispositions_id_seq OWNED BY public.candidate_dispositions.id;


--
-- Name: candidates; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.candidates (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    kind character varying(10) NOT NULL,
    payload_json jsonb NOT NULL,
    source_document_id bigint NOT NULL,
    source_pages integer[] NOT NULL,
    confidence double precision,
    prompt_version character varying(64),
    model character varying(64),
    citations_verified boolean DEFAULT false NOT NULL,
    state character varying(8) DEFAULT 'pending'::character varying NOT NULL,
    merged_into bigint,
    adjudicated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    extraction_run_id bigint,
    CONSTRAINT candidate_kind CHECK (((kind)::text = ANY (ARRAY[('dependency'::character varying)::text, ('event'::character varying)::text, ('evidence'::character varying)::text]))),
    CONSTRAINT candidate_state CHECK (((state)::text = ANY (ARRAY[('pending'::character varying)::text, ('accepted'::character varying)::text, ('merged'::character varying)::text, ('rejected'::character varying)::text])))
);


ALTER TABLE public.candidates OWNER TO corridor;

--
-- Name: candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.candidates_id_seq OWNER TO corridor;

--
-- Name: candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.candidates_id_seq OWNED BY public.candidates.id;


--
-- Name: cohort_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.cohort_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    revision_comparison_run_id bigint NOT NULL,
    predecessor_extraction_run_id bigint NOT NULL,
    successor_extraction_run_id bigint NOT NULL,
    external_org text NOT NULL,
    rule_version character varying(64) NOT NULL,
    matcher_version character varying(64) NOT NULL,
    members jsonb NOT NULL,
    member_count integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.cohort_receipts OWNER TO corridor;

--
-- Name: cohort_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.cohort_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cohort_receipts_id_seq OWNER TO corridor;

--
-- Name: cohort_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.cohort_receipts_id_seq OWNED BY public.cohort_receipts.id;


--
-- Name: commitment_lineages; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.commitment_lineages (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    internal_owner text,
    next_action text,
    action_due_date date,
    action_due_date_reason character varying(64),
    milestone_impact character varying(32),
    milestone_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    plan_needs_review boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deferral_reason character varying(64),
    deferral_return_date date
);


ALTER TABLE public.commitment_lineages OWNER TO corridor;

--
-- Name: commitment_lineages_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.commitment_lineages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.commitment_lineages_id_seq OWNER TO corridor;

--
-- Name: commitment_lineages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.commitment_lineages_id_seq OWNED BY public.commitment_lineages.id;


--
-- Name: condition_resolutions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.condition_resolutions (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    evidence_link_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    condition_text text NOT NULL,
    basis_evidence_link_id bigint,
    basis_event_id bigint,
    reason text,
    receipt_json jsonb,
    resolved_by character varying(128) NOT NULL,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_condition_resolution_actor CHECK ((length(TRIM(BOTH FROM resolved_by)) > 0)),
    CONSTRAINT ck_condition_resolution_clear_has_basis CHECK ((((kind)::text <> 'cleared'::text) OR (basis_evidence_link_id IS NOT NULL) OR (basis_event_id IS NOT NULL))),
    CONSTRAINT ck_condition_resolution_dismissal_has_reason CHECK ((((kind)::text <> 'dismissed'::text) OR ((reason IS NOT NULL) AND (length(TRIM(BOTH FROM reason)) > 0)))),
    CONSTRAINT ck_condition_resolution_kind CHECK (((kind)::text = ANY (ARRAY[('cleared'::character varying)::text, ('dismissed'::character varying)::text]))),
    CONSTRAINT ck_condition_resolution_text CHECK ((length(TRIM(BOTH FROM condition_text)) > 0))
);


ALTER TABLE public.condition_resolutions OWNER TO corridor;

--
-- Name: condition_resolutions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.condition_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.condition_resolutions_id_seq OWNER TO corridor;

--
-- Name: condition_resolutions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.condition_resolutions_id_seq OWNED BY public.condition_resolutions.id;


--
-- Name: coordination_summary_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.coordination_summary_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    source_scope character varying(32) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    timeout_seconds integer NOT NULL,
    max_requests integer NOT NULL,
    retry_policy character varying(32) NOT NULL,
    retention_policy character varying(64) NOT NULL,
    observation_context character varying(128) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_summary_config_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_summary_config_context CHECK ((length(TRIM(BOTH FROM observation_context)) > 0)),
    CONSTRAINT ck_summary_config_input_budget CHECK (((max_input_tokens >= 1) AND (max_input_tokens <= 200000))),
    CONSTRAINT ck_summary_config_model CHECK ((length(TRIM(BOTH FROM model)) > 0)),
    CONSTRAINT ck_summary_config_no_retry CHECK (((retry_policy)::text = 'none'::text)),
    CONSTRAINT ck_summary_config_one_request CHECK ((max_requests = 1)),
    CONSTRAINT ck_summary_config_output_budget CHECK (((max_output_tokens >= 1) AND (max_output_tokens <= 20000))),
    CONSTRAINT ck_summary_config_prompt CHECK ((length(TRIM(BOTH FROM prompt_version)) > 0)),
    CONSTRAINT ck_summary_config_retention CHECK (((retention_policy)::text = ANY ((ARRAY['retained_indefinitely'::character varying, 'class_b_30_days'::character varying])::text[]))),
    CONSTRAINT ck_summary_config_source_scope CHECK (((source_scope)::text = ANY (ARRAY[('all_sources'::character varying)::text, ('documents_only'::character varying)::text]))),
    CONSTRAINT ck_summary_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);


ALTER TABLE public.coordination_summary_configurations OWNER TO corridor;

--
-- Name: coordination_summary_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.coordination_summary_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.coordination_summary_configurations_id_seq OWNER TO corridor;

--
-- Name: coordination_summary_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.coordination_summary_configurations_id_seq OWNED BY public.coordination_summary_configurations.id;


--
-- Name: coordination_summary_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.coordination_summary_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    reading_sha256 character varying(64) NOT NULL,
    project_reading_json jsonb,
    evaluated_on date NOT NULL,
    ruleset_version character varying(32) NOT NULL,
    statement_publication_fingerprint character varying(64) NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    summary_markdown text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    retention_class character varying(16) DEFAULT 'class_b'::character varying NOT NULL,
    retention_content_sha256 character varying(64),
    retention_deleted_at timestamp with time zone,
    CONSTRAINT ck_coordination_summary_requests_retention_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_coordination_summary_requests_retention_digest CHECK (((retention_content_sha256 IS NULL) OR ((retention_content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_summary_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_summary_request_reading_sha CHECK (((reading_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_summary_request_status CHECK (((status)::text = ANY (ARRAY[('completed'::character varying)::text, ('empty_input'::character varying)::text, ('budget_exhausted'::character varying)::text, ('timeout'::character varying)::text, ('transport_failure'::character varying)::text, ('validation_refused'::character varying)::text])))
);


ALTER TABLE public.coordination_summary_requests OWNER TO corridor;

--
-- Name: coordination_summary_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.coordination_summary_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.coordination_summary_requests_id_seq OWNER TO corridor;

--
-- Name: coordination_summary_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.coordination_summary_requests_id_seq OWNED BY public.coordination_summary_requests.id;


--
-- Name: extracted_proposals; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extracted_proposals (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    subject_key text NOT NULL,
    candidate_metadata_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.extracted_proposals OWNER TO corridor;

--
-- Name: fact_decisions; Type: TABLE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TABLE public.fact_decisions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    subject_key text NOT NULL,
    fact_type character varying(64) NOT NULL,
    revision_id bigint NOT NULL,
    superseded_by bigint,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    disposition character varying(32) DEFAULT 'include'::character varying NOT NULL,
    CONSTRAINT ck_fact_decision_disposition CHECK (((disposition)::text = ANY ((ARRAY['include'::character varying, 'do_not_add'::character varying, 'restore'::character varying])::text[])))
);


ALTER TABLE public.fact_decisions OWNER TO corridor_fact_decision_writer;

--
-- Name: facts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.facts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint,
    extraction_run_id bigint,
    fact_type character varying(64) NOT NULL,
    subject_kind character varying(32) NOT NULL,
    subject_key text NOT NULL,
    text_value text,
    date_value date,
    date_range_start date,
    date_range_end date,
    external_org_value_id bigint,
    document_value_id bigint,
    transformation character varying(64) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    content_sha256 character varying(64),
    CONSTRAINT ck_facts_content_sha256 CHECK (((content_sha256 IS NULL) OR ((content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_facts_recorded_by CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_facts_source_binding CHECK ((((document_id IS NOT NULL) AND (extraction_run_id IS NOT NULL) AND ((fact_type)::text <> ALL ((ARRAY['statement_timing'::character varying, 'supporting_documentation_in_use'::character varying])::text[]))) OR ((document_id IS NULL) AND (extraction_run_id IS NULL) AND ((fact_type)::text = ANY ((ARRAY['statement_wording'::character varying, 'statement_timing'::character varying, 'applies_to'::character varying, 'supporting_documentation_in_use'::character varying])::text[]))))),
    CONSTRAINT ck_facts_subject CHECK (((((fact_type)::text = ANY ((ARRAY['utility_id'::character varying, 'external_org'::character varying, 'external_org_contact'::character varying, 'utility_type'::character varying, 'utility_subtype'::character varying, 'utility_function'::character varying, 'operational_status'::character varying, 'size'::character varying, 'material'::character varying, 'oh_ug'::character varying, 'row_placement'::character varying, 'orientation'::character varying, 'baseline'::character varying, 'station_from'::character varying, 'station_to'::character varying, 'offset_from'::character varying, 'offset_to'::character varying, 'sue_level'::character varying, 'conflict_description'::character varying, 'resolution_strategy'::character varying, 'notes'::character varying, 'alignment'::character varying, 'location_start'::character varying, 'location_end'::character varying, 'offset_side'::character varying, 'potential_conflict'::character varying, 'data_source'::character varying, 'marked_resolution'::character varying, 'committed_date'::character varying, 'action_due_date'::character varying, 'need_date'::character varying])::text[])) AND ((subject_kind)::text = 'source_row'::text) AND (length(TRIM(BOTH FROM subject_key)) > 0)) OR (((fact_type)::text = ANY ((ARRAY['applies_to'::character varying, 'closure_result'::character varying])::text[])) AND ((subject_kind)::text = 'source_row'::text) AND (length(TRIM(BOTH FROM subject_key)) > 0)) OR (((fact_type)::text = ANY ((ARRAY['statement_wording'::character varying, 'statement_timing'::character varying, 'applies_to'::character varying])::text[])) AND ((subject_kind)::text = 'statement_candidate'::text) AND (length(TRIM(BOTH FROM subject_key)) > 0)) OR (((fact_type)::text = 'supporting_documentation_in_use'::text) AND ((subject_kind)::text = 'record_subject'::text) AND (length(TRIM(BOTH FROM subject_key)) > 0)))),
    CONSTRAINT ck_facts_type CHECK (((fact_type)::text = ANY ((ARRAY['utility_id'::character varying, 'external_org'::character varying, 'external_org_contact'::character varying, 'utility_type'::character varying, 'utility_subtype'::character varying, 'utility_function'::character varying, 'operational_status'::character varying, 'size'::character varying, 'material'::character varying, 'oh_ug'::character varying, 'row_placement'::character varying, 'orientation'::character varying, 'baseline'::character varying, 'station_from'::character varying, 'station_to'::character varying, 'offset_from'::character varying, 'offset_to'::character varying, 'sue_level'::character varying, 'conflict_description'::character varying, 'resolution_strategy'::character varying, 'notes'::character varying, 'alignment'::character varying, 'location_start'::character varying, 'location_end'::character varying, 'offset_side'::character varying, 'potential_conflict'::character varying, 'data_source'::character varying, 'marked_resolution'::character varying, 'committed_date'::character varying, 'action_due_date'::character varying, 'need_date'::character varying, 'applies_to'::character varying, 'closure_result'::character varying, 'statement_wording'::character varying, 'statement_timing'::character varying, 'supporting_documentation_in_use'::character varying])::text[]))),
    CONSTRAINT ck_facts_typed_value CHECK (((((fact_type)::text = ANY ((ARRAY['utility_id'::character varying, 'external_org'::character varying, 'external_org_contact'::character varying, 'utility_type'::character varying, 'utility_subtype'::character varying, 'utility_function'::character varying, 'operational_status'::character varying, 'size'::character varying, 'material'::character varying, 'oh_ug'::character varying, 'row_placement'::character varying, 'orientation'::character varying, 'baseline'::character varying, 'station_from'::character varying, 'station_to'::character varying, 'offset_from'::character varying, 'offset_to'::character varying, 'sue_level'::character varying, 'conflict_description'::character varying, 'resolution_strategy'::character varying, 'notes'::character varying, 'alignment'::character varying, 'location_start'::character varying, 'location_end'::character varying, 'offset_side'::character varying, 'potential_conflict'::character varying, 'data_source'::character varying, 'marked_resolution'::character varying])::text[])) AND (text_value IS NOT NULL) AND (length(TRIM(BOTH FROM text_value)) > 0) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (((fact_type)::text = 'external_org'::text) OR (external_org_value_id IS NULL)) AND (document_value_id IS NULL) AND ((transformation)::text = 'trim_cell_text_v1'::text)) OR (((fact_type)::text = ANY ((ARRAY['committed_date'::character varying, 'action_due_date'::character varying, 'need_date'::character varying])::text[])) AND (text_value IS NULL) AND (date_value IS NOT NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NULL) AND ((transformation)::text = 'iso_date_cell_v1'::text)) OR (((fact_type)::text = 'applies_to'::text) AND (text_value IS NULL) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NULL) AND ((transformation)::text = 'structured_reference_set_v1'::text)) OR (((fact_type)::text = 'closure_result'::text) AND (text_value IS NULL) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NULL) AND ((transformation)::text = 'typed_closure_result_v1'::text)) OR (((fact_type)::text = 'statement_wording'::text) AND (text_value IS NOT NULL) AND (length(TRIM(BOTH FROM text_value)) > 0) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NULL) AND ((transformation)::text = 'exact_prose_span_v1'::text)) OR (((fact_type)::text = 'statement_timing'::text) AND (text_value IS NULL) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NULL) AND ((transformation)::text = 'typed_statement_timing_v1'::text)) OR (((fact_type)::text = 'supporting_documentation_in_use'::text) AND (text_value IS NULL) AND (date_value IS NULL) AND (date_range_start IS NULL) AND (date_range_end IS NULL) AND (external_org_value_id IS NULL) AND (document_value_id IS NOT NULL) AND ((transformation)::text = 'supporting_document_revision_v1'::text))))
);


ALTER TABLE public.facts OWNER TO corridor;

--
-- Name: current_project_record; Type: VIEW; Schema: public; Owner: corridor
--

CREATE VIEW public.current_project_record AS
 SELECT decisions.project_id,
    proposals.candidate_id,
    candidates.merged_into AS dependency_id,
    decisions.subject_key,
    decisions.fact_type,
    facts.text_value,
    facts.date_value,
    facts.date_range_start,
    facts.date_range_end,
    facts.external_org_value_id,
    facts.document_value_id,
    decisions.id AS decision_id,
    facts.id AS fact_id,
    decisions.revision_id,
    decisions.decided_at
   FROM (((public.fact_decisions decisions
     JOIN public.facts ON ((facts.id = decisions.fact_id)))
     LEFT JOIN public.extracted_proposals proposals ON (((proposals.project_id = facts.project_id) AND (proposals.document_id = facts.document_id) AND (proposals.extraction_run_id = facts.extraction_run_id) AND (proposals.subject_key = facts.subject_key))))
     LEFT JOIN public.candidates ON ((candidates.id = proposals.candidate_id)))
  WHERE ((decisions.superseded_by IS NULL) AND ((decisions.disposition)::text = 'include'::text) AND (NOT (EXISTS ( SELECT 1
           FROM public.fact_decisions suppression
          WHERE ((suppression.project_id = decisions.project_id) AND (suppression.subject_key = decisions.subject_key) AND ((suppression.fact_type)::text = 'statement_wording'::text) AND (suppression.superseded_by IS NULL) AND ((suppression.disposition)::text = 'do_not_add'::text))))));


ALTER VIEW public.current_project_record OWNER TO corridor;

--
-- Name: dependencies; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependencies (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    ref_code character varying(32) NOT NULL,
    dep_type character varying(18) NOT NULL,
    title text NOT NULL,
    location_desc text,
    station_from character varying(32),
    station_to character varying(32),
    external_org_id bigint,
    milestone_id bigint,
    external_contact text,
    internal_owner text,
    committed_date date,
    need_date date,
    evidence_required text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_ref character varying(64),
    resolution_strategy character varying(16),
    next_action text,
    action_due_date date,
    dismissed_at timestamp with time zone,
    action_due_date_reason character varying(64),
    deferral_reason character varying(64),
    deferral_return_date date,
    milestone_registration_id bigint,
    cost_responsibility character varying(64),
    CONSTRAINT dep_type CHECK (((dep_type)::text = ANY (ARRAY[('utility_relocation'::character varying)::text, ('agreement'::character varying)::text, ('permit'::character varying)::text, ('row'::character varying)::text, ('railroad'::character varying)::text, ('access'::character varying)::text, ('other'::character varying)::text]))),
    CONSTRAINT resolution_strategy CHECK (((resolution_strategy)::text = ANY (ARRAY[('relocate'::character varying)::text, ('remove'::character varying)::text, ('abandon_in_place'::character varying)::text, ('adjust_vertical'::character varying)::text, ('protect_in_place'::character varying)::text, ('change_design'::character varying)::text, ('policy_exception'::character varying)::text])))
);


ALTER TABLE public.dependencies OWNER TO corridor;

--
-- Name: dependencies_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependencies_id_seq OWNER TO corridor;

--
-- Name: dependencies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependencies_id_seq OWNED BY public.dependencies.id;


--
-- Name: dependency_admission_outcomes; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_admission_outcomes (
    id bigint NOT NULL,
    policy_run_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    outcome character varying(9) NOT NULL,
    reason character varying(64),
    dependency_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'dependency-admission'::character varying NOT NULL,
    eligibility_json jsonb,
    eligibility_sha256 character varying(64),
    CONSTRAINT ck_dependency_admission_outcome_eligibility_sha256 CHECK (((eligibility_sha256 IS NULL) OR ((eligibility_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_dependency_admission_outcome_eligibility_shape CHECK (((((outcome)::text = 'abstained'::text) AND (((eligibility_json IS NULL) AND (eligibility_sha256 IS NULL)) OR ((eligibility_json IS NOT NULL) AND (eligibility_sha256 IS NOT NULL)))) OR (((outcome)::text = ANY (ARRAY[('admitted'::character varying)::text, ('merged'::character varying)::text])) AND (eligibility_json IS NULL) AND (eligibility_sha256 IS NULL)))),
    CONSTRAINT ck_dependency_admission_outcome_kind CHECK (((((outcome)::text = ANY (ARRAY[('admitted'::character varying)::text, ('merged'::character varying)::text])) AND (reason IS NULL) AND (dependency_id IS NOT NULL)) OR (((outcome)::text = 'abstained'::text) AND (reason IS NOT NULL) AND (dependency_id IS NULL)))),
    CONSTRAINT ck_dependency_admission_outcome_value CHECK (((outcome)::text = ANY (ARRAY[('admitted'::character varying)::text, ('merged'::character varying)::text, ('abstained'::character varying)::text]))),
    CONSTRAINT ck_dependency_admission_outcomes_family CHECK (((family)::text = 'dependency-admission'::text))
);


ALTER TABLE public.dependency_admission_outcomes OWNER TO corridor;

--
-- Name: dependency_admission_outcomes_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_admission_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_admission_outcomes_id_seq OWNER TO corridor;

--
-- Name: dependency_admission_outcomes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_admission_outcomes_id_seq OWNED BY public.dependency_admission_outcomes.id;


--
-- Name: dependency_dismissals; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_dismissals (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    reason character varying(32) NOT NULL,
    dismissed_by text NOT NULL,
    dismissed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_dismissals_attributable CHECK ((length(TRIM(BOTH FROM dismissed_by)) > 0)),
    CONSTRAINT ck_dependency_dismissals_reason CHECK (((reason)::text = ANY (ARRAY[('duplicate'::character varying)::text, ('not-a-conflict'::character varying)::text, ('wrong'::character varying)::text])))
);


ALTER TABLE public.dependency_dismissals OWNER TO corridor;

--
-- Name: dependency_dismissals_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_dismissals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_dismissals_id_seq OWNER TO corridor;

--
-- Name: dependency_dismissals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_dismissals_id_seq OWNED BY public.dependency_dismissals.id;


--
-- Name: dependency_event_evidence; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_event_evidence (
    evidence_link_id bigint NOT NULL,
    event_id bigint NOT NULL,
    recorded_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_event_evidence_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0))
);


ALTER TABLE public.dependency_event_evidence OWNER TO corridor;

--
-- Name: dependency_event_migration_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_event_migration_receipts (
    event_id bigint NOT NULL,
    original_event jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.dependency_event_migration_receipts OWNER TO corridor;

--
-- Name: dependency_event_scope_decisions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_event_scope_decisions (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    scope_mode character varying(16) NOT NULL,
    supersedes_scope_decision_id bigint,
    decided_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_event_scope_decisions_actor CHECK ((length(TRIM(BOTH FROM decided_by)) > 0)),
    CONSTRAINT ck_dependency_event_scope_decisions_mode CHECK (((scope_mode)::text = ANY (ARRAY[('unknown'::character varying)::text, ('selected'::character varying)::text, ('all_active'::character varying)::text, ('carried_forward'::character varying)::text])))
);


ALTER TABLE public.dependency_event_scope_decisions OWNER TO corridor;

--
-- Name: dependency_event_scope_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_event_scope_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_event_scope_decisions_id_seq OWNER TO corridor;

--
-- Name: dependency_event_scope_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_event_scope_decisions_id_seq OWNED BY public.dependency_event_scope_decisions.id;


--
-- Name: dependency_event_scopes; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_event_scopes (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    scope_decision_id bigint NOT NULL,
    recorded_by text NOT NULL
);


ALTER TABLE public.dependency_event_scopes OWNER TO corridor;

--
-- Name: dependency_event_scopes_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_event_scopes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_event_scopes_id_seq OWNER TO corridor;

--
-- Name: dependency_event_scopes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_event_scopes_id_seq OWNED BY public.dependency_event_scopes.id;


--
-- Name: dependency_event_timings; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_event_timings (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    kind character varying(16) NOT NULL,
    text text NOT NULL,
    "precision" character varying(32) NOT NULL,
    start_date date,
    end_date date,
    CONSTRAINT ck_dependency_event_timing_bounds CHECK ((((("precision")::text = 'day'::text) AND (start_date IS NOT NULL) AND (end_date = start_date)) OR ((("precision")::text = 'month'::text) AND (start_date IS NOT NULL) AND (end_date IS NOT NULL) AND (start_date = (date_trunc('month'::text, (start_date)::timestamp without time zone))::date) AND (end_date = ((date_trunc('month'::text, (start_date)::timestamp without time zone) + '1 mon -1 days'::interval))::date)) OR ((("precision")::text = ANY (ARRAY[('approximate'::character varying)::text, ('legacy_unknown'::character varying)::text])) AND (start_date IS NULL) AND (end_date IS NULL)))),
    CONSTRAINT ck_dependency_event_timing_kind CHECK (((kind)::text = ANY (ARRAY[('previous'::character varying)::text, ('new'::character varying)::text]))),
    CONSTRAINT ck_dependency_event_timing_precision CHECK ((("precision")::text = ANY (ARRAY[('day'::character varying)::text, ('month'::character varying)::text, ('approximate'::character varying)::text, ('legacy_unknown'::character varying)::text])))
);


ALTER TABLE public.dependency_event_timings OWNER TO corridor;

--
-- Name: dependency_event_timings_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_event_timings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_event_timings_id_seq OWNER TO corridor;

--
-- Name: dependency_event_timings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_event_timings_id_seq OWNED BY public.dependency_event_timings.id;


--
-- Name: dependency_events; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_events (
    id bigint NOT NULL,
    event_type character varying(32) NOT NULL,
    event_date date,
    description text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_kind character varying DEFAULT 'cited'::character varying NOT NULL,
    stated_party text,
    project_id bigint NOT NULL,
    affected_external_org_id bigint,
    stated_external_org_id bigint,
    scope_mode character varying(16) DEFAULT 'unknown'::character varying NOT NULL,
    timing_direction character varying(16),
    attribution_state character varying(16) DEFAULT 'unresolved'::character varying NOT NULL,
    commitment_lineage_id bigint,
    supersedes_event_id bigint,
    closes_commitment_lineage_id bigint,
    CONSTRAINT ck_dependency_events_attribution CHECK (((((attribution_state)::text = 'resolved'::text) AND (stated_external_org_id IS NOT NULL)) OR (((attribution_state)::text = 'unresolved'::text) AND (stated_external_org_id IS NULL)))),
    CONSTRAINT ck_dependency_events_scope_mode CHECK (((scope_mode)::text = ANY (ARRAY[('unknown'::character varying)::text, ('selected'::character varying)::text, ('all_active'::character varying)::text, ('carried_forward'::character varying)::text]))),
    CONSTRAINT ck_dependency_events_source_kind CHECK (((source_kind)::text = ANY (ARRAY[('cited'::character varying)::text, ('verbal'::character varying)::text]))),
    CONSTRAINT ck_dependency_events_timing_direction CHECK (((timing_direction IS NULL) OR ((timing_direction)::text = ANY (ARRAY[('earlier'::character varying)::text, ('later'::character varying)::text, ('unknown'::character varying)::text])))),
    CONSTRAINT event_type CHECK (((event_type)::text = ANY (ARRAY[('commitment'::character varying)::text, ('committed_date_change'::character varying)::text, ('response'::character varying)::text, ('escalation'::character varying)::text, ('status_change'::character varying)::text, ('closure'::character varying)::text])))
);


ALTER TABLE public.dependency_events OWNER TO corridor;

--
-- Name: dependency_events_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_events_id_seq OWNER TO corridor;

--
-- Name: dependency_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_events_id_seq OWNED BY public.dependency_events.id;


--
-- Name: dependency_evidence_sufficiencies; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dependency_evidence_sufficiencies (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    evidence_link_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_link_id bigint
);


ALTER TABLE public.dependency_evidence_sufficiencies OWNER TO corridor;

--
-- Name: dependency_evidence_sufficiencies_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dependency_evidence_sufficiencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dependency_evidence_sufficiencies_id_seq OWNER TO corridor;

--
-- Name: dependency_evidence_sufficiencies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dependency_evidence_sufficiencies_id_seq OWNED BY public.dependency_evidence_sufficiencies.id;


--
-- Name: discovered_references; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.discovered_references (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    location_id character varying(128) NOT NULL,
    reference_key character varying(64) NOT NULL,
    source_url text NOT NULL,
    archive_url text,
    member text,
    observed_title text,
    observed_type_hint character varying(64),
    first_observed_at timestamp with time zone NOT NULL,
    last_observed_at timestamp with time zone NOT NULL,
    observed_count integer DEFAULT 1 NOT NULL,
    state character varying(10) DEFAULT 'proposed'::character varying NOT NULL,
    authorized_doc_type character varying(32),
    authorized_registry_id character varying(128),
    authorized_by character varying(128),
    authorized_at timestamp with time zone,
    registered_document_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_discovered_reference_authorized_doc_type CHECK (((authorized_doc_type IS NULL) OR ((authorized_doc_type)::text = ANY (ARRAY[('matrix'::character varying)::text, ('minutes'::character varying)::text, ('agreement'::character varying)::text, ('email'::character varying)::text, ('plan'::character varying)::text, ('schedule'::character varying)::text, ('spec'::character varying)::text, ('status_report'::character varying)::text, ('other'::character varying)::text])))),
    CONSTRAINT ck_discovered_reference_observed_count CHECK ((observed_count >= 1)),
    CONSTRAINT ck_discovered_reference_state CHECK (((((state)::text = 'proposed'::text) AND (authorized_at IS NULL) AND (registered_document_id IS NULL)) OR (((state)::text = 'authorized'::text) AND (authorized_at IS NOT NULL) AND (authorized_doc_type IS NOT NULL)) OR (((state)::text = 'registered'::text) AND (authorized_at IS NOT NULL) AND (registered_document_id IS NOT NULL)))),
    CONSTRAINT discovered_reference_state CHECK (((state)::text = ANY (ARRAY[('proposed'::character varying)::text, ('authorized'::character varying)::text, ('registered'::character varying)::text])))
);


ALTER TABLE public.discovered_references OWNER TO corridor;

--
-- Name: discovered_references_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.discovered_references_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.discovered_references_id_seq OWNER TO corridor;

--
-- Name: discovered_references_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.discovered_references_id_seq OWNED BY public.discovered_references.id;


--
-- Name: dispute_history_resolutions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dispute_history_resolutions (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    field_name character varying(64) NOT NULL,
    older_assertion_id bigint NOT NULL,
    newer_assertion_id bigint NOT NULL,
    covers_assertion_id bigint NOT NULL,
    outcome character varying(32) NOT NULL,
    rule_version character varying(64) NOT NULL,
    why text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dispute_history_resolutions_distinct_assertions CHECK ((older_assertion_id <> newer_assertion_id)),
    CONSTRAINT ck_dispute_history_resolutions_outcome CHECK (((outcome)::text = ANY (ARRAY[('physical_superseded'::character varying)::text, ('contractual_amendment'::character varying)::text]))),
    CONSTRAINT ck_dispute_history_resolutions_rule_version CHECK ((length(TRIM(BOTH FROM rule_version)) > 0))
);


ALTER TABLE public.dispute_history_resolutions OWNER TO corridor;

--
-- Name: dispute_history_resolutions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dispute_history_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dispute_history_resolutions_id_seq OWNER TO corridor;

--
-- Name: dispute_history_resolutions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dispute_history_resolutions_id_seq OWNED BY public.dispute_history_resolutions.id;


--
-- Name: dispute_settlements; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.dispute_settlements (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    field_name character varying(64) NOT NULL,
    settled_value text,
    settled_by text NOT NULL,
    covers_assertion_id bigint NOT NULL,
    settled_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dispute_settlements_attributable CHECK ((length(TRIM(BOTH FROM settled_by)) > 0))
);


ALTER TABLE public.dispute_settlements OWNER TO corridor;

--
-- Name: dispute_settlements_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.dispute_settlements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.dispute_settlements_id_seq OWNER TO corridor;

--
-- Name: dispute_settlements_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.dispute_settlements_id_seq OWNED BY public.dispute_settlements.id;


--
-- Name: doc_pages; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.doc_pages (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    text text NOT NULL,
    image_path text,
    text_source character varying(10) DEFAULT 'text_layer'::character varying NOT NULL,
    inventory_json jsonb,
    routing_json jsonb,
    CONSTRAINT ck_doc_pages_inventory_routing_pair CHECK (((inventory_json IS NULL) = (routing_json IS NULL))),
    CONSTRAINT text_source CHECK (((text_source)::text = ANY (ARRAY[('text_layer'::character varying)::text, ('ocr'::character varying)::text, ('cells'::character varying)::text])))
);


ALTER TABLE public.doc_pages OWNER TO corridor;

--
-- Name: doc_pages_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.doc_pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.doc_pages_id_seq OWNER TO corridor;

--
-- Name: doc_pages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.doc_pages_id_seq OWNED BY public.doc_pages.id;


--
-- Name: document_notification_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.document_notification_attempts (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    dispatch_id bigint NOT NULL,
    project_id bigint NOT NULL,
    attempt_number integer NOT NULL,
    outcome character varying(24) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    error_code character varying(64),
    runtime_owner character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_document_attempt_outcome CHECK (((outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text, ('skipped'::character varying)::text]))),
    CONSTRAINT ck_document_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_document_attempt_positive CHECK ((attempt_number > 0))
);


ALTER TABLE public.document_notification_attempts OWNER TO corridor;

--
-- Name: document_notification_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.document_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.document_notification_attempts_id_seq OWNER TO corridor;

--
-- Name: document_notification_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.document_notification_attempts_id_seq OWNED BY public.document_notification_attempts.id;


--
-- Name: document_notification_dispatches; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.document_notification_dispatches (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    notification_id bigint NOT NULL,
    project_id bigint NOT NULL,
    channel character varying(16) NOT NULL,
    delivery_state character varying(16) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error_code character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    idempotency_key character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_document_dispatch_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_document_dispatch_channel CHECK (((channel)::text = 'email'::text)),
    CONSTRAINT ck_document_dispatch_idempotency_hex CHECK (((idempotency_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_document_dispatch_retry_shape CHECK (((((delivery_state)::text = 'retry_due'::text) AND (next_attempt_at IS NOT NULL)) OR (((delivery_state)::text <> 'retry_due'::text) AND (next_attempt_at IS NULL)))),
    CONSTRAINT ck_document_dispatch_state CHECK (((delivery_state)::text = ANY (ARRAY[('queued'::character varying)::text, ('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text])))
);


ALTER TABLE public.document_notification_dispatches OWNER TO corridor;

--
-- Name: document_notification_dispatches_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.document_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.document_notification_dispatches_id_seq OWNER TO corridor;

--
-- Name: document_notification_dispatches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.document_notification_dispatches_id_seq OWNED BY public.document_notification_dispatches.id;


--
-- Name: document_notifications; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.document_notifications (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    project_id bigint NOT NULL,
    category character varying(32) NOT NULL,
    subject_kind character varying(16) NOT NULL,
    dependency_id bigint,
    commitment_lineage_id bigint,
    recipient_principal_subject character varying(128) NOT NULL,
    recipient_role character varying(48) NOT NULL,
    review_confirmation_id bigint,
    requirement_field character varying(64),
    reviewed_evidence_link_id bigint,
    original_reviewer_subject character varying(128),
    predecessor_document_id bigint,
    successor_document_id bigint,
    comparison_id bigint,
    finding_id bigint,
    statement_event_id bigint,
    reason_code character varying(64) NOT NULL,
    change_uncertain boolean DEFAULT false NOT NULL,
    source_context_json jsonb,
    occurrence_key character varying(64) NOT NULL,
    registered_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_document_notification_actor CHECK ((length(TRIM(BOTH FROM registered_by)) > 0)),
    CONSTRAINT ck_document_notification_authentic_source CHECK (((((category)::text = 'documentation_loss'::text) AND (review_confirmation_id IS NOT NULL)) OR (((category)::text = 'document_change'::text) AND ((comparison_id IS NOT NULL) OR (statement_event_id IS NOT NULL))))),
    CONSTRAINT ck_document_notification_category CHECK (((category)::text = ANY (ARRAY[('documentation_loss'::character varying)::text, ('document_change'::character varying)::text]))),
    CONSTRAINT ck_document_notification_key_hex CHECK (((occurrence_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_document_notification_recipient CHECK ((length(TRIM(BOTH FROM recipient_principal_subject)) > 0)),
    CONSTRAINT ck_document_notification_recipient_role CHECK (((recipient_role)::text = ANY (ARRAY[('current_assignee'::character varying)::text, ('original_reviewer'::character varying)::text, ('current_assignee_and_original_reviewer'::character varying)::text]))),
    CONSTRAINT ck_document_notification_subject_kind CHECK (((subject_kind)::text = ANY (ARRAY[('constraint'::character varying)::text, ('statement'::character varying)::text]))),
    CONSTRAINT ck_document_notification_subject_shape CHECK (((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL))))
);


ALTER TABLE public.document_notifications OWNER TO corridor;

--
-- Name: document_notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.document_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.document_notifications_id_seq OWNER TO corridor;

--
-- Name: document_notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.document_notifications_id_seq OWNED BY public.document_notifications.id;


--
-- Name: document_quarantines; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.document_quarantines (
    document_id bigint NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.document_quarantines OWNER TO corridor;

--
-- Name: document_rendition_derivations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.document_rendition_derivations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    derived_document_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    source_format character varying(16) NOT NULL,
    derived_format character varying(16) NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    derived_sha256 character varying(64) NOT NULL,
    tool character varying(64) NOT NULL,
    tool_version character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_rendition_derivation_distinct_documents CHECK ((source_document_id <> derived_document_id)),
    CONSTRAINT ck_rendition_derivation_hashes CHECK ((((source_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((derived_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_rendition_derivation_kind CHECK ((((kind)::text = 'format_conversion'::text) AND ((source_format)::text = 'xls'::text) AND ((derived_format)::text = 'xlsx'::text))),
    CONSTRAINT ck_rendition_derivation_tool CHECK (((length(TRIM(BOTH FROM tool)) > 0) AND (length(TRIM(BOTH FROM tool_version)) > 0)))
);


ALTER TABLE public.document_rendition_derivations OWNER TO corridor;

--
-- Name: document_rendition_derivations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.document_rendition_derivations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.document_rendition_derivations_id_seq OWNER TO corridor;

--
-- Name: document_rendition_derivations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.document_rendition_derivations_id_seq OWNED BY public.document_rendition_derivations.id;


--
-- Name: documentation_field_confirmations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.documentation_field_confirmations (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    evidence_link_id bigint NOT NULL,
    field_name character varying(64) NOT NULL,
    classification character varying(64) NOT NULL,
    conclusion character varying(64) NOT NULL,
    confirmed_by text NOT NULL,
    confirmed_at timestamp with time zone DEFAULT now() NOT NULL,
    condition_immaterial boolean DEFAULT false NOT NULL,
    overridden_condition_text text,
    CONSTRAINT ck_documentation_confirmation_actor CHECK ((length(TRIM(BOTH FROM confirmed_by)) > 0)),
    CONSTRAINT ck_documentation_confirmation_known_classification CHECK (((classification)::text = ANY (ARRAY[('approved'::character varying)::text, ('conditional'::character varying)::text]))),
    CONSTRAINT ck_documentation_confirmation_known_conclusion CHECK (((conclusion)::text = 'approved'::text)),
    CONSTRAINT ck_documentation_confirmation_known_field CHECK (((field_name)::text = 'approval_interpretation'::text)),
    CONSTRAINT ck_documentation_confirmation_override_records_hedge CHECK (((condition_immaterial = false) OR (((classification)::text = 'conditional'::text) AND (overridden_condition_text IS NOT NULL) AND (length(TRIM(BOTH FROM overridden_condition_text)) > 0))))
);


ALTER TABLE public.documentation_field_confirmations OWNER TO corridor;

--
-- Name: documentation_field_confirmations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.documentation_field_confirmations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.documentation_field_confirmations_id_seq OWNER TO corridor;

--
-- Name: documentation_field_confirmations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.documentation_field_confirmations_id_seq OWNED BY public.documentation_field_confirmations.id;


--
-- Name: documents; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.documents (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    filename text NOT NULL,
    doc_type character varying(13) NOT NULL,
    source_url text,
    retrieved_at timestamp with time zone,
    doc_date date,
    pages integer,
    parse_status character varying(7) DEFAULT 'pending'::character varying NOT NULL,
    superseded_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    extraction_tiers jsonb,
    header_disagreements integer,
    registry_id character varying(128),
    superseded_on date,
    supersession_source_document_id bigint,
    supersession_source_page integer,
    numbering_scheme character varying(32) DEFAULT 'project-unique'::character varying NOT NULL,
    CONSTRAINT ck_documents_complete_supersession CHECK ((((superseded_by IS NULL) AND (superseded_on IS NULL) AND (supersession_source_document_id IS NULL) AND (supersession_source_page IS NULL)) OR ((superseded_by IS NOT NULL) AND (registry_id IS NOT NULL) AND (superseded_on IS NOT NULL) AND (supersession_source_document_id IS NOT NULL) AND (supersession_source_page IS NOT NULL) AND (supersession_source_page > 0)))),
    CONSTRAINT ck_documents_no_self_attested_supersession CHECK (((supersession_source_document_id IS NULL) OR (supersession_source_document_id <> id))),
    CONSTRAINT ck_documents_no_self_supersession CHECK (((superseded_by IS NULL) OR (superseded_by <> id))),
    CONSTRAINT ck_documents_numbering_scheme CHECK (((numbering_scheme)::text = ANY (ARRAY[('project-unique'::character varying)::text, ('per-party'::character varying)::text]))),
    CONSTRAINT doc_type CHECK (((doc_type)::text = ANY (ARRAY[('matrix'::character varying)::text, ('minutes'::character varying)::text, ('agreement'::character varying)::text, ('email'::character varying)::text, ('plan'::character varying)::text, ('schedule'::character varying)::text, ('spec'::character varying)::text, ('status_report'::character varying)::text, ('other'::character varying)::text]))),
    CONSTRAINT parse_status CHECK (((parse_status)::text = ANY (ARRAY[('pending'::character varying)::text, ('parsed'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.documents OWNER TO corridor;

--
-- Name: documents_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.documents_id_seq OWNER TO corridor;

--
-- Name: documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;


--
-- Name: due_action_notification_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_action_notification_attempts (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    dispatch_id bigint NOT NULL,
    project_id bigint NOT NULL,
    attempt_number integer NOT NULL,
    outcome character varying(24) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    error_code character varying(64),
    runtime_owner character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_action_attempt_outcome CHECK (((outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text, ('skipped'::character varying)::text]))),
    CONSTRAINT ck_due_action_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_due_action_attempt_positive CHECK ((attempt_number > 0))
);


ALTER TABLE public.due_action_notification_attempts OWNER TO corridor;

--
-- Name: due_action_notification_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_action_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_action_notification_attempts_id_seq OWNER TO corridor;

--
-- Name: due_action_notification_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_action_notification_attempts_id_seq OWNED BY public.due_action_notification_attempts.id;


--
-- Name: due_action_notification_dispatches; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_action_notification_dispatches (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    notification_id bigint NOT NULL,
    project_id bigint NOT NULL,
    channel character varying(16) NOT NULL,
    delivery_state character varying(16) NOT NULL,
    recipient_contact text,
    delivery_limitation character varying(64),
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_error_code character varying(64),
    provider_message_id character varying(200),
    provider_result_json jsonb,
    idempotency_key character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_action_dispatch_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_due_action_dispatch_channel CHECK (((channel)::text = 'email'::text)),
    CONSTRAINT ck_due_action_dispatch_idempotency_hex CHECK (((idempotency_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_due_action_dispatch_retry_shape CHECK (((((delivery_state)::text = 'retry_due'::text) AND (next_attempt_at IS NOT NULL)) OR (((delivery_state)::text <> 'retry_due'::text) AND (next_attempt_at IS NULL)))),
    CONSTRAINT ck_due_action_dispatch_state CHECK (((delivery_state)::text = ANY (ARRAY[('queued'::character varying)::text, ('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text, ('uncertain'::character varying)::text])))
);


ALTER TABLE public.due_action_notification_dispatches OWNER TO corridor;

--
-- Name: due_action_notification_dispatches_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_action_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_action_notification_dispatches_id_seq OWNER TO corridor;

--
-- Name: due_action_notification_dispatches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_action_notification_dispatches_id_seq OWNED BY public.due_action_notification_dispatches.id;


--
-- Name: due_action_notifications; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_action_notifications (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    project_id bigint NOT NULL,
    category character varying(32) NOT NULL,
    subject_kind character varying(16),
    dependency_id bigint,
    commitment_lineage_id bigint,
    plan_decision_id bigint,
    urgency character varying(16),
    action_due_date date,
    check_identity character varying(128),
    observation_start date,
    observation_end date,
    summary_json jsonb,
    recipient_role character varying(16) NOT NULL,
    recipient_roster_entry_id bigint NOT NULL,
    recipient_principal_subject character varying(128) NOT NULL,
    configuration_version character varying(64) NOT NULL,
    occurrence_key character varying(64) NOT NULL,
    registered_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_action_notification_actor CHECK ((length(TRIM(BOTH FROM registered_by)) > 0)),
    CONSTRAINT ck_due_action_notification_category CHECK (((category)::text = ANY (ARRAY[('next_action_due'::character varying)::text, ('next_action_escalation'::character varying)::text, ('daily_summary'::character varying)::text]))),
    CONSTRAINT ck_due_action_notification_key_hex CHECK (((occurrence_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_due_action_notification_recipient CHECK ((length(TRIM(BOTH FROM recipient_principal_subject)) > 0)),
    CONSTRAINT ck_due_action_notification_role CHECK (((recipient_role)::text = ANY (ARRAY[('assignee'::character varying)::text, ('escalation'::character varying)::text, ('summary'::character varying)::text]))),
    CONSTRAINT ck_due_action_notification_shape CHECK (((((category)::text = 'daily_summary'::text) AND (subject_kind IS NULL) AND (dependency_id IS NULL) AND (commitment_lineage_id IS NULL) AND (plan_decision_id IS NULL) AND (urgency IS NULL) AND (action_due_date IS NULL) AND ((recipient_role)::text = 'summary'::text) AND (observation_start IS NOT NULL) AND (observation_end IS NOT NULL)) OR (((category)::text = ANY (ARRAY[('next_action_due'::character varying)::text, ('next_action_escalation'::character varying)::text])) AND ((subject_kind)::text = ANY (ARRAY[('constraint'::character varying)::text, ('statement'::character varying)::text])) AND (plan_decision_id IS NOT NULL) AND (urgency IS NOT NULL) AND ((recipient_role)::text = ANY (ARRAY[('assignee'::character varying)::text, ('escalation'::character varying)::text])) AND ((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL)))))),
    CONSTRAINT ck_due_action_notification_urgency CHECK (((urgency IS NULL) OR ((urgency)::text = ANY (ARRAY[('soon'::character varying)::text, ('overdue'::character varying)::text, ('urgent_overdue'::character varying)::text]))))
);


ALTER TABLE public.due_action_notifications OWNER TO corridor;

--
-- Name: due_action_notifications_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_action_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_action_notifications_id_seq OWNER TO corridor;

--
-- Name: due_action_notifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_action_notifications_id_seq OWNED BY public.due_action_notifications.id;


--
-- Name: due_work_occurrences; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_work_occurrences (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    scheduled_job_id bigint NOT NULL,
    occurrence_key character varying(64) NOT NULL,
    due_at timestamp with time zone NOT NULL,
    state character varying(16) NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    owner character varying(128),
    claim_token character varying(64),
    claimed_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    deadline_at timestamp with time zone,
    last_error_code character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_work_occurrence_attempt_count CHECK ((attempt_count >= 0)),
    CONSTRAINT ck_due_work_occurrence_claim_shape CHECK (((((state)::text = 'claimed'::text) AND (owner IS NOT NULL) AND (claim_token IS NOT NULL) AND (lease_expires_at IS NOT NULL) AND (claimed_at IS NOT NULL) AND (deadline_at IS NOT NULL)) OR (((state)::text <> 'claimed'::text) AND (owner IS NULL) AND (claim_token IS NULL) AND (lease_expires_at IS NULL) AND (claimed_at IS NULL) AND (deadline_at IS NULL)))),
    CONSTRAINT ck_due_work_occurrence_retry_shape CHECK (((((state)::text = 'retry_due'::text) AND (next_attempt_at IS NOT NULL)) OR (((state)::text <> 'retry_due'::text) AND (next_attempt_at IS NULL)))),
    CONSTRAINT ck_due_work_occurrence_state CHECK (((state)::text = ANY (ARRAY[('pending'::character varying)::text, ('claimed'::character varying)::text, ('retry_due'::character varying)::text, ('completed'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.due_work_occurrences OWNER TO corridor;

--
-- Name: due_work_occurrences_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_work_occurrences_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_work_occurrences_id_seq OWNER TO corridor;

--
-- Name: due_work_occurrences_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_work_occurrences_id_seq OWNED BY public.due_work_occurrences.id;


--
-- Name: due_work_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_work_receipts (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    occurrence_id bigint NOT NULL,
    project_id bigint NOT NULL,
    handler_key character varying(64) NOT NULL,
    attempt_number integer NOT NULL,
    attempt_id character varying(64) NOT NULL,
    runtime_owner character varying(128) NOT NULL,
    execution_outcome character varying(16) NOT NULL,
    handler_result_json jsonb,
    error_code character varying(64),
    safe_next_step character varying(128) NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_work_receipt_attempt CHECK (((attempt_number > 0) AND (finished_at >= started_at))),
    CONSTRAINT ck_due_work_receipt_content_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_due_work_receipt_outcome CHECK (((execution_outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('retry_due'::character varying)::text, ('failed'::character varying)::text]))),
    CONSTRAINT ck_due_work_receipt_result_shape CHECK (((((execution_outcome)::text = 'completed'::text) AND (handler_result_json IS NOT NULL) AND (error_code IS NULL)) OR (((execution_outcome)::text = ANY (ARRAY[('retry_due'::character varying)::text, ('failed'::character varying)::text])) AND (handler_result_json IS NULL) AND (error_code IS NOT NULL))))
);


ALTER TABLE public.due_work_receipts OWNER TO corridor;

--
-- Name: due_work_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_work_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_work_receipts_id_seq OWNER TO corridor;

--
-- Name: due_work_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_work_receipts_id_seq OWNED BY public.due_work_receipts.id;


--
-- Name: due_work_schedules; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.due_work_schedules (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    project_id bigint NOT NULL,
    handler_key character varying(64) NOT NULL,
    configuration_version character varying(64) NOT NULL,
    scope_json jsonb NOT NULL,
    configuration_json jsonb NOT NULL,
    configuration_sha256 character varying(64) NOT NULL,
    input_identity_sha256 character varying(64) NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    cadence character varying(32) NOT NULL,
    timezone_name character varying(64) NOT NULL,
    missed_run_policy character varying(32) NOT NULL,
    retention_days integer NOT NULL,
    max_attempts integer NOT NULL,
    backoff_seconds integer NOT NULL,
    claim_ttl_seconds integer NOT NULL,
    deadline_seconds integer NOT NULL,
    concurrency_limit integer NOT NULL,
    model_token_budget integer NOT NULL,
    notification_budget integer NOT NULL,
    enabled_at timestamp with time zone NOT NULL,
    disabled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_due_work_schedule_budgets CHECK (((retention_days > 0) AND (max_attempts > 0) AND (backoff_seconds >= 0) AND (claim_ttl_seconds > 0) AND (deadline_seconds > 0) AND (concurrency_limit > 0) AND (model_token_budget >= 0) AND (notification_budget >= 0))),
    CONSTRAINT ck_due_work_schedule_configuration_sha256 CHECK (((configuration_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_due_work_schedule_disable_order CHECK (((disabled_at IS NULL) OR (disabled_at >= enabled_at))),
    CONSTRAINT ck_due_work_schedule_input_identity_sha256 CHECK (((input_identity_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.due_work_schedules OWNER TO corridor;

--
-- Name: due_work_schedules_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.due_work_schedules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.due_work_schedules_id_seq OWNER TO corridor;

--
-- Name: due_work_schedules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.due_work_schedules_id_seq OWNED BY public.due_work_schedules.id;


--
-- Name: event_admission_acceptance_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.event_admission_acceptance_receipts (
    id bigint NOT NULL,
    project_id integer NOT NULL,
    status character varying(16) NOT NULL,
    source_revision character varying(64) NOT NULL,
    migration_head character varying(64) NOT NULL,
    predecessor_policy_version character varying(64) NOT NULL,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    reason_version character varying(64) NOT NULL,
    selection_rule character varying(128) NOT NULL,
    receipt_json jsonb NOT NULL,
    receipt_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_admission_acceptance_policy_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_event_admission_acceptance_receipt_sha256 CHECK (((receipt_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_event_admission_acceptance_status CHECK (((status)::text = ANY (ARRAY[('passed'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.event_admission_acceptance_receipts OWNER TO corridor;

--
-- Name: event_admission_acceptance_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.event_admission_acceptance_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.event_admission_acceptance_receipts_id_seq OWNER TO corridor;

--
-- Name: event_admission_acceptance_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.event_admission_acceptance_receipts_id_seq OWNED BY public.event_admission_acceptance_receipts.id;


--
-- Name: event_admission_activations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.event_admission_activations (
    id bigint NOT NULL,
    project_id integer NOT NULL,
    acceptance_receipt_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    policy_version character varying(64) NOT NULL,
    reason character varying(128) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_admission_activation_action CHECK (((action)::text = ANY (ARRAY[('activate'::character varying)::text, ('suspend'::character varying)::text]))),
    CONSTRAINT ck_event_admission_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_event_admission_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0))
);


ALTER TABLE public.event_admission_activations OWNER TO corridor;

--
-- Name: event_admission_activations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.event_admission_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.event_admission_activations_id_seq OWNER TO corridor;

--
-- Name: event_admission_activations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.event_admission_activations_id_seq OWNED BY public.event_admission_activations.id;


--
-- Name: event_admission_outcomes; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.event_admission_outcomes (
    id bigint NOT NULL,
    policy_run_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    outcome character varying(9) NOT NULL,
    reason character varying(64),
    dependency_event_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'event-admission'::character varying NOT NULL,
    commitment_lineage_id bigint,
    scope_decision_id bigint,
    candidate_disposition_id bigint,
    audit_log_id bigint,
    eligibility_json jsonb,
    eligibility_sha256 character varying(64),
    CONSTRAINT ck_event_admission_outcome_eligibility_sha256 CHECK (((eligibility_sha256 IS NULL) OR ((eligibility_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_event_admission_outcome_kind CHECK (((((outcome)::text = 'admitted'::text) AND (reason IS NULL) AND (dependency_event_id IS NOT NULL)) OR (((outcome)::text = 'abstained'::text) AND (reason IS NOT NULL) AND (dependency_event_id IS NULL)))),
    CONSTRAINT ck_event_admission_outcome_value CHECK (((outcome)::text = ANY (ARRAY[('admitted'::character varying)::text, ('abstained'::character varying)::text]))),
    CONSTRAINT ck_event_admission_outcomes_family CHECK (((family)::text = 'event-admission'::text))
);


ALTER TABLE public.event_admission_outcomes OWNER TO corridor;

--
-- Name: event_admission_outcomes_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.event_admission_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.event_admission_outcomes_id_seq OWNER TO corridor;

--
-- Name: event_admission_outcomes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.event_admission_outcomes_id_seq OWNED BY public.event_admission_outcomes.id;


--
-- Name: event_cohort_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.event_cohort_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    rule_version character varying(64) NOT NULL,
    input_run_ids jsonb NOT NULL,
    members jsonb NOT NULL,
    member_count integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.event_cohort_receipts OWNER TO corridor;

--
-- Name: event_cohort_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.event_cohort_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.event_cohort_receipts_id_seq OWNER TO corridor;

--
-- Name: event_cohort_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.event_cohort_receipts_id_seq OWNED BY public.event_cohort_receipts.id;


--
-- Name: evidence_investigation_candidate_review_starts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_candidate_review_starts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    principal character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL
);


ALTER TABLE public.evidence_investigation_candidate_review_starts OWNER TO corridor;

--
-- Name: evidence_investigation_candidate_review_starts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_candidate_review_starts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq OWNED BY public.evidence_investigation_candidate_review_starts.id;


--
-- Name: evidence_investigation_capture_contracts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_capture_contracts (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    cohort_id character varying(128) NOT NULL,
    contract_sha256 character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    prompt_sha256 character varying(64) NOT NULL,
    adapter_contract_version character varying(128) NOT NULL,
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    baseline_identity character varying(128) NOT NULL,
    window_start timestamp with time zone NOT NULL,
    cutoff_at timestamp with time zone NOT NULL,
    protection_end timestamp with time zone NOT NULL,
    history_retained_from timestamp with time zone NOT NULL,
    missing_label_policy character varying(32) NOT NULL,
    member_case_public_ids_json jsonb NOT NULL,
    contract_json jsonb NOT NULL,
    declared_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_capture_contract_actor CHECK ((length(TRIM(BOTH FROM declared_by)) > 0)),
    CONSTRAINT ck_capture_contract_missing_label_policy CHECK (((missing_label_policy)::text = 'remain_missing'::text)),
    CONSTRAINT ck_capture_contract_window_before_cutoff CHECK ((window_start <= cutoff_at))
);


ALTER TABLE public.evidence_investigation_capture_contracts OWNER TO corridor;

--
-- Name: evidence_investigation_capture_contracts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_capture_contracts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_capture_contracts_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_capture_contracts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_capture_contracts_id_seq OWNED BY public.evidence_investigation_capture_contracts.id;


--
-- Name: evidence_investigation_capture_results; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_capture_results (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    capture_contract_id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    run_id bigint,
    cutoff_at timestamp with time zone NOT NULL,
    executed_at timestamp with time zone NOT NULL,
    completeness character varying(16) NOT NULL,
    incomplete_reason character varying(64),
    candidate_disposition character varying(32),
    scope_mode character varying(32),
    selected_dependency_ids_json jsonb NOT NULL,
    correction boolean NOT NULL,
    undo boolean NOT NULL,
    unresolved boolean NOT NULL,
    human_outcome_identity character varying(64),
    outcome_identities_json jsonb NOT NULL,
    strata_json jsonb NOT NULL,
    review_seconds double precision,
    association_sha256 character varying(64) NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_capture_result_completeness CHECK (((completeness)::text = ANY (ARRAY[('complete'::character varying)::text, ('incomplete'::character varying)::text]))),
    CONSTRAINT ck_capture_result_incomplete_reason CHECK (((((completeness)::text = 'complete'::text) AND (incomplete_reason IS NULL)) OR (((completeness)::text = 'incomplete'::text) AND (incomplete_reason IS NOT NULL))))
);


ALTER TABLE public.evidence_investigation_capture_results OWNER TO corridor;

--
-- Name: evidence_investigation_capture_results_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_capture_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_capture_results_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_capture_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_capture_results_id_seq OWNED BY public.evidence_investigation_capture_results.id;


--
-- Name: evidence_investigation_evaluation_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_evaluation_receipts (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    evaluation_version character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    selected_run_ids_json jsonb NOT NULL,
    identity_json jsonb NOT NULL,
    metrics_json jsonb NOT NULL,
    strata_json jsonb NOT NULL,
    human_scores_json jsonb NOT NULL,
    gates_json jsonb NOT NULL,
    limitations_json jsonb NOT NULL,
    summary_markdown text NOT NULL,
    receipt_sha256 character varying(64) NOT NULL,
    evaluated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.evidence_investigation_evaluation_receipts OWNER TO corridor;

--
-- Name: evidence_investigation_evaluation_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_evaluation_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq OWNED BY public.evidence_investigation_evaluation_receipts.id;


--
-- Name: evidence_investigation_packet_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_packet_receipts (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    packet_json jsonb NOT NULL,
    validator_outcome character varying(32) NOT NULL,
    packet_sha256 character varying(64) NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL
);


ALTER TABLE public.evidence_investigation_packet_receipts OWNER TO corridor;

--
-- Name: evidence_investigation_packet_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_packet_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_packet_receipts_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_packet_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_packet_receipts_id_seq OWNED BY public.evidence_investigation_packet_receipts.id;


--
-- Name: evidence_investigation_review_observations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_review_observations (
    id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    boundary character varying(16) NOT NULL,
    principal character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_shadow_review_boundary CHECK (((boundary)::text = ANY (ARRAY[('start'::character varying)::text, ('end'::character varying)::text])))
);


ALTER TABLE public.evidence_investigation_review_observations OWNER TO corridor;

--
-- Name: evidence_investigation_review_observations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_review_observations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_review_observations_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_review_observations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_review_observations_id_seq OWNED BY public.evidence_investigation_review_observations.id;


--
-- Name: evidence_investigation_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_runs (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    extraction_run_id bigint,
    terminal_status character varying(32) NOT NULL,
    reason character varying(128),
    detail text,
    adapter character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    transport_gate_sha256 character varying(64) NOT NULL,
    candidate_payload_sha256 character varying(64) NOT NULL,
    read_fingerprint character varying(64),
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone NOT NULL,
    adapter_contract_version character varying(128),
    prompt_sha256 character varying(64),
    CONSTRAINT ck_evidence_investigation_runs_hashes CHECK (((length((candidate_payload_sha256)::text) = 64) AND (length((transport_gate_sha256)::text) = 64))),
    CONSTRAINT ck_evidence_investigation_runs_terminal_status CHECK (((terminal_status)::text = ANY (ARRAY[('options_available'::character varying)::text, ('human_judgment_needed'::character varying)::text, ('abstained'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.evidence_investigation_runs OWNER TO corridor;

--
-- Name: evidence_investigation_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_runs_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_runs_id_seq OWNED BY public.evidence_investigation_runs.id;


--
-- Name: evidence_investigation_shadow_cases; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_shadow_cases (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    extraction_run_id bigint,
    candidate_payload_sha256 character varying(64) NOT NULL,
    read_fingerprint character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    case_json jsonb NOT NULL,
    registered_evidence_json jsonb NOT NULL,
    option_population_json jsonb NOT NULL,
    option_population_sha256 character varying(64) NOT NULL,
    frozen_at timestamp with time zone NOT NULL,
    prompt_sha256 character varying(64),
    adapter_contract_version character varying(128),
    transport_gate_sha256 character varying(64),
    budget_json jsonb,
    tool_contract_version character varying(128)
);


ALTER TABLE public.evidence_investigation_shadow_cases OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_cases_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_shadow_cases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_shadow_cases_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_cases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_shadow_cases_id_seq OWNED BY public.evidence_investigation_shadow_cases.id;


--
-- Name: evidence_investigation_shadow_executions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_shadow_executions (
    id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    run_id bigint NOT NULL,
    execution_status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.evidence_investigation_shadow_executions OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_executions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_shadow_executions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_shadow_executions_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_executions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_shadow_executions_id_seq OWNED BY public.evidence_investigation_shadow_executions.id;


--
-- Name: evidence_investigation_shadow_outcomes; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_shadow_outcomes (
    id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    candidate_disposition character varying(32),
    scope_mode character varying(32),
    selected_dependency_ids_json jsonb NOT NULL,
    correction boolean NOT NULL,
    undo boolean NOT NULL,
    unresolved boolean NOT NULL,
    outcome_identities_json jsonb NOT NULL,
    strata_json jsonb NOT NULL,
    review_seconds double precision,
    outcome_sha256 character varying(64) NOT NULL,
    captured_at timestamp with time zone NOT NULL,
    human_outcome_identity character varying(64) NOT NULL
);


ALTER TABLE public.evidence_investigation_shadow_outcomes OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_outcomes_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_shadow_outcomes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq OWNED BY public.evidence_investigation_shadow_outcomes.id;


--
-- Name: evidence_investigation_step_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_investigation_step_receipts (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    ordinal integer NOT NULL,
    step_type character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    opaque_references_json jsonb NOT NULL,
    normalized_arguments_json jsonb NOT NULL,
    result_summary_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    elapsed_ms integer NOT NULL,
    request_sha256 character varying(64) NOT NULL,
    result_sha256 character varying(64) NOT NULL
);


ALTER TABLE public.evidence_investigation_step_receipts OWNER TO corridor;

--
-- Name: evidence_investigation_step_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_investigation_step_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_investigation_step_receipts_id_seq OWNER TO corridor;

--
-- Name: evidence_investigation_step_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_investigation_step_receipts_id_seq OWNED BY public.evidence_investigation_step_receipts.id;


--
-- Name: evidence_links; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.evidence_links (
    id bigint NOT NULL,
    dependency_id bigint,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    quote text NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.evidence_links OWNER TO corridor;

--
-- Name: evidence_links_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.evidence_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.evidence_links_id_seq OWNER TO corridor;

--
-- Name: evidence_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.evidence_links_id_seq OWNED BY public.evidence_links.id;


--
-- Name: external_orgs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.external_orgs (
    id bigint NOT NULL,
    name text NOT NULL,
    org_type character varying(10) DEFAULT 'utility'::character varying NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT org_type CHECK (((org_type)::text = ANY (ARRAY[('utility'::character varying)::text, ('railroad'::character varying)::text, ('agency'::character varying)::text, ('consultant'::character varying)::text, ('other'::character varying)::text])))
);


ALTER TABLE public.external_orgs OWNER TO corridor;

--
-- Name: external_orgs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.external_orgs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.external_orgs_id_seq OWNER TO corridor;

--
-- Name: external_orgs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.external_orgs_id_seq OWNED BY public.external_orgs.id;


--
-- Name: external_report_artifacts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.external_report_artifacts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    artifact_name text NOT NULL,
    format character varying(16) DEFAULT 'pdf'::character varying NOT NULL,
    pdf_bytes bytea NOT NULL,
    pdf_sha256 character varying(64) NOT NULL,
    evaluated_on date NOT NULL,
    ruleset_version character varying(64) NOT NULL,
    evaluation_context_json jsonb NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    record_context_json jsonb NOT NULL,
    rendered_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_external_report_artifacts_artifact_name CHECK ((length(TRIM(BOTH FROM artifact_name)) > 0)),
    CONSTRAINT ck_external_report_artifacts_context_object CHECK ((jsonb_typeof(record_context_json) = 'object'::text)),
    CONSTRAINT ck_external_report_artifacts_evaluation_object CHECK ((jsonb_typeof(evaluation_context_json) = 'object'::text)),
    CONSTRAINT ck_external_report_artifacts_nonempty_pdf CHECK ((octet_length(pdf_bytes) > 5)),
    CONSTRAINT ck_external_report_artifacts_pdf_only CHECK (((format)::text = 'pdf'::text)),
    CONSTRAINT ck_external_report_artifacts_pdf_sha256 CHECK (((pdf_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_external_report_artifacts_provenance_mode CHECK (((provenance_mode)::text = ANY (ARRAY[('all-supported-sources'::character varying)::text, ('document-only'::character varying)::text])))
);


ALTER TABLE public.external_report_artifacts OWNER TO corridor;

--
-- Name: external_report_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.external_report_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.external_report_artifacts_id_seq OWNER TO corridor;

--
-- Name: external_report_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.external_report_artifacts_id_seq OWNED BY public.external_report_artifacts.id;


--
-- Name: external_report_releases; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.external_report_releases (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    artifact_name text NOT NULL,
    format character varying(16) DEFAULT 'pdf'::character varying NOT NULL,
    pdf_bytes bytea,
    pdf_sha256 character varying(64) NOT NULL,
    evaluated_on date NOT NULL,
    ruleset_version character varying(64) NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    record_context_json jsonb,
    released_by character varying(128) NOT NULL,
    released_at timestamp with time zone DEFAULT now() NOT NULL,
    evaluation_context_json jsonb,
    artifact_id bigint,
    released_by_display text,
    content_storage character varying(16) DEFAULT 'artifact'::character varying NOT NULL,
    CONSTRAINT ck_external_report_releases_artifact_name CHECK ((length(TRIM(BOTH FROM artifact_name)) > 0)),
    CONSTRAINT ck_external_report_releases_content_owner CHECK (((((content_storage)::text = 'legacy'::text) AND (pdf_bytes IS NOT NULL) AND (record_context_json IS NOT NULL)) OR (((content_storage)::text = 'artifact'::text) AND (artifact_id IS NOT NULL) AND (pdf_bytes IS NULL) AND (evaluation_context_json IS NULL) AND (record_context_json IS NULL)))),
    CONSTRAINT ck_external_report_releases_context_object CHECK (((record_context_json IS NULL) OR (jsonb_typeof(record_context_json) = 'object'::text))),
    CONSTRAINT ck_external_report_releases_evaluation_object CHECK (((evaluation_context_json IS NULL) OR (jsonb_typeof(evaluation_context_json) = 'object'::text))),
    CONSTRAINT ck_external_report_releases_nonempty_pdf CHECK (((pdf_bytes IS NULL) OR (octet_length(pdf_bytes) > 5))),
    CONSTRAINT ck_external_report_releases_pdf_only CHECK (((format)::text = 'pdf'::text)),
    CONSTRAINT ck_external_report_releases_pdf_sha256 CHECK (((pdf_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_external_report_releases_provenance_mode CHECK (((provenance_mode)::text = ANY (ARRAY[('all-supported-sources'::character varying)::text, ('document-only'::character varying)::text]))),
    CONSTRAINT ck_external_report_releases_released_by CHECK ((length(TRIM(BOTH FROM released_by)) > 0))
);


ALTER TABLE public.external_report_releases OWNER TO corridor;

--
-- Name: external_report_releases_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.external_report_releases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.external_report_releases_id_seq OWNER TO corridor;

--
-- Name: external_report_releases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.external_report_releases_id_seq OWNED BY public.external_report_releases.id;


--
-- Name: extracted_proposal_facts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extracted_proposal_facts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    proposal_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    ordinal integer NOT NULL
);


ALTER TABLE public.extracted_proposal_facts OWNER TO corridor;

--
-- Name: extracted_proposal_facts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extracted_proposal_facts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extracted_proposal_facts_id_seq OWNER TO corridor;

--
-- Name: extracted_proposal_facts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extracted_proposal_facts_id_seq OWNED BY public.extracted_proposal_facts.id;


--
-- Name: extracted_proposals_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extracted_proposals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extracted_proposals_id_seq OWNER TO corridor;

--
-- Name: extracted_proposals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extracted_proposals_id_seq OWNED BY public.extracted_proposals.id;


--
-- Name: extraction_failure_diagnosis_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extraction_failure_diagnosis_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    timeout_seconds integer NOT NULL,
    max_requests integer NOT NULL,
    retry_policy character varying(32) NOT NULL,
    retention_policy character varying(64) NOT NULL,
    observation_context character varying(128) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_failure_diagnosis_config_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_failure_diagnosis_config_context CHECK ((length(TRIM(BOTH FROM observation_context)) > 0)),
    CONSTRAINT ck_failure_diagnosis_config_input_budget CHECK (((max_input_tokens >= 1) AND (max_input_tokens <= 200000))),
    CONSTRAINT ck_failure_diagnosis_config_model CHECK ((length(TRIM(BOTH FROM model)) > 0)),
    CONSTRAINT ck_failure_diagnosis_config_no_retry CHECK (((retry_policy)::text = 'none'::text)),
    CONSTRAINT ck_failure_diagnosis_config_one_request CHECK ((max_requests = 1)),
    CONSTRAINT ck_failure_diagnosis_config_output_budget CHECK (((max_output_tokens >= 1) AND (max_output_tokens <= 20000))),
    CONSTRAINT ck_failure_diagnosis_config_prompt CHECK ((length(TRIM(BOTH FROM prompt_version)) > 0)),
    CONSTRAINT ck_failure_diagnosis_config_retention CHECK (((retention_policy)::text = ANY ((ARRAY['retained_indefinitely'::character varying, 'class_b_30_days'::character varying])::text[]))),
    CONSTRAINT ck_failure_diagnosis_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);


ALTER TABLE public.extraction_failure_diagnosis_configurations OWNER TO corridor;

--
-- Name: extraction_failure_diagnosis_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq OWNER TO corridor;

--
-- Name: extraction_failure_diagnosis_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq OWNED BY public.extraction_failure_diagnosis_configurations.id;


--
-- Name: extraction_failure_diagnosis_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extraction_failure_diagnosis_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    input_sha256 character varying(64) NOT NULL,
    state_token character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    source_context_json jsonb,
    diagnosis_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb,
    usage_json jsonb,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    retention_class character varying(16) DEFAULT 'class_b'::character varying NOT NULL,
    retention_content_sha256 character varying(64),
    retention_deleted_at timestamp with time zone,
    CONSTRAINT ck_extraction_failure_diagnosis_requests_retention_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_extraction_failure_diagnosis_requests_retention_digest CHECK (((retention_content_sha256 IS NULL) OR ((retention_content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_failure_diagnosis_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_failure_diagnosis_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_failure_diagnosis_request_input_sha CHECK (((input_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_failure_diagnosis_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_failure_diagnosis_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_failure_diagnosis_request_status CHECK (((status)::text = ANY (ARRAY[('completed'::character varying)::text, ('budget_exhausted'::character varying)::text, ('timeout'::character varying)::text, ('transport_failure'::character varying)::text, ('validation_refused'::character varying)::text, ('stale_input'::character varying)::text])))
);


ALTER TABLE public.extraction_failure_diagnosis_requests OWNER TO corridor;

--
-- Name: extraction_failure_diagnosis_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extraction_failure_diagnosis_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extraction_failure_diagnosis_requests_id_seq OWNER TO corridor;

--
-- Name: extraction_failure_diagnosis_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extraction_failure_diagnosis_requests_id_seq OWNED BY public.extraction_failure_diagnosis_requests.id;


--
-- Name: extraction_measurement_case_states; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extraction_measurement_case_states (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    case_key character varying(160) NOT NULL,
    predecessor_state_id bigint,
    kind character varying(48) NOT NULL,
    state character varying(16) NOT NULL,
    ruling_type character varying(64) NOT NULL,
    ruling_id bigint NOT NULL,
    source_identity_json jsonb NOT NULL,
    expected_json jsonb NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_extraction_measurement_case_states_expected CHECK (((jsonb_typeof(expected_json) = 'object'::text) AND (jsonb_typeof((expected_json -> 'scoring_rule'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM (expected_json ->> 'scoring_rule'::text))) > 0))),
    CONSTRAINT ck_extraction_measurement_case_states_identity CHECK (((length(TRIM(BOTH FROM case_key)) > 0) AND (length(TRIM(BOTH FROM recorded_by)) > 0) AND (ruling_id > 0))),
    CONSTRAINT ck_extraction_measurement_case_states_kind CHECK (((kind)::text = ANY (ARRAY[('candidate_correction'::character varying)::text, ('source_discrepancy_settlement'::character varying)::text, ('do_not_add'::character varying)::text, ('statement_fact_correction'::character varying)::text, ('statement_scope_correction'::character varying)::text]))),
    CONSTRAINT ck_extraction_measurement_case_states_source CHECK (((jsonb_typeof(source_identity_json) = 'object'::text) AND (source_identity_json ?& ARRAY['candidate_id'::text, 'extraction_run_id'::text, 'documents'::text]) AND (jsonb_typeof((source_identity_json -> 'documents'::text)) = 'array'::text) AND (jsonb_array_length((source_identity_json -> 'documents'::text)) > 0))),
    CONSTRAINT ck_extraction_measurement_case_states_state CHECK (((state)::text = ANY (ARRAY[('active'::character varying)::text, ('reversed'::character varying)::text])))
);


ALTER TABLE public.extraction_measurement_case_states OWNER TO corridor;

--
-- Name: extraction_measurement_case_states_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extraction_measurement_case_states_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extraction_measurement_case_states_id_seq OWNER TO corridor;

--
-- Name: extraction_measurement_case_states_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extraction_measurement_case_states_id_seq OWNED BY public.extraction_measurement_case_states.id;


--
-- Name: extraction_run_candidates; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extraction_run_candidates (
    id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    candidate_id bigint NOT NULL
);


ALTER TABLE public.extraction_run_candidates OWNER TO corridor;

--
-- Name: extraction_run_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extraction_run_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extraction_run_candidates_id_seq OWNER TO corridor;

--
-- Name: extraction_run_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extraction_run_candidates_id_seq OWNED BY public.extraction_run_candidates.id;


--
-- Name: extraction_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.extraction_runs (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    prompt_version character varying(64) NOT NULL,
    candidate_count integer NOT NULL,
    page_errors integer DEFAULT 0 NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    outcome character varying(11) DEFAULT 'completed'::character varying NOT NULL,
    model character varying(64),
    schema_version character varying(64),
    error_detail text,
    candidate_inputs_json jsonb,
    prompt_sha256 character varying(64),
    schema_sha256 character varying(64),
    postprocessor_sha256 character varying(64),
    extractor_config_sha256 character varying(64),
    extractor_config_json jsonb,
    token_usage_json jsonb,
    row_accounting_json jsonb,
    CONSTRAINT ck_extraction_runs_completed_row_accounting CHECK (((NOT (((outcome)::text = 'completed'::text) AND ((prompt_version)::text = ANY ((ARRAY['sheet_native_v2'::character varying, 'matrix_tiered_v4'::character varying, 'prose_interpretation_v1'::character varying])::text[])))) OR ((row_accounting_json IS NOT NULL) AND ((((row_accounting_json ->> 'schema_version'::text) = 'matrix-row-accounting-v1'::text) AND (jsonb_array_length((row_accounting_json -> 'unaccounted_rows'::text)) = 0) AND (((row_accounting_json ->> 'accounted_row_count'::text))::integer = ((row_accounting_json ->> 'detected_row_count'::text))::integer) AND (((row_accounting_json ->> 'extracted_row_count'::text))::integer = candidate_count)) OR (((row_accounting_json ->> 'schema_version'::text) = 'prose-segment-accounting-v1'::text) AND (((row_accounting_json ->> 'proposed_fact_count'::text))::integer = candidate_count)))))),
    CONSTRAINT ck_extraction_runs_config_receipt_shape CHECK ((((prompt_sha256 IS NULL) AND (schema_sha256 IS NULL) AND (postprocessor_sha256 IS NULL) AND (extractor_config_json IS NULL) AND (extractor_config_sha256 IS NULL) AND (token_usage_json IS NULL)) OR (((prompt_sha256 IS NOT NULL) AND (schema_sha256 IS NOT NULL) AND (postprocessor_sha256 IS NOT NULL) AND (extractor_config_json IS NOT NULL) AND (extractor_config_sha256 IS NOT NULL) AND (token_usage_json IS NOT NULL) AND ((prompt_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((schema_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((postprocessor_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((extractor_config_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(extractor_config_json) = 'object'::text) AND (extractor_config_json ?& ARRAY['receipt_version'::text, 'extractor'::text, 'prompt_version'::text, 'model'::text, 'schema_version'::text, 'prompt_sha256'::text, 'schema_sha256'::text, 'postprocessor_sha256'::text, 'request_controls'::text, 'runtime'::text]) AND (jsonb_typeof((extractor_config_json -> 'receipt_version'::text)) = 'number'::text) AND ((extractor_config_json ->> 'receipt_version'::text) = '1'::text) AND (jsonb_typeof((extractor_config_json -> 'extractor'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM (extractor_config_json ->> 'extractor'::text))) > 0) AND (jsonb_typeof((extractor_config_json -> 'prompt_version'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'schema_version'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'prompt_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'schema_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'postprocessor_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'request_controls'::text)) = 'object'::text) AND (jsonb_typeof((extractor_config_json -> 'runtime'::text)) = 'object'::text) AND ((extractor_config_json -> 'runtime'::text) ?& ARRAY['python_implementation'::text, 'python_version'::text, 'dependency_lock_sha256'::text, 'packages'::text]) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'python_implementation'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM ((extractor_config_json -> 'runtime'::text) ->> 'python_implementation'::text))) > 0) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'python_version'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM ((extractor_config_json -> 'runtime'::text) ->> 'python_version'::text))) > 0) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'dependency_lock_sha256'::text)) = 'string'::text) AND (((extractor_config_json -> 'runtime'::text) ->> 'dependency_lock_sha256'::text) ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'packages'::text)) = 'object'::text) AND ((extractor_config_json ->> 'prompt_version'::text) = (prompt_version)::text) AND ((extractor_config_json ->> 'schema_version'::text) = (schema_version)::text) AND ((extractor_config_json ->> 'prompt_sha256'::text) = (prompt_sha256)::text) AND ((extractor_config_json ->> 'schema_sha256'::text) = (schema_sha256)::text) AND ((extractor_config_json ->> 'postprocessor_sha256'::text) = (postprocessor_sha256)::text) AND (((model IS NULL) AND (jsonb_typeof((extractor_config_json -> 'model'::text)) = 'null'::text)) OR ((model IS NOT NULL) AND (jsonb_typeof((extractor_config_json -> 'model'::text)) = 'string'::text) AND ((extractor_config_json ->> 'model'::text) = (model)::text))) AND (jsonb_typeof(token_usage_json) = 'object'::text) AND (token_usage_json ?& ARRAY['scope'::text, 'document_ids'::text, 'measurement'::text]) AND (jsonb_typeof((token_usage_json -> 'scope'::text)) = 'string'::text) AND (jsonb_typeof((token_usage_json -> 'measurement'::text)) = 'string'::text) AND ((token_usage_json ->> 'scope'::text) = ANY (ARRAY['run'::text, 'batch'::text])) AND (jsonb_typeof((token_usage_json -> 'document_ids'::text)) = 'array'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) > 0) AND ((((token_usage_json ->> 'scope'::text) = 'run'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) = 1)) OR (((token_usage_json ->> 'scope'::text) = 'batch'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) > 1))) AND ((((token_usage_json ->> 'measurement'::text) = 'unavailable'::text) AND (token_usage_json ? 'reason'::text) AND (jsonb_typeof((token_usage_json -> 'reason'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM (token_usage_json ->> 'reason'::text))) > 0)) OR (((token_usage_json ->> 'measurement'::text) = 'exact'::text) AND (token_usage_json ?& ARRAY['prompt_tokens'::text, 'completion_tokens'::text, 'reasoning_tokens'::text, 'cached_tokens'::text]) AND (jsonb_typeof((token_usage_json -> 'prompt_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'completion_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'reasoning_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'cached_tokens'::text)) = 'number'::text) AND ((token_usage_json ->> 'prompt_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'completion_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'reasoning_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'cached_tokens'::text) ~ '^[0-9]+$'::text) AND (((token_usage_json ->> 'prompt_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'completion_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'reasoning_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'cached_tokens'::text))::numeric >= (0)::numeric))) AND public.extraction_token_usage_membership_is_valid(document_id, token_usage_json)) IS TRUE))),
    CONSTRAINT ck_extraction_runs_row_accounting_shape CHECK (((row_accounting_json IS NULL) OR ((jsonb_typeof(row_accounting_json) = 'object'::text) AND ((row_accounting_json ->> 'reader_version'::text) = (prompt_version)::text) AND (((row_accounting_json ?& ARRAY['schema_version'::text, 'reader_version'::text, 'reader_path'::text, 'detected_row_count'::text, 'accounted_row_count'::text, 'extracted_row_count'::text, 'blank_row_count'::text, 'skipped_row_count'::text, 'unaccounted_rows'::text, 'rows'::text]) AND ((row_accounting_json ->> 'schema_version'::text) = 'matrix-row-accounting-v1'::text) AND (jsonb_typeof((row_accounting_json -> 'rows'::text)) = 'array'::text) AND (jsonb_typeof((row_accounting_json -> 'unaccounted_rows'::text)) = 'array'::text) AND ((row_accounting_json ->> 'detected_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'accounted_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'extracted_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'blank_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'skipped_row_count'::text) ~ '^[0-9]+$'::text) AND (jsonb_array_length((row_accounting_json -> 'rows'::text)) = ((row_accounting_json ->> 'detected_row_count'::text))::integer) AND (((row_accounting_json ->> 'accounted_row_count'::text))::integer = ((((row_accounting_json ->> 'extracted_row_count'::text))::integer + ((row_accounting_json ->> 'blank_row_count'::text))::integer) + ((row_accounting_json ->> 'skipped_row_count'::text))::integer)) AND (jsonb_array_length((row_accounting_json -> 'unaccounted_rows'::text)) = (((row_accounting_json ->> 'detected_row_count'::text))::integer - ((row_accounting_json ->> 'accounted_row_count'::text))::integer))) OR ((row_accounting_json ?& ARRAY['schema_version'::text, 'reader_version'::text, 'reader_path'::text, 'document_id'::text, 'detected_segment_count'::text, 'read_segment_count'::text, 'proposed_fact_count'::text, 'unread_segment_ids'::text, 'proposed_subject_candidate_ids'::text, 'unproposed_subject_candidate_ids'::text]) AND ((row_accounting_json ->> 'schema_version'::text) = 'prose-segment-accounting-v1'::text) AND ((row_accounting_json ->> 'reader_path'::text) = 'prose_interpretation'::text) AND ((row_accounting_json ->> 'document_id'::text) ~ '^[0-9]+$'::text) AND (((row_accounting_json ->> 'document_id'::text))::bigint = document_id) AND ((row_accounting_json ->> 'detected_segment_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'read_segment_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'proposed_fact_count'::text) ~ '^[0-9]+$'::text) AND (jsonb_typeof((row_accounting_json -> 'unread_segment_ids'::text)) = 'array'::text) AND (jsonb_typeof((row_accounting_json -> 'proposed_subject_candidate_ids'::text)) = 'array'::text) AND (jsonb_typeof((row_accounting_json -> 'unproposed_subject_candidate_ids'::text)) = 'array'::text) AND ((((row_accounting_json ->> 'read_segment_count'::text))::integer + jsonb_array_length((row_accounting_json -> 'unread_segment_ids'::text))) = ((row_accounting_json ->> 'detected_segment_count'::text))::integer)))))),
    CONSTRAINT extraction_outcome CHECK (((outcome)::text = ANY (ARRAY[('completed'::character varying)::text, ('failed'::character varying)::text, ('unreadable'::character varying)::text, ('no_matrix'::character varying)::text, ('quarantined'::character varying)::text])))
);


ALTER TABLE public.extraction_runs OWNER TO corridor;

--
-- Name: extraction_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.extraction_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.extraction_runs_id_seq OWNER TO corridor;

--
-- Name: extraction_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.extraction_runs_id_seq OWNED BY public.extraction_runs.id;


--
-- Name: fact_applies_to; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_applies_to (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    ordinal integer NOT NULL,
    CONSTRAINT ck_fact_applies_to_ordinal CHECK ((ordinal > 0))
);


ALTER TABLE public.fact_applies_to OWNER TO corridor;

--
-- Name: fact_applies_to_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_applies_to_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_applies_to_id_seq OWNER TO corridor;

--
-- Name: fact_applies_to_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_applies_to_id_seq OWNED BY public.fact_applies_to.id;


--
-- Name: fact_closure_results; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_closure_results (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    closure_kind character varying(48) NOT NULL,
    successor_dependency_id bigint,
    CONSTRAINT ck_fact_closure_result_kind CHECK (((closure_kind)::text = ANY ((ARRAY['source_marked_resolved'::character varying, 'constraint_closed'::character varying, 'constraint_remains_open'::character varying])::text[])))
);


ALTER TABLE public.fact_closure_results OWNER TO corridor;

--
-- Name: fact_closure_results_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_closure_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_closure_results_id_seq OWNER TO corridor;

--
-- Name: fact_closure_results_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_closure_results_id_seq OWNED BY public.fact_closure_results.id;


--
-- Name: fact_closure_sources; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_closure_sources (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    source_segment_id bigint NOT NULL,
    ordinal integer NOT NULL,
    CONSTRAINT ck_fact_closure_source_ordinal CHECK ((ordinal > 0))
);


ALTER TABLE public.fact_closure_sources OWNER TO corridor;

--
-- Name: fact_closure_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_closure_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_closure_sources_id_seq OWNER TO corridor;

--
-- Name: fact_closure_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_closure_sources_id_seq OWNED BY public.fact_closure_sources.id;


--
-- Name: fact_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE SEQUENCE public.fact_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_decisions_id_seq OWNER TO corridor_fact_decision_writer;

--
-- Name: fact_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER SEQUENCE public.fact_decisions_id_seq OWNED BY public.fact_decisions.id;


--
-- Name: fact_dispositions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_dispositions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    predecessor_fact_id bigint NOT NULL,
    successor_fact_id bigint NOT NULL,
    kind character varying(64) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fact_dispositions_kind_check CHECK (((kind)::text = 'source_reading_correction'::text))
);


ALTER TABLE public.fact_dispositions OWNER TO corridor;

--
-- Name: fact_dispositions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_dispositions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_dispositions_id_seq OWNER TO corridor;

--
-- Name: fact_dispositions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_dispositions_id_seq OWNED BY public.fact_dispositions.id;


--
-- Name: fact_sources; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_sources (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint,
    fact_id bigint NOT NULL,
    source_segment_id bigint NOT NULL,
    role character varying(32) NOT NULL,
    ordinal integer NOT NULL,
    CONSTRAINT ck_fact_sources_ordinal CHECK ((ordinal > 0)),
    CONSTRAINT ck_fact_sources_role CHECK (((role)::text = ANY ((ARRAY['value_source'::character varying, 'context'::character varying, 'attribution_source'::character varying])::text[])))
);


ALTER TABLE public.fact_sources OWNER TO corridor;

--
-- Name: fact_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_sources_id_seq OWNER TO corridor;

--
-- Name: fact_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_sources_id_seq OWNED BY public.fact_sources.id;


--
-- Name: fact_statement_timings; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.fact_statement_timings (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    fact_id bigint NOT NULL,
    timing_role character varying(16) NOT NULL,
    text text NOT NULL,
    "precision" character varying(32) NOT NULL,
    start_date date,
    end_date date,
    CONSTRAINT ck_fact_statement_timing_bounds CHECK ((((("precision")::text = 'day'::text) AND (start_date IS NOT NULL) AND (end_date = start_date)) OR ((("precision")::text = 'month'::text) AND (start_date IS NOT NULL) AND (end_date IS NOT NULL) AND (start_date = (date_trunc('month'::text, (start_date)::timestamp without time zone))::date) AND (end_date = ((date_trunc('month'::text, (start_date)::timestamp without time zone) + '1 mon -1 days'::interval))::date)) OR ((("precision")::text = 'approximate'::text) AND (start_date IS NULL) AND (end_date IS NULL)))),
    CONSTRAINT ck_fact_statement_timing_precision CHECK ((("precision")::text = ANY ((ARRAY['day'::character varying, 'month'::character varying, 'approximate'::character varying])::text[]))),
    CONSTRAINT ck_fact_statement_timing_role CHECK (((timing_role)::text = ANY ((ARRAY['previous'::character varying, 'new'::character varying])::text[]))),
    CONSTRAINT ck_fact_statement_timing_text CHECK ((length(TRIM(BOTH FROM text)) > 0))
);


ALTER TABLE public.fact_statement_timings OWNER TO corridor;

--
-- Name: fact_statement_timings_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.fact_statement_timings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.fact_statement_timings_id_seq OWNER TO corridor;

--
-- Name: fact_statement_timings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.fact_statement_timings_id_seq OWNED BY public.fact_statement_timings.id;


--
-- Name: facts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.facts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.facts_id_seq OWNER TO corridor;

--
-- Name: facts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.facts_id_seq OWNED BY public.facts.id;


--
-- Name: follow_up_plan_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.follow_up_plan_receipts (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    internal_owner_roster_entry_id bigint NOT NULL,
    internal_owner_decision_id bigint,
    next_action_decision_id bigint,
    resumed_deferral_decision_id bigint,
    audit_log_id bigint NOT NULL,
    expected_predecessors_json jsonb NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_follow_up_plan_receipt_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_follow_up_plan_receipt_one_result CHECK (((internal_owner_decision_id IS NOT NULL) OR (next_action_decision_id IS NOT NULL))),
    CONSTRAINT ck_follow_up_plan_receipt_predecessors_object CHECK ((jsonb_typeof(expected_predecessors_json) = 'object'::text))
);


ALTER TABLE public.follow_up_plan_receipts OWNER TO corridor;

--
-- Name: follow_up_plan_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.follow_up_plan_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.follow_up_plan_receipts_id_seq OWNER TO corridor;

--
-- Name: follow_up_plan_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.follow_up_plan_receipts_id_seq OWNED BY public.follow_up_plan_receipts.id;


--
-- Name: follow_up_plan_reversals; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.follow_up_plan_reversals (
    id bigint NOT NULL,
    receipt_id bigint NOT NULL,
    internal_owner_reversal_decision_id bigint,
    next_action_reversal_decision_id bigint,
    deferral_reversal_decision_id bigint,
    audit_log_id bigint NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_follow_up_plan_reversal_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0))
);


ALTER TABLE public.follow_up_plan_reversals OWNER TO corridor;

--
-- Name: follow_up_plan_reversals_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.follow_up_plan_reversals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.follow_up_plan_reversals_id_seq OWNER TO corridor;

--
-- Name: follow_up_plan_reversals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.follow_up_plan_reversals_id_seq OWNED BY public.follow_up_plan_reversals.id;


--
-- Name: inbound_messages; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.inbound_messages (
    id bigint NOT NULL,
    raw_sha256 character varying(64) NOT NULL,
    storage_path text NOT NULL,
    message_id text,
    sender text,
    subject text,
    sent_at timestamp with time zone,
    headers_json jsonb NOT NULL,
    body_text text NOT NULL,
    thread_id bigint NOT NULL,
    project_id bigint,
    route_status character varying(16) NOT NULL,
    route_evidence_json jsonb NOT NULL,
    document_id bigint,
    attachments_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inbound_message_route CHECK (((route_status)::text = ANY (ARRAY[('routed'::character varying)::text, ('triage'::character varying)::text]))),
    CONSTRAINT ck_inbound_message_sha256 CHECK (((raw_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.inbound_messages OWNER TO corridor;

--
-- Name: inbound_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.inbound_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.inbound_messages_id_seq OWNER TO corridor;

--
-- Name: inbound_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.inbound_messages_id_seq OWNED BY public.inbound_messages.id;


--
-- Name: inbound_route_triage; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.inbound_route_triage (
    id bigint NOT NULL,
    thread_id bigint NOT NULL,
    candidate_project_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    state character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    resolved_project_id bigint,
    resolved_by text,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inbound_route_triage_state CHECK (((state)::text = ANY (ARRAY[('pending'::character varying)::text, ('resolved'::character varying)::text])))
);


ALTER TABLE public.inbound_route_triage OWNER TO corridor;

--
-- Name: inbound_route_triage_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.inbound_route_triage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.inbound_route_triage_id_seq OWNER TO corridor;

--
-- Name: inbound_route_triage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.inbound_route_triage_id_seq OWNED BY public.inbound_route_triage.id;


--
-- Name: inbound_thread_readings; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.inbound_thread_readings (
    id bigint NOT NULL,
    thread_id bigint NOT NULL,
    closing_message_id bigint NOT NULL,
    resolution character varying(16) NOT NULL,
    candidate_id bigint,
    open_question text,
    turn_context_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    prompt_version character varying(64),
    model character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inbound_thread_reading_claim CHECK ((((resolution)::text = 'concluded'::text) = (candidate_id IS NOT NULL))),
    CONSTRAINT ck_inbound_thread_reading_question CHECK ((((resolution)::text = 'unresolved'::text) = (open_question IS NOT NULL))),
    CONSTRAINT ck_inbound_thread_reading_resolution CHECK (((resolution)::text = ANY (ARRAY[('concluded'::character varying)::text, ('unresolved'::character varying)::text])))
);


ALTER TABLE public.inbound_thread_readings OWNER TO corridor;

--
-- Name: inbound_thread_readings_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.inbound_thread_readings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.inbound_thread_readings_id_seq OWNER TO corridor;

--
-- Name: inbound_thread_readings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.inbound_thread_readings_id_seq OWNED BY public.inbound_thread_readings.id;


--
-- Name: inbound_threads; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.inbound_threads (
    id bigint NOT NULL,
    project_id bigint,
    dependency_id bigint,
    bound_by_message_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.inbound_threads OWNER TO corridor;

--
-- Name: inbound_threads_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.inbound_threads_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.inbound_threads_id_seq OWNER TO corridor;

--
-- Name: inbound_threads_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.inbound_threads_id_seq OWNED BY public.inbound_threads.id;


--
-- Name: intake_project_identifiers; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.intake_project_identifiers (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    kind character varying(48) NOT NULL,
    value_normalized character varying(256) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_intake_identifier_kind CHECK ((length(TRIM(BOTH FROM kind)) > 0)),
    CONSTRAINT ck_intake_identifier_value CHECK ((length(TRIM(BOTH FROM value_normalized)) > 0))
);


ALTER TABLE public.intake_project_identifiers OWNER TO corridor;

--
-- Name: intake_project_identifiers_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.intake_project_identifiers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.intake_project_identifiers_id_seq OWNER TO corridor;

--
-- Name: intake_project_identifiers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.intake_project_identifiers_id_seq OWNED BY public.intake_project_identifiers.id;


--
-- Name: key_date_draft_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.key_date_draft_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    allowed_pages_json jsonb NOT NULL,
    requested_by character varying(128) NOT NULL,
    status character varying(16) NOT NULL,
    reason character varying(128),
    detail text,
    configuration_json jsonb NOT NULL,
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    unresolved_json jsonb NOT NULL,
    sequencing_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_key_date_draft_receipts_json CHECK (((jsonb_typeof(allowed_pages_json) = 'array'::text) AND (jsonb_typeof(configuration_json) = 'object'::text) AND (jsonb_typeof(budget_json) = 'object'::text) AND (jsonb_typeof(usage_json) = 'object'::text) AND (jsonb_typeof(unresolved_json) = 'array'::text) AND (jsonb_typeof(sequencing_json) = 'array'::text))),
    CONSTRAINT ck_key_date_draft_receipts_source_sha256 CHECK (((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_key_date_draft_receipts_status CHECK (((status)::text = ANY (ARRAY[('drafted'::character varying)::text, ('abstained'::character varying)::text, ('failed'::character varying)::text])))
);


ALTER TABLE public.key_date_draft_receipts OWNER TO corridor;

--
-- Name: key_date_draft_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.key_date_draft_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.key_date_draft_receipts_id_seq OWNER TO corridor;

--
-- Name: key_date_draft_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.key_date_draft_receipts_id_seq OWNED BY public.key_date_draft_receipts.id;


--
-- Name: key_date_draft_row_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.key_date_draft_row_receipts (
    id bigint NOT NULL,
    receipt_id bigint NOT NULL,
    ordinal integer NOT NULL,
    code character varying(64) NOT NULL,
    name text NOT NULL,
    need_date date NOT NULL,
    "precision" character varying(16) NOT NULL,
    source_page integer NOT NULL,
    source_quote text NOT NULL,
    CONSTRAINT ck_key_date_draft_row_precision CHECK ((("precision")::text = 'day'::text)),
    CONSTRAINT ck_key_date_draft_row_source_page CHECK ((source_page > 0))
);


ALTER TABLE public.key_date_draft_row_receipts OWNER TO corridor;

--
-- Name: key_date_draft_row_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.key_date_draft_row_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.key_date_draft_row_receipts_id_seq OWNER TO corridor;

--
-- Name: key_date_draft_row_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.key_date_draft_row_receipts_id_seq OWNED BY public.key_date_draft_row_receipts.id;


--
-- Name: legacy_ledger_archives; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.legacy_ledger_archives (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    format_version character varying(64) NOT NULL,
    content_json jsonb NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    dependency_count integer NOT NULL,
    assertion_count integer NOT NULL,
    evidence_link_count integer NOT NULL,
    audit_log_count integer NOT NULL,
    ref_code_high_watermark integer NOT NULL,
    retired_by text NOT NULL,
    retired_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_legacy_ledger_archive_content_object CHECK ((jsonb_typeof(content_json) = 'object'::text)),
    CONSTRAINT ck_legacy_ledger_archive_counts CHECK (((dependency_count >= 0) AND (assertion_count >= 0) AND (evidence_link_count >= 0) AND (audit_log_count >= 0))),
    CONSTRAINT ck_legacy_ledger_archive_ref_high_watermark CHECK ((ref_code_high_watermark >= 0)),
    CONSTRAINT ck_legacy_ledger_archive_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.legacy_ledger_archives OWNER TO corridor;

--
-- Name: legacy_ledger_archives_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.legacy_ledger_archives_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.legacy_ledger_archives_id_seq OWNER TO corridor;

--
-- Name: legacy_ledger_archives_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.legacy_ledger_archives_id_seq OWNED BY public.legacy_ledger_archives.id;


--
-- Name: milestone_registrations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.milestone_registrations (
    id bigint NOT NULL,
    milestone_id bigint NOT NULL,
    source_name text NOT NULL,
    source_sha256 character varying(64),
    source_row_json jsonb NOT NULL,
    recorded_by character varying(128) NOT NULL,
    predecessor_registration_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_milestone_registrations_recorded_by CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_milestone_registrations_source_row CHECK ((jsonb_typeof(source_row_json) = 'object'::text)),
    CONSTRAINT ck_milestone_registrations_source_sha256 CHECK (((source_sha256 IS NULL) OR ((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);


ALTER TABLE public.milestone_registrations OWNER TO corridor;

--
-- Name: milestone_registrations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.milestone_registrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.milestone_registrations_id_seq OWNER TO corridor;

--
-- Name: milestone_registrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.milestone_registrations_id_seq OWNED BY public.milestone_registrations.id;


--
-- Name: milestones; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.milestones (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    code character varying(64) NOT NULL,
    name text NOT NULL,
    need_date date,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    current_registration_id bigint
);


ALTER TABLE public.milestones OWNER TO corridor;

--
-- Name: milestones_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.milestones_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.milestones_id_seq OWNER TO corridor;

--
-- Name: milestones_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.milestones_id_seq OWNED BY public.milestones.id;


--
-- Name: operative_support; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.operative_support (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    evidence_link_id bigint NOT NULL,
    role character varying(11) NOT NULL,
    field_name character varying(64),
    designated_by text NOT NULL,
    designated_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_link_id bigint,
    CONSTRAINT operative_support_role CHECK (((role)::text = 'publication'::text))
);


ALTER TABLE public.operative_support OWNER TO corridor;

--
-- Name: operative_support_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.operative_support_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.operative_support_id_seq OWNER TO corridor;

--
-- Name: operative_support_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.operative_support_id_seq OWNED BY public.operative_support.id;


--
-- Name: organization_identity_activations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.organization_identity_activations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    replay_case_count integer,
    reason character varying(160) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_organization_identity_activation_action CHECK (((action)::text = ANY (ARRAY[('activate'::character varying)::text, ('suspend'::character varying)::text]))),
    CONSTRAINT ck_organization_identity_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_organization_identity_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_organization_identity_activation_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.organization_identity_activations OWNER TO corridor;

--
-- Name: organization_identity_activations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.organization_identity_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.organization_identity_activations_id_seq OWNER TO corridor;

--
-- Name: organization_identity_activations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.organization_identity_activations_id_seq OWNED BY public.organization_identity_activations.id;


--
-- Name: organization_identity_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.organization_identity_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    external_org_id bigint NOT NULL,
    method character varying(64) NOT NULL,
    scope character varying(32) DEFAULT 'registry'::character varying NOT NULL,
    stated_wording text NOT NULL,
    evidence_json jsonb NOT NULL,
    facility_classes_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    recorded_by character varying(128) NOT NULL,
    policy_version character varying(64),
    policy_sha256 character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_organization_identity_receipt_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_organization_identity_receipt_evidence CHECK ((jsonb_typeof(evidence_json) = 'object'::text)),
    CONSTRAINT ck_organization_identity_receipt_facility_classes CHECK ((jsonb_typeof(facility_classes_json) = 'array'::text)),
    CONSTRAINT ck_organization_identity_receipt_method CHECK (((method)::text = ANY (ARRAY[('human_confirmation'::character varying)::text, ('automatic_name_alias'::character varying)::text, ('automatic_facility_class'::character varying)::text, ('automatic_contact'::character varying)::text, ('automatic_revision_lineage'::character varying)::text, ('automatic_stated_alias'::character varying)::text, ('human_cited_alias_confirmation'::character varying)::text, ('alias_correction'::character varying)::text]))),
    CONSTRAINT ck_organization_identity_receipt_scope CHECK (((scope)::text = 'registry'::text)),
    CONSTRAINT ck_organization_identity_receipt_wording CHECK ((length(TRIM(BOTH FROM stated_wording)) > 0))
);


ALTER TABLE public.organization_identity_receipts OWNER TO corridor;

--
-- Name: organization_identity_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.organization_identity_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.organization_identity_receipts_id_seq OWNER TO corridor;

--
-- Name: organization_identity_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.organization_identity_receipts_id_seq OWNED BY public.organization_identity_receipts.id;


--
-- Name: page_processing_failures; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.page_processing_failures (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_number integer NOT NULL,
    engine character varying(64) NOT NULL,
    configuration_json jsonb NOT NULL,
    region_id character varying(64) NOT NULL,
    scope_json jsonb NOT NULL,
    error_type character varying(160) NOT NULL,
    error_message text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_page_processing_failures_engine CHECK ((length((engine)::text) > 0)),
    CONSTRAINT ck_page_processing_failures_error_message CHECK ((length(error_message) > 0)),
    CONSTRAINT ck_page_processing_failures_error_type CHECK ((length((error_type)::text) > 0)),
    CONSTRAINT ck_page_processing_failures_page_number CHECK ((page_number > 0)),
    CONSTRAINT ck_page_processing_failures_region_id CHECK ((length((region_id)::text) > 0))
);


ALTER TABLE public.page_processing_failures OWNER TO corridor;

--
-- Name: page_processing_failures_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.page_processing_failures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.page_processing_failures_id_seq OWNER TO corridor;

--
-- Name: page_processing_failures_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.page_processing_failures_id_seq OWNED BY public.page_processing_failures.id;


--
-- Name: page_render_derivatives; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.page_render_derivatives (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_number integer NOT NULL,
    derivative_key character varying(64) NOT NULL,
    profile_name character varying(32) NOT NULL,
    profile_id character varying(64) NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    artifact_path text NOT NULL,
    artifact_sha256 character varying(64) NOT NULL,
    artifact_bytes bigint NOT NULL,
    manifest_json jsonb NOT NULL,
    retention_class character varying(32) DEFAULT 'intermediary_processing'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_page_render_derivatives_artifact_bytes CHECK ((artifact_bytes > 0)),
    CONSTRAINT ck_page_render_derivatives_artifact_sha256 CHECK (((artifact_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_page_render_derivatives_page_number CHECK ((page_number > 0)),
    CONSTRAINT ck_page_render_derivatives_profile_id CHECK ((length((profile_id)::text) > 0)),
    CONSTRAINT ck_page_render_derivatives_profile_name CHECK ((length((profile_name)::text) > 0)),
    CONSTRAINT ck_page_render_derivatives_retention_class CHECK (((retention_class)::text = 'intermediary_processing'::text)),
    CONSTRAINT ck_page_render_derivatives_source_sha256 CHECK (((source_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.page_render_derivatives OWNER TO corridor;

--
-- Name: page_render_derivatives_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.page_render_derivatives_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.page_render_derivatives_id_seq OWNER TO corridor;

--
-- Name: page_render_derivatives_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.page_render_derivatives_id_seq OWNED BY public.page_render_derivatives.id;


--
-- Name: person_identities; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.person_identities (
    id bigint NOT NULL,
    email_normalized text NOT NULL,
    principal_subject character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_person_identity_email CHECK ((length(TRIM(BOTH FROM email_normalized)) > 0)),
    CONSTRAINT ck_person_identity_principal CHECK ((length(TRIM(BOTH FROM principal_subject)) > 0))
);


ALTER TABLE public.person_identities OWNER TO corridor;

--
-- Name: person_identities_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.person_identities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.person_identities_id_seq OWNER TO corridor;

--
-- Name: person_identities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.person_identities_id_seq OWNED BY public.person_identities.id;


--
-- Name: policy_approvals; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.policy_approvals (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    family character varying(32) NOT NULL,
    policy_version character varying(64) NOT NULL,
    approved_by text NOT NULL,
    policy_json jsonb NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_policy_approvals_family CHECK (((family)::text = ANY (ARRAY[('automatic-carry-forward'::character varying)::text, ('event-admission'::character varying)::text, ('dependency-admission'::character varying)::text]))),
    CONSTRAINT ck_policy_approvals_object CHECK ((jsonb_typeof(policy_json) = 'object'::text)),
    CONSTRAINT ck_policy_approvals_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.policy_approvals OWNER TO corridor;

--
-- Name: policy_approvals_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.policy_approvals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.policy_approvals_id_seq OWNER TO corridor;

--
-- Name: policy_approvals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.policy_approvals_id_seq OWNED BY public.policy_approvals.id;


--
-- Name: policy_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.policy_runs (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    family character varying(32) NOT NULL,
    policy_approval_id bigint,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    abstention_reason_version character varying(64) NOT NULL,
    applied_count integer NOT NULL,
    abstained_count integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_policy_runs_counts CHECK (((applied_count >= 0) AND (abstained_count >= 0))),
    CONSTRAINT ck_policy_runs_family CHECK (((family)::text = ANY (ARRAY[('automatic-carry-forward'::character varying)::text, ('event-admission'::character varying)::text, ('dependency-admission'::character varying)::text]))),
    CONSTRAINT ck_policy_runs_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.policy_runs OWNER TO corridor;

--
-- Name: policy_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.policy_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.policy_runs_id_seq OWNER TO corridor;

--
-- Name: policy_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.policy_runs_id_seq OWNED BY public.policy_runs.id;


--
-- Name: processing_artifacts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.processing_artifacts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    kind character varying(64) NOT NULL,
    retention_class character varying(16) NOT NULL,
    storage_path text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    terminal_at timestamp with time zone NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT ck_processing_artifact_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_processing_artifact_kind CHECK (((kind)::text = ANY ((ARRAY['page_render'::character varying, 'raw_ocr'::character varying, 'token_layer'::character varying, 'alternate_table_hypothesis'::character varying, 'unselected_model_response'::character varying, 'copied_prompt_context'::character varying, 'agent_trace'::character varying, 'evaluation_working_data'::character varying, 'abandoned_report_preparation'::character varying])::text[]))),
    CONSTRAINT ck_processing_artifact_sha CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.processing_artifacts OWNER TO corridor;

--
-- Name: processing_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.processing_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.processing_artifacts_id_seq OWNER TO corridor;

--
-- Name: processing_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.processing_artifacts_id_seq OWNED BY public.processing_artifacts.id;


--
-- Name: production_run_explanation_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.production_run_explanation_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    timeout_seconds integer NOT NULL,
    max_requests integer NOT NULL,
    retry_policy character varying(32) NOT NULL,
    retention_policy character varying(64) NOT NULL,
    observation_context character varying(128) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_run_explanation_config_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_run_explanation_config_context CHECK ((length(TRIM(BOTH FROM observation_context)) > 0)),
    CONSTRAINT ck_run_explanation_config_input_budget CHECK (((max_input_tokens >= 1) AND (max_input_tokens <= 200000))),
    CONSTRAINT ck_run_explanation_config_model CHECK ((length(TRIM(BOTH FROM model)) > 0)),
    CONSTRAINT ck_run_explanation_config_no_retry CHECK (((retry_policy)::text = 'none'::text)),
    CONSTRAINT ck_run_explanation_config_one_request CHECK ((max_requests = 1)),
    CONSTRAINT ck_run_explanation_config_output_budget CHECK (((max_output_tokens >= 1) AND (max_output_tokens <= 20000))),
    CONSTRAINT ck_run_explanation_config_prompt CHECK ((length(TRIM(BOTH FROM prompt_version)) > 0)),
    CONSTRAINT ck_run_explanation_config_retention CHECK (((retention_policy)::text = ANY ((ARRAY['retained_indefinitely'::character varying, 'class_b_30_days'::character varying])::text[]))),
    CONSTRAINT ck_run_explanation_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);


ALTER TABLE public.production_run_explanation_configurations OWNER TO corridor;

--
-- Name: production_run_explanation_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.production_run_explanation_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.production_run_explanation_configurations_id_seq OWNER TO corridor;

--
-- Name: production_run_explanation_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.production_run_explanation_configurations_id_seq OWNED BY public.production_run_explanation_configurations.id;


--
-- Name: production_run_explanation_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.production_run_explanation_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    comparison_sha256 character varying(64) NOT NULL,
    state_token character varying(64) NOT NULL,
    competing_run_ids_json jsonb,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    comparison_json jsonb,
    explanation_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb,
    usage_json jsonb,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    retention_class character varying(16) DEFAULT 'class_b'::character varying NOT NULL,
    retention_content_sha256 character varying(64),
    retention_deleted_at timestamp with time zone,
    CONSTRAINT ck_production_run_explanation_requests_retention_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_production_run_explanation_requests_retention_digest CHECK (((retention_content_sha256 IS NULL) OR ((retention_content_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_run_explanation_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_run_explanation_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_run_explanation_request_comparison_sha CHECK (((comparison_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_run_explanation_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_run_explanation_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_run_explanation_request_status CHECK (((status)::text = ANY (ARRAY[('completed'::character varying)::text, ('budget_exhausted'::character varying)::text, ('timeout'::character varying)::text, ('transport_failure'::character varying)::text, ('validation_refused'::character varying)::text, ('stale_input'::character varying)::text])))
);


ALTER TABLE public.production_run_explanation_requests OWNER TO corridor;

--
-- Name: production_run_explanation_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.production_run_explanation_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.production_run_explanation_requests_id_seq OWNER TO corridor;

--
-- Name: production_run_explanation_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.production_run_explanation_requests_id_seq OWNED BY public.production_run_explanation_requests.id;


--
-- Name: project_check_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.project_check_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    ruleset_version character varying(32) NOT NULL,
    stale_days integer NOT NULL,
    due_soon_days integer NOT NULL,
    action_due_soon_days integer NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_project_check_configurations_action_due_soon_days CHECK (((action_due_soon_days >= 1) AND (action_due_soon_days <= 3650))),
    CONSTRAINT ck_project_check_configurations_created_by CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_project_check_configurations_due_soon_days CHECK (((due_soon_days >= 1) AND (due_soon_days <= 3650))),
    CONSTRAINT ck_project_check_configurations_ruleset_version CHECK ((length(TRIM(BOTH FROM ruleset_version)) > 0)),
    CONSTRAINT ck_project_check_configurations_stale_days CHECK (((stale_days >= 1) AND (stale_days <= 3650)))
);


ALTER TABLE public.project_check_configurations OWNER TO corridor;

--
-- Name: project_check_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.project_check_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.project_check_configurations_id_seq OWNER TO corridor;

--
-- Name: project_check_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.project_check_configurations_id_seq OWNED BY public.project_check_configurations.id;


--
-- Name: project_record_revisions; Type: TABLE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TABLE public.project_record_revisions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    predecessor_revision_id bigint,
    command_type character varying(64) NOT NULL,
    human_principal character varying(128),
    released_policy character varying(128),
    idempotency_key character varying(160) NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_project_record_revision_authority_xor CHECK (((human_principal IS NULL) <> (released_policy IS NULL)))
);


ALTER TABLE public.project_record_revisions OWNER TO corridor_fact_decision_writer;

--
-- Name: project_record_revisions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE SEQUENCE public.project_record_revisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.project_record_revisions_id_seq OWNER TO corridor_fact_decision_writer;

--
-- Name: project_record_revisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER SEQUENCE public.project_record_revisions_id_seq OWNED BY public.project_record_revisions.id;


--
-- Name: project_roster_entries; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.project_roster_entries (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    principal_subject character varying(128) NOT NULL,
    display_name text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    can_coordinate boolean DEFAULT false NOT NULL,
    can_review_documentation boolean DEFAULT false NOT NULL,
    can_release_externally boolean DEFAULT false NOT NULL,
    is_technical_operator boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_project_roster_display_name CHECK ((length(TRIM(BOTH FROM display_name)) > 0)),
    CONSTRAINT ck_project_roster_principal CHECK ((length(TRIM(BOTH FROM principal_subject)) > 0))
);


ALTER TABLE public.project_roster_entries OWNER TO corridor;

--
-- Name: project_roster_entries_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.project_roster_entries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.project_roster_entries_id_seq OWNER TO corridor;

--
-- Name: project_roster_entries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.project_roster_entries_id_seq OWNED BY public.project_roster_entries.id;


--
-- Name: projects; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.projects (
    id bigint NOT NULL,
    slug character varying(64) NOT NULL,
    name text NOT NULL,
    agency text,
    is_synthetic boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    project_side_parties jsonb DEFAULT '[]'::jsonb NOT NULL
);


ALTER TABLE public.projects OWNER TO corridor;

--
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.projects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.projects_id_seq OWNER TO corridor;

--
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- Name: reconfirmation_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.reconfirmation_receipts (
    audit_log_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    successor_candidate_id bigint NOT NULL,
    before_json jsonb NOT NULL,
    after_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_reconfirmation_receipt_after_object CHECK ((jsonb_typeof(after_json) = 'object'::text)),
    CONSTRAINT ck_reconfirmation_receipt_before_object CHECK ((jsonb_typeof(before_json) = 'object'::text))
);


ALTER TABLE public.reconfirmation_receipts OWNER TO corridor;

--
-- Name: record_inclusion_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.record_inclusion_requests (
    project_id bigint NOT NULL,
    dirty_seq bigint DEFAULT '0'::bigint NOT NULL,
    reconciled_seq bigint DEFAULT '0'::bigint NOT NULL,
    last_reason text,
    requested_at timestamp with time zone,
    reconciled_at timestamp with time zone,
    CONSTRAINT ck_record_inclusion_requests_non_negative CHECK (((dirty_seq >= 0) AND (reconciled_seq >= 0))),
    CONSTRAINT ck_record_inclusion_requests_watermark_order CHECK ((reconciled_seq <= dirty_seq))
);


ALTER TABLE public.record_inclusion_requests OWNER TO corridor;

--
-- Name: report_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.report_runs (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    ruleset_version character varying(32) NOT NULL,
    snapshot_json jsonb NOT NULL,
    output_path text,
    document_only boolean DEFAULT false NOT NULL
);


ALTER TABLE public.report_runs OWNER TO corridor;

--
-- Name: report_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.report_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.report_runs_id_seq OWNER TO corridor;

--
-- Name: report_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.report_runs_id_seq OWNED BY public.report_runs.id;


--
-- Name: retention_holds; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retention_holds (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    reason text NOT NULL,
    placed_by character varying(128) NOT NULL,
    placed_at timestamp with time zone DEFAULT now() NOT NULL,
    lifted_by character varying(128),
    lifted_at timestamp with time zone,
    CONSTRAINT ck_retention_holds_actor CHECK ((length(TRIM(BOTH FROM placed_by)) > 0)),
    CONSTRAINT ck_retention_holds_lift CHECK ((((lifted_by IS NULL) AND (lifted_at IS NULL)) OR ((length(TRIM(BOTH FROM lifted_by)) > 0) AND (lifted_at IS NOT NULL)))),
    CONSTRAINT ck_retention_holds_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0))
);


ALTER TABLE public.retention_holds OWNER TO corridor;

--
-- Name: retention_holds_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.retention_holds_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.retention_holds_id_seq OWNER TO corridor;

--
-- Name: retention_holds_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.retention_holds_id_seq OWNED BY public.retention_holds.id;


--
-- Name: retention_manifest_items; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retention_manifest_items (
    id bigint NOT NULL,
    manifest_id bigint NOT NULL,
    project_id bigint NOT NULL,
    family character varying(64) NOT NULL,
    source_row_id bigint NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    terminal_at timestamp with time zone NOT NULL,
    delete_after timestamp with time zone NOT NULL,
    CONSTRAINT ck_retention_manifest_item_sha CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.retention_manifest_items OWNER TO corridor;

--
-- Name: retention_manifest_items_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.retention_manifest_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.retention_manifest_items_id_seq OWNER TO corridor;

--
-- Name: retention_manifest_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.retention_manifest_items_id_seq OWNED BY public.retention_manifest_items.id;


--
-- Name: retention_manifests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retention_manifests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    as_of timestamp with time zone NOT NULL,
    status character varying(16) NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    executed_at timestamp with time zone,
    CONSTRAINT ck_retention_manifest_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_retention_manifest_sha CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_retention_manifest_status CHECK (((status)::text = ANY ((ARRAY['dry_run'::character varying, 'executed'::character varying, 'refused'::character varying])::text[])))
);


ALTER TABLE public.retention_manifests OWNER TO corridor;

--
-- Name: retention_manifests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.retention_manifests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.retention_manifests_id_seq OWNER TO corridor;

--
-- Name: retention_manifests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.retention_manifests_id_seq OWNED BY public.retention_manifests.id;


--
-- Name: retention_references; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retention_references (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    family character varying(64) NOT NULL,
    source_row_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    referenced_by text NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    CONSTRAINT ck_retention_reference_identity CHECK (((length(TRIM(BOTH FROM family)) > 0) AND (length(TRIM(BOTH FROM referenced_by)) > 0))),
    CONSTRAINT ck_retention_reference_kind CHECK (((kind)::text = ANY ((ARRAY['segment'::character varying, 'decision'::character varying, 'review'::character varying, 'release'::character varying, 'processing_failure'::character varying])::text[])))
);


ALTER TABLE public.retention_references OWNER TO corridor;

--
-- Name: retention_references_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.retention_references_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.retention_references_id_seq OWNER TO corridor;

--
-- Name: retention_references_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.retention_references_id_seq OWNED BY public.retention_references.id;


--
-- Name: retired_automatic_carry_forward_policy_activations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retired_automatic_carry_forward_policy_activations (
    project_id bigint NOT NULL,
    policy_approval_id bigint NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'automatic-carry-forward'::character varying NOT NULL,
    CONSTRAINT ck_active_automatic_carry_forward_policy_family CHECK (((family)::text = 'automatic-carry-forward'::text))
);


ALTER TABLE public.retired_automatic_carry_forward_policy_activations OWNER TO corridor;

--
-- Name: retired_dependency_statuses; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.retired_dependency_statuses (
    dependency_id bigint NOT NULL,
    status character varying(32) NOT NULL,
    retired_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.retired_dependency_statuses OWNER TO corridor;

--
-- Name: revision_change_explanation_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.revision_change_explanation_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    timeout_seconds integer NOT NULL,
    max_requests integer NOT NULL,
    retry_policy character varying(32) NOT NULL,
    retention_policy character varying(64) NOT NULL,
    observation_context character varying(128) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_rev_change_expl_cfg_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_rev_change_expl_cfg_context CHECK ((length(TRIM(BOTH FROM observation_context)) > 0)),
    CONSTRAINT ck_rev_change_expl_cfg_input_budget CHECK (((max_input_tokens >= 1) AND (max_input_tokens <= 200000))),
    CONSTRAINT ck_rev_change_expl_cfg_model CHECK ((length(TRIM(BOTH FROM model)) > 0)),
    CONSTRAINT ck_rev_change_expl_cfg_no_retry CHECK (((retry_policy)::text = 'none'::text)),
    CONSTRAINT ck_rev_change_expl_cfg_one_request CHECK ((max_requests = 1)),
    CONSTRAINT ck_rev_change_expl_cfg_output_budget CHECK (((max_output_tokens >= 1) AND (max_output_tokens <= 20000))),
    CONSTRAINT ck_rev_change_expl_cfg_prompt CHECK ((length(TRIM(BOTH FROM prompt_version)) > 0)),
    CONSTRAINT ck_rev_change_expl_cfg_retention CHECK (((retention_policy)::text = ANY ((ARRAY['retained_indefinitely'::character varying, 'class_b_30_days'::character varying])::text[]))),
    CONSTRAINT ck_rev_change_expl_cfg_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);


ALTER TABLE public.revision_change_explanation_configurations OWNER TO corridor;

--
-- Name: revision_change_explanation_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.revision_change_explanation_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.revision_change_explanation_configurations_id_seq OWNER TO corridor;

--
-- Name: revision_change_explanation_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.revision_change_explanation_configurations_id_seq OWNED BY public.revision_change_explanation_configurations.id;


--
-- Name: revision_change_explanation_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.revision_change_explanation_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    comparison_id bigint NOT NULL,
    finding_id bigint NOT NULL,
    finding_state character varying(32) NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    comparison_sha256 character varying(64) NOT NULL,
    state_token character varying(64) NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    comparison_json jsonb,
    explanation_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb,
    usage_json jsonb,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    retention_class character varying(16) DEFAULT 'class_b'::character varying NOT NULL,
    retention_content_sha256 character varying(64),
    retention_deleted_at timestamp with time zone,
    CONSTRAINT ck_rev_change_expl_req_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_rev_change_expl_req_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_rev_change_expl_req_comparison_sha CHECK (((comparison_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_rev_change_expl_req_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_rev_change_expl_req_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_rev_change_expl_req_status CHECK (((status)::text = ANY (ARRAY[('completed'::character varying)::text, ('budget_exhausted'::character varying)::text, ('timeout'::character varying)::text, ('transport_failure'::character varying)::text, ('validation_refused'::character varying)::text, ('stale_input'::character varying)::text]))),
    CONSTRAINT ck_revision_change_explanation_requests_retention_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_revision_change_explanation_requests_retention_digest CHECK (((retention_content_sha256 IS NULL) OR ((retention_content_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);


ALTER TABLE public.revision_change_explanation_requests OWNER TO corridor;

--
-- Name: revision_change_explanation_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.revision_change_explanation_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.revision_change_explanation_requests_id_seq OWNER TO corridor;

--
-- Name: revision_change_explanation_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.revision_change_explanation_requests_id_seq OWNED BY public.revision_change_explanation_requests.id;


--
-- Name: revision_comparison_findings; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.revision_comparison_findings (
    id bigint NOT NULL,
    revision_comparison_run_id bigint NOT NULL,
    ordinal integer NOT NULL,
    state character varying(9) NOT NULL,
    predecessor_candidate_ids bigint[] NOT NULL,
    successor_candidate_ids bigint[] NOT NULL,
    match_score double precision,
    field_changes jsonb NOT NULL,
    matcher_detail jsonb NOT NULL,
    CONSTRAINT ck_revision_comparison_finding_shape CHECK (((((state)::text = 'added'::text) AND (cardinality(predecessor_candidate_ids) = 0) AND (cardinality(successor_candidate_ids) = 1)) OR (((state)::text = 'dropped'::text) AND (cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 0)) OR (((state)::text = 'unmatched'::text) AND (((cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 0)) OR ((cardinality(predecessor_candidate_ids) = 0) AND (cardinality(successor_candidate_ids) = 1)))) OR (((state)::text = ANY (ARRAY[('unchanged'::character varying)::text, ('changed'::character varying)::text])) AND (cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 1)) OR (((state)::text = 'ambiguous'::text) AND (cardinality(predecessor_candidate_ids) > 0) AND (cardinality(successor_candidate_ids) > 0)))),
    CONSTRAINT ck_revision_comparison_match_score CHECK (((match_score IS NULL) OR ((match_score >= (0)::double precision) AND (match_score <= (1)::double precision)))),
    CONSTRAINT revision_comparison_state CHECK (((state)::text = ANY (ARRAY[('added'::character varying)::text, ('dropped'::character varying)::text, ('unchanged'::character varying)::text, ('changed'::character varying)::text, ('ambiguous'::character varying)::text, ('unmatched'::character varying)::text])))
);


ALTER TABLE public.revision_comparison_findings OWNER TO corridor;

--
-- Name: revision_comparison_findings_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.revision_comparison_findings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.revision_comparison_findings_id_seq OWNER TO corridor;

--
-- Name: revision_comparison_findings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.revision_comparison_findings_id_seq OWNED BY public.revision_comparison_findings.id;


--
-- Name: revision_comparison_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.revision_comparison_runs (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    predecessor_document_id bigint NOT NULL,
    successor_document_id bigint NOT NULL,
    predecessor_extraction_run_id bigint NOT NULL,
    successor_extraction_run_id bigint NOT NULL,
    predecessor_schema_version character varying(64),
    successor_schema_version character varying(64),
    predecessor_prompt_version character varying(64) NOT NULL,
    successor_prompt_version character varying(64) NOT NULL,
    predecessor_model character varying(64),
    successor_model character varying(64),
    matcher_version character varying(64) NOT NULL,
    matcher_config jsonb NOT NULL,
    predecessor_inputs_json jsonb NOT NULL,
    successor_inputs_json jsonb NOT NULL,
    finding_count integer NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    sealed_at timestamp with time zone,
    CONSTRAINT ck_revision_comparison_distinct_documents CHECK ((predecessor_document_id <> successor_document_id)),
    CONSTRAINT ck_revision_comparison_finding_count CHECK ((finding_count >= 0))
);


ALTER TABLE public.revision_comparison_runs OWNER TO corridor;

--
-- Name: revision_comparison_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.revision_comparison_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.revision_comparison_runs_id_seq OWNER TO corridor;

--
-- Name: revision_comparison_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.revision_comparison_runs_id_seq OWNED BY public.revision_comparison_runs.id;


--
-- Name: revision_reconciliation_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.revision_reconciliation_requests (
    project_id bigint NOT NULL,
    dirty_seq bigint DEFAULT '0'::bigint NOT NULL,
    reconciled_seq bigint DEFAULT '0'::bigint NOT NULL,
    last_reason text,
    requested_at timestamp with time zone,
    reconciled_at timestamp with time zone,
    CONSTRAINT ck_revision_reconciliation_requests_non_negative CHECK (((dirty_seq >= 0) AND (reconciled_seq >= 0))),
    CONSTRAINT ck_revision_reconciliation_requests_watermark_order CHECK ((reconciled_seq <= dirty_seq))
);


ALTER TABLE public.revision_reconciliation_requests OWNER TO corridor;

--
-- Name: schedule_governing_derivations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.schedule_governing_derivations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    source_name text NOT NULL,
    source_sha256 character varying(64),
    method character varying(24) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    matches_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_schedule_governing_matches CHECK ((jsonb_typeof(matches_json) = 'array'::text)),
    CONSTRAINT ck_schedule_governing_method CHECK (((method)::text = ANY (ARRAY[('coded'::character varying)::text, ('awaiting_pick'::character varying)::text, ('human_pick'::character varying)::text]))),
    CONSTRAINT ck_schedule_governing_recorded_by CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_schedule_governing_sha256 CHECK (((source_sha256 IS NULL) OR ((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);


ALTER TABLE public.schedule_governing_derivations OWNER TO corridor;

--
-- Name: schedule_governing_derivations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.schedule_governing_derivations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.schedule_governing_derivations_id_seq OWNER TO corridor;

--
-- Name: schedule_governing_derivations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.schedule_governing_derivations_id_seq OWNED BY public.schedule_governing_derivations.id;


--
-- Name: schedule_link_activations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.schedule_link_activations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    replay_case_count integer,
    reason character varying(160) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_schedule_link_activation_action CHECK (((action)::text = ANY (ARRAY[('activate'::character varying)::text, ('suspend'::character varying)::text]))),
    CONSTRAINT ck_schedule_link_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_schedule_link_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_schedule_link_activation_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.schedule_link_activations OWNER TO corridor;

--
-- Name: schedule_link_activations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.schedule_link_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.schedule_link_activations_id_seq OWNER TO corridor;

--
-- Name: schedule_link_activations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.schedule_link_activations_id_seq OWNED BY public.schedule_link_activations.id;


--
-- Name: schedule_link_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.schedule_link_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    milestone_id bigint NOT NULL,
    milestone_registration_id bigint NOT NULL,
    audit_log_id bigint NOT NULL,
    basis character varying(32) NOT NULL,
    decided_by character varying(128) NOT NULL,
    policy_version character varying(64),
    policy_sha256 character varying(64),
    deciding_values_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_schedule_link_receipts_basis CHECK (((basis)::text = ANY (ARRAY[('exact_station_containment'::character varying)::text, ('human_choice'::character varying)::text, ('flow_through'::character varying)::text]))),
    CONSTRAINT ck_schedule_link_receipts_decided_by CHECK ((length(TRIM(BOTH FROM decided_by)) > 0)),
    CONSTRAINT ck_schedule_link_receipts_sha256 CHECK (((policy_sha256 IS NULL) OR ((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_schedule_link_receipts_values CHECK ((jsonb_typeof(deciding_values_json) = 'object'::text))
);


ALTER TABLE public.schedule_link_receipts OWNER TO corridor;

--
-- Name: schedule_link_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.schedule_link_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.schedule_link_receipts_id_seq OWNER TO corridor;

--
-- Name: schedule_link_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.schedule_link_receipts_id_seq OWNED BY public.schedule_link_receipts.id;


--
-- Name: scheduled_report_publications; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.scheduled_report_publications (
    id bigint NOT NULL,
    public_id character varying(64) NOT NULL,
    occurrence_id bigint NOT NULL,
    schedule_id bigint NOT NULL,
    project_id bigint NOT NULL,
    configuration_version character varying(64) NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    predecessor_release_id bigint,
    prepared_artifact_id bigint,
    evaluated_on date NOT NULL,
    window_start date,
    comparison_window_days integer,
    ruleset_version character varying(64) NOT NULL,
    thresholds_json jsonb NOT NULL,
    snapshot_json jsonb NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_scheduled_report_publication_predecessor_window CHECK (((predecessor_release_id IS NULL) = (window_start IS NULL))),
    CONSTRAINT ck_scheduled_report_publication_provenance_mode CHECK (((provenance_mode)::text = ANY (ARRAY[('all-supported-sources'::character varying)::text, ('document-only'::character varying)::text]))),
    CONSTRAINT ck_scheduled_report_publication_snapshot_object CHECK ((jsonb_typeof(snapshot_json) = 'object'::text)),
    CONSTRAINT ck_scheduled_report_publication_thresholds_object CHECK ((jsonb_typeof(thresholds_json) = 'object'::text)),
    CONSTRAINT ck_scheduled_report_publication_window_days CHECK (((window_start IS NULL) = (comparison_window_days IS NULL))),
    CONSTRAINT ck_scheduled_report_publication_window_nonneg CHECK (((comparison_window_days IS NULL) OR (comparison_window_days >= 0)))
);


ALTER TABLE public.scheduled_report_publications OWNER TO corridor;

--
-- Name: scheduled_report_publications_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.scheduled_report_publications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.scheduled_report_publications_id_seq OWNER TO corridor;

--
-- Name: scheduled_report_publications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.scheduled_report_publications_id_seq OWNED BY public.scheduled_report_publications.id;


--
-- Name: sign_in_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.sign_in_attempts (
    id bigint NOT NULL,
    scope_kind character varying(32) NOT NULL,
    scope_value text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.sign_in_attempts OWNER TO corridor;

--
-- Name: sign_in_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.sign_in_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.sign_in_attempts_id_seq OWNER TO corridor;

--
-- Name: sign_in_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.sign_in_attempts_id_seq OWNED BY public.sign_in_attempts.id;


--
-- Name: sign_in_tokens; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.sign_in_tokens (
    id bigint NOT NULL,
    email_normalized text NOT NULL,
    token_sha256 character varying(64) NOT NULL,
    redirect_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    CONSTRAINT ck_sign_in_token_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT ck_sign_in_token_hash CHECK ((length((token_sha256)::text) = 64))
);


ALTER TABLE public.sign_in_tokens OWNER TO corridor;

--
-- Name: sign_in_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.sign_in_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.sign_in_tokens_id_seq OWNER TO corridor;

--
-- Name: sign_in_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.sign_in_tokens_id_seq OWNED BY public.sign_in_tokens.id;


--
-- Name: source_fact_append_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.source_fact_append_receipts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    idempotency_key character varying(160) NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_source_fact_append_content_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_source_fact_append_key CHECK ((length(TRIM(BOTH FROM idempotency_key)) > 0))
);


ALTER TABLE public.source_fact_append_receipts OWNER TO corridor;

--
-- Name: source_fact_append_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.source_fact_append_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.source_fact_append_receipts_id_seq OWNER TO corridor;

--
-- Name: source_fact_append_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.source_fact_append_receipts_id_seq OWNED BY public.source_fact_append_receipts.id;


--
-- Name: source_fetch_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.source_fetch_attempts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    reference_key character varying(64),
    location_id character varying(128) NOT NULL,
    attempted_at timestamp with time zone NOT NULL,
    outcome character varying(16) NOT NULL,
    resolved_url text,
    http_status integer,
    content_type text,
    byte_count integer,
    sha256 character varying(64),
    prior_sha256 character varying(64),
    document_id bigint,
    reason text,
    resumable boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_source_fetch_attempt_prior_sha256 CHECK (((prior_sha256 IS NULL) OR ((prior_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_source_fetch_attempt_sha256 CHECK (((sha256 IS NULL) OR ((sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT source_fetch_outcome CHECK (((outcome)::text = ANY (ARRAY[('registered'::character varying)::text, ('unchanged'::character varying)::text, ('drift'::character varying)::text, ('failed'::character varying)::text, ('budget_exhausted'::character varying)::text])))
);


ALTER TABLE public.source_fetch_attempts OWNER TO corridor;

--
-- Name: source_fetch_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.source_fetch_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.source_fetch_attempts_id_seq OWNER TO corridor;

--
-- Name: source_fetch_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.source_fetch_attempts_id_seq OWNED BY public.source_fetch_attempts.id;


--
-- Name: source_intake_draft_configurations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.source_intake_draft_configurations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    timeout_seconds integer NOT NULL,
    max_requests integer NOT NULL,
    retry_policy character varying(32) NOT NULL,
    retention_policy character varying(64) NOT NULL,
    observation_context character varying(128) NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_intake_draft_config_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_intake_draft_config_context CHECK ((length(TRIM(BOTH FROM observation_context)) > 0)),
    CONSTRAINT ck_intake_draft_config_input_budget CHECK (((max_input_tokens >= 1) AND (max_input_tokens <= 200000))),
    CONSTRAINT ck_intake_draft_config_model CHECK ((length(TRIM(BOTH FROM model)) > 0)),
    CONSTRAINT ck_intake_draft_config_no_retry CHECK (((retry_policy)::text = 'none'::text)),
    CONSTRAINT ck_intake_draft_config_one_request CHECK ((max_requests = 1)),
    CONSTRAINT ck_intake_draft_config_output_budget CHECK (((max_output_tokens >= 1) AND (max_output_tokens <= 20000))),
    CONSTRAINT ck_intake_draft_config_prompt CHECK ((length(TRIM(BOTH FROM prompt_version)) > 0)),
    CONSTRAINT ck_intake_draft_config_retention CHECK (((retention_policy)::text = ANY ((ARRAY['retained_indefinitely'::character varying, 'class_b_30_days'::character varying])::text[]))),
    CONSTRAINT ck_intake_draft_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);


ALTER TABLE public.source_intake_draft_configurations OWNER TO corridor;

--
-- Name: source_intake_draft_configurations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.source_intake_draft_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.source_intake_draft_configurations_id_seq OWNER TO corridor;

--
-- Name: source_intake_draft_configurations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.source_intake_draft_configurations_id_seq OWNED BY public.source_intake_draft_configurations.id;


--
-- Name: source_intake_draft_requests; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.source_intake_draft_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    staged_sha256 character varying(64) NOT NULL,
    filename text NOT NULL,
    declared_doc_type character varying(64) NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    state_token character varying(64) NOT NULL,
    permitted_pages_json jsonb,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    source_json jsonb,
    proposals_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb,
    usage_json jsonb,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    retention_class character varying(16) DEFAULT 'class_b'::character varying NOT NULL,
    retention_content_sha256 character varying(64),
    retention_deleted_at timestamp with time zone,
    CONSTRAINT ck_intake_draft_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_intake_draft_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_intake_draft_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_intake_draft_request_source_sha CHECK (((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_staged_sha CHECK (((staged_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_status CHECK (((status)::text = ANY (ARRAY[('completed'::character varying)::text, ('budget_exhausted'::character varying)::text, ('timeout'::character varying)::text, ('transport_failure'::character varying)::text, ('validation_refused'::character varying)::text, ('stale_input'::character varying)::text]))),
    CONSTRAINT ck_source_intake_draft_requests_retention_class CHECK (((retention_class)::text = 'class_b'::text)),
    CONSTRAINT ck_source_intake_draft_requests_retention_digest CHECK (((retention_content_sha256 IS NULL) OR ((retention_content_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);


ALTER TABLE public.source_intake_draft_requests OWNER TO corridor;

--
-- Name: source_intake_draft_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.source_intake_draft_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.source_intake_draft_requests_id_seq OWNER TO corridor;

--
-- Name: source_intake_draft_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.source_intake_draft_requests_id_seq OWNED BY public.source_intake_draft_requests.id;


--
-- Name: source_segments; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.source_segments (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint,
    kind character varying(32) NOT NULL,
    exact_text text NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    ordinal integer NOT NULL,
    sheet_name text,
    cell_range character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    page_no integer,
    start_offset integer,
    end_offset integer,
    statement_id bigint,
    CONSTRAINT ck_source_segments_content_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_source_segments_exact_text CHECK ((length(exact_text) > 0)),
    CONSTRAINT ck_source_segments_kind CHECK (((kind)::text = ANY ((ARRAY['spreadsheet_cell'::character varying, 'prose_span'::character varying, 'recorded_verbal_statement'::character varying])::text[]))),
    CONSTRAINT ck_source_segments_locator CHECK (((((kind)::text = 'spreadsheet_cell'::text) AND (document_id IS NOT NULL) AND (statement_id IS NULL) AND (length(sheet_name) > 0) AND ((cell_range)::text ~ '^[A-Z]+[1-9][0-9]*$'::text) AND (page_no IS NULL) AND (start_offset IS NULL) AND (end_offset IS NULL)) OR (((kind)::text = 'prose_span'::text) AND (document_id IS NOT NULL) AND (statement_id IS NULL) AND (sheet_name IS NULL) AND (cell_range IS NULL) AND (page_no > 0) AND (start_offset >= 0) AND (end_offset > start_offset)) OR (((kind)::text = 'recorded_verbal_statement'::text) AND (document_id IS NULL) AND (statement_id IS NOT NULL) AND (sheet_name IS NULL) AND (cell_range IS NULL) AND (page_no IS NULL) AND (start_offset IS NULL) AND (end_offset IS NULL)))),
    CONSTRAINT ck_source_segments_ordinal CHECK ((ordinal > 0))
);


ALTER TABLE public.source_segments OWNER TO corridor;

--
-- Name: source_segments_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.source_segments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.source_segments_id_seq OWNER TO corridor;

--
-- Name: source_segments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.source_segments_id_seq OWNED BY public.source_segments.id;


--
-- Name: stated_by_people; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.stated_by_people (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    display_name text NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    email_normalized text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_stated_by_people_display_name CHECK ((length(TRIM(BOTH FROM display_name)) > 0)),
    CONSTRAINT ck_stated_by_people_email CHECK (((email_normalized IS NULL) OR ((length(TRIM(BOTH FROM email_normalized)) > 0) AND (email_normalized = lower(TRIM(BOTH FROM email_normalized))))))
);


ALTER TABLE public.stated_by_people OWNER TO corridor;

--
-- Name: stated_by_people_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.stated_by_people_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.stated_by_people_id_seq OWNER TO corridor;

--
-- Name: stated_by_people_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.stated_by_people_id_seq OWNED BY public.stated_by_people.id;


--
-- Name: statement_coordination_receipts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_coordination_receipts (
    id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    commitment_lineage_id bigint NOT NULL,
    dependency_event_id bigint NOT NULL,
    scope_decision_id bigint NOT NULL,
    internal_owner_roster_entry_id bigint NOT NULL,
    internal_owner_decision_id bigint NOT NULL,
    next_action_decision_id bigint NOT NULL,
    milestone_impact_decision_id bigint,
    audit_log_id bigint NOT NULL,
    expected_predecessors_json jsonb NOT NULL,
    accepted_facts_json jsonb NOT NULL,
    candidate_payload_sha256 character varying(64) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    candidate_disposition_id bigint,
    CONSTRAINT ck_statement_coordination_receipt_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_statement_coordination_receipt_facts_object CHECK ((jsonb_typeof(accepted_facts_json) = 'object'::text)),
    CONSTRAINT ck_statement_coordination_receipt_predecessors_object CHECK ((jsonb_typeof(expected_predecessors_json) = 'object'::text))
);


ALTER TABLE public.statement_coordination_receipts OWNER TO corridor;

--
-- Name: statement_coordination_receipts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_coordination_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_coordination_receipts_id_seq OWNER TO corridor;

--
-- Name: statement_coordination_receipts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_coordination_receipts_id_seq OWNED BY public.statement_coordination_receipts.id;


--
-- Name: statement_coordination_reversal_effects; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_coordination_reversal_effects (
    id bigint NOT NULL,
    reversal_id bigint NOT NULL,
    effect_kind character varying(32) NOT NULL,
    target_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_coordination_reversal_effects_kind CHECK (((effect_kind)::text = ANY (ARRAY[('statement'::character varying)::text, ('scope_decision'::character varying)::text, ('work_decision'::character varying)::text, ('milestone_link'::character varying)::text, ('candidate_disposition'::character varying)::text, ('candidate_projection'::character varying)::text, ('lineage_projection'::character varying)::text, ('audit_pointer'::character varying)::text, ('grouping_receipt'::character varying)::text])))
);


ALTER TABLE public.statement_coordination_reversal_effects OWNER TO corridor;

--
-- Name: statement_coordination_reversal_effects_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_coordination_reversal_effects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_coordination_reversal_effects_id_seq OWNER TO corridor;

--
-- Name: statement_coordination_reversal_effects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_coordination_reversal_effects_id_seq OWNED BY public.statement_coordination_reversal_effects.id;


--
-- Name: statement_coordination_reversals; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_coordination_reversals (
    id bigint NOT NULL,
    receipt_id bigint,
    candidate_disposition_id bigint,
    candidate_id bigint NOT NULL,
    audit_log_id bigint NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_coordination_reversals_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_statement_coordination_reversals_one_source CHECK ((((receipt_id IS NOT NULL) AND (candidate_disposition_id IS NULL)) OR ((receipt_id IS NULL) AND (candidate_disposition_id IS NOT NULL))))
);


ALTER TABLE public.statement_coordination_reversals OWNER TO corridor;

--
-- Name: statement_coordination_reversals_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_coordination_reversals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_coordination_reversals_id_seq OWNER TO corridor;

--
-- Name: statement_coordination_reversals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_coordination_reversals_id_seq OWNED BY public.statement_coordination_reversals.id;


--
-- Name: statement_suggestion_eligibility_declarations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_suggestion_eligibility_declarations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    contract_version character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_suggestion_eligibility_contract CHECK ((length(TRIM(BOTH FROM contract_version)) > 0))
);


ALTER TABLE public.statement_suggestion_eligibility_declarations OWNER TO corridor;

--
-- Name: statement_suggestion_eligibility_declarations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq OWNER TO corridor;

--
-- Name: statement_suggestion_eligibility_declarations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq OWNED BY public.statement_suggestion_eligibility_declarations.id;


--
-- Name: statement_suggestion_protection_ends; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_suggestion_protection_ends (
    id bigint NOT NULL,
    protection_id bigint NOT NULL,
    ended_at timestamp with time zone NOT NULL
);


ALTER TABLE public.statement_suggestion_protection_ends OWNER TO corridor;

--
-- Name: statement_suggestion_protection_ends_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_suggestion_protection_ends_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_suggestion_protection_ends_id_seq OWNER TO corridor;

--
-- Name: statement_suggestion_protection_ends_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_suggestion_protection_ends_id_seq OWNED BY public.statement_suggestion_protection_ends.id;


--
-- Name: statement_suggestion_protections; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.statement_suggestion_protections (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    observation_contract character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_suggestion_protection_contract CHECK ((length(TRIM(BOTH FROM observation_contract)) > 0)),
    CONSTRAINT ck_statement_suggestion_protection_kind CHECK (((kind)::text = ANY (ARRAY[('shadow_cohort'::character varying)::text, ('no_agent_baseline'::character varying)::text])))
);


ALTER TABLE public.statement_suggestion_protections OWNER TO corridor;

--
-- Name: statement_suggestion_protections_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.statement_suggestion_protections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.statement_suggestion_protections_id_seq OWNER TO corridor;

--
-- Name: statement_suggestion_protections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.statement_suggestion_protections_id_seq OWNED BY public.statement_suggestion_protections.id;


--
-- Name: subject_candidate_suggestions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.subject_candidate_suggestions (
    id bigint NOT NULL,
    attempt_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    rank integer NOT NULL,
    model character varying(64) NOT NULL,
    prompt_version character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_subject_candidate_suggestion_model CHECK (((length(TRIM(BOTH FROM model)) > 0) AND (length(TRIM(BOTH FROM prompt_version)) > 0))),
    CONSTRAINT ck_subject_candidate_suggestion_rank CHECK ((rank > 0))
);


ALTER TABLE public.subject_candidate_suggestions OWNER TO corridor;

--
-- Name: subject_candidate_suggestions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.subject_candidate_suggestions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subject_candidate_suggestions_id_seq OWNER TO corridor;

--
-- Name: subject_candidate_suggestions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.subject_candidate_suggestions_id_seq OWNED BY public.subject_candidate_suggestions.id;


--
-- Name: subject_resolution_attempts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.subject_resolution_attempts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    source_segment_id bigint NOT NULL,
    reference_kind character varying(48) NOT NULL,
    raw_reference text NOT NULL,
    normalized_reference text NOT NULL,
    expected_subject_type character varying(32) NOT NULL,
    usage character varying(32) NOT NULL,
    state character varying(24) NOT NULL,
    attention_reason character varying(64),
    rule_identity character varying(64) NOT NULL,
    resolved_external_org_id bigint,
    resolved_stated_by_person_id bigint,
    resolved_dependency_id bigint,
    resolved_document_id bigint,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_subject_resolution_attention CHECK (((((state)::text = 'resolved'::text) AND (attention_reason IS NULL)) OR (((state)::text <> 'resolved'::text) AND (length(TRIM(BOTH FROM attention_reason)) > 0)))),
    CONSTRAINT ck_subject_resolution_expected_type CHECK (((expected_subject_type)::text = ANY ((ARRAY['external_org'::character varying, 'person'::character varying, 'constraint'::character varying, 'document'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_reference CHECK (((length(TRIM(BOTH FROM raw_reference)) > 0) AND (length(TRIM(BOTH FROM normalized_reference)) > 0))),
    CONSTRAINT ck_subject_resolution_reference_kind CHECK (((reference_kind)::text = ANY ((ARRAY['organization_name'::character varying, 'email_sender'::character varying, 'email_domain'::character varying, 'person_name'::character varying, 'person_email'::character varying, 'source_identifier'::character varying, 'activity_identifier'::character varying, 'document_identifier'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_rule CHECK (((rule_identity)::text = 'exact-registered-alias-v1'::text)),
    CONSTRAINT ck_subject_resolution_sha256 CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_subject_resolution_state CHECK (((state)::text = ANY ((ARRAY['resolved'::character varying, 'unresolved'::character varying, 'conflict'::character varying, 'stale'::character varying, 'actor_boundary'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_target CHECK (((((state)::text = 'resolved'::text) AND (num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, resolved_dependency_id, resolved_document_id) = 1) AND ((((expected_subject_type)::text = 'external_org'::text) AND (resolved_external_org_id IS NOT NULL)) OR (((expected_subject_type)::text = 'person'::text) AND (resolved_stated_by_person_id IS NOT NULL)) OR (((expected_subject_type)::text = 'constraint'::text) AND (resolved_dependency_id IS NOT NULL)) OR (((expected_subject_type)::text = 'document'::text) AND (resolved_document_id IS NOT NULL)))) OR (((state)::text <> 'resolved'::text) AND (num_nonnulls(resolved_external_org_id, resolved_stated_by_person_id, resolved_dependency_id, resolved_document_id) = 0)))),
    CONSTRAINT ck_subject_resolution_usage CHECK (((usage)::text = ANY ((ARRAY['identity'::character varying, 'statement_speaker'::character varying, 'affected_subject'::character varying])::text[])))
);


ALTER TABLE public.subject_resolution_attempts OWNER TO corridor;

--
-- Name: subject_resolution_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.subject_resolution_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subject_resolution_attempts_id_seq OWNER TO corridor;

--
-- Name: subject_resolution_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.subject_resolution_attempts_id_seq OWNED BY public.subject_resolution_attempts.id;


--
-- Name: subject_resolution_candidates; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.subject_resolution_candidates (
    id bigint NOT NULL,
    attempt_id bigint NOT NULL,
    subject_type character varying(32) NOT NULL,
    subject_key character varying(96) NOT NULL,
    external_org_id bigint,
    stated_by_person_id bigint,
    dependency_id bigint,
    document_id bigint,
    display_name text NOT NULL,
    candidate_state character varying(16) NOT NULL,
    match_source character varying(64) NOT NULL,
    CONSTRAINT ck_subject_resolution_candidate_match_source CHECK (((match_source)::text = ANY ((ARRAY['registered_alias'::character varying, 'human_alias_decision'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_candidate_state CHECK (((candidate_state)::text = ANY ((ARRAY['active'::character varying, 'stale'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_candidate_target CHECK (((num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1) AND ((((subject_type)::text = 'external_org'::text) AND (external_org_id IS NOT NULL)) OR (((subject_type)::text = 'person'::text) AND (stated_by_person_id IS NOT NULL)) OR (((subject_type)::text = 'constraint'::text) AND (dependency_id IS NOT NULL)) OR (((subject_type)::text = 'document'::text) AND (document_id IS NOT NULL))))),
    CONSTRAINT ck_subject_resolution_candidate_text CHECK (((length(TRIM(BOTH FROM subject_key)) > 0) AND (length(TRIM(BOTH FROM display_name)) > 0))),
    CONSTRAINT ck_subject_resolution_candidate_type CHECK (((subject_type)::text = ANY ((ARRAY['external_org'::character varying, 'person'::character varying, 'constraint'::character varying, 'document'::character varying])::text[])))
);


ALTER TABLE public.subject_resolution_candidates OWNER TO corridor;

--
-- Name: subject_resolution_candidates_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.subject_resolution_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subject_resolution_candidates_id_seq OWNER TO corridor;

--
-- Name: subject_resolution_candidates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.subject_resolution_candidates_id_seq OWNED BY public.subject_resolution_candidates.id;


--
-- Name: subject_resolution_decisions; Type: TABLE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TABLE public.subject_resolution_decisions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    attempt_id bigint NOT NULL,
    revision_id bigint NOT NULL,
    source_document_id bigint NOT NULL,
    source_segment_id bigint NOT NULL,
    reference_kind character varying(48) NOT NULL,
    raw_reference text NOT NULL,
    normalized_reference text NOT NULL,
    decision_kind character varying(48) NOT NULL,
    subject_type character varying(32) NOT NULL,
    external_org_id bigint,
    stated_by_person_id bigint,
    dependency_id bigint,
    document_id bigint,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_subject_resolution_decision_actor CHECK (((length(TRIM(BOTH FROM recorded_by)) > 0) AND (length(TRIM(BOTH FROM normalized_reference)) > 0))),
    CONSTRAINT ck_subject_resolution_decision_kind CHECK (((decision_kind)::text = 'human_alias_registration'::text)),
    CONSTRAINT ck_subject_resolution_decision_reference_kind CHECK (((reference_kind)::text = ANY ((ARRAY['organization_name'::character varying, 'email_sender'::character varying, 'email_domain'::character varying, 'person_name'::character varying, 'person_email'::character varying, 'source_identifier'::character varying, 'activity_identifier'::character varying, 'document_identifier'::character varying])::text[]))),
    CONSTRAINT ck_subject_resolution_decision_target CHECK (((num_nonnulls(external_org_id, stated_by_person_id, dependency_id, document_id) = 1) AND ((((subject_type)::text = 'external_org'::text) AND (external_org_id IS NOT NULL)) OR (((subject_type)::text = 'person'::text) AND (stated_by_person_id IS NOT NULL)) OR (((subject_type)::text = 'constraint'::text) AND (dependency_id IS NOT NULL)) OR (((subject_type)::text = 'document'::text) AND (document_id IS NOT NULL))))),
    CONSTRAINT ck_subject_resolution_decision_type CHECK (((subject_type)::text = ANY ((ARRAY['external_org'::character varying, 'person'::character varying, 'constraint'::character varying, 'document'::character varying])::text[])))
);


ALTER TABLE public.subject_resolution_decisions OWNER TO corridor_fact_decision_writer;

--
-- Name: subject_resolution_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE SEQUENCE public.subject_resolution_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.subject_resolution_decisions_id_seq OWNER TO corridor_fact_decision_writer;

--
-- Name: subject_resolution_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER SEQUENCE public.subject_resolution_decisions_id_seq OWNED BY public.subject_resolution_decisions.id;


--
-- Name: token_layers; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.token_layers (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    origin character varying(8) NOT NULL,
    layer_key character varying(64) NOT NULL,
    source_sha256 character varying(64) NOT NULL,
    engine_json jsonb NOT NULL,
    token_count integer NOT NULL,
    quality_json jsonb NOT NULL,
    artifact_path text NOT NULL,
    artifact_sha256 character varying(64) NOT NULL,
    artifact_bytes bigint NOT NULL,
    retention_class character varying(32) DEFAULT 'intermediary_processing'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_token_layers_artifact_bytes CHECK ((artifact_bytes > 0)),
    CONSTRAINT ck_token_layers_artifact_sha256 CHECK (((artifact_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_token_layers_origin CHECK (((origin)::text = ANY ((ARRAY['native'::character varying, 'ocr'::character varying])::text[]))),
    CONSTRAINT ck_token_layers_page_no CHECK ((page_no > 0)),
    CONSTRAINT ck_token_layers_retention_class CHECK (((retention_class)::text = 'intermediary_processing'::text)),
    CONSTRAINT ck_token_layers_source_sha256 CHECK (((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_token_layers_token_count CHECK ((token_count >= 0))
);


ALTER TABLE public.token_layers OWNER TO corridor;

--
-- Name: token_layers_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.token_layers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.token_layers_id_seq OWNER TO corridor;

--
-- Name: token_layers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.token_layers_id_seq OWNED BY public.token_layers.id;


--
-- Name: unreadable_cell_admission_activations; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.unreadable_cell_admission_activations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    policy_version character varying(64) NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    replay_case_count integer,
    reason character varying(160) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_unreadable_cell_admission_action CHECK (((action)::text = ANY (ARRAY[('activate'::character varying)::text, ('suspend'::character varying)::text]))),
    CONSTRAINT ck_unreadable_cell_admission_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_unreadable_cell_admission_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_unreadable_cell_admission_sha CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);


ALTER TABLE public.unreadable_cell_admission_activations OWNER TO corridor;

--
-- Name: unreadable_cell_admission_activations_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.unreadable_cell_admission_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.unreadable_cell_admission_activations_id_seq OWNER TO corridor;

--
-- Name: unreadable_cell_admission_activations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.unreadable_cell_admission_activations_id_seq OWNED BY public.unreadable_cell_admission_activations.id;


--
-- Name: unreadable_cell_reading_profiles; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.unreadable_cell_reading_profiles (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    profile_version character varying(64) NOT NULL,
    min_readable_text_chars integer NOT NULL,
    page_scope_json jsonb NOT NULL,
    image_op_identities_json jsonb NOT NULL,
    read_identities_json jsonb NOT NULL,
    max_cells_per_page integer NOT NULL,
    max_image_ops_per_cell integer NOT NULL,
    max_reads_per_cell integer NOT NULL,
    max_corpus_reads_per_cell integer NOT NULL,
    timeout_seconds integer NOT NULL,
    created_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_unreadable_cell_profile_actor CHECK ((length(TRIM(BOTH FROM created_by)) > 0)),
    CONSTRAINT ck_unreadable_cell_profile_image_ops CHECK ((jsonb_typeof(image_op_identities_json) = 'array'::text)),
    CONSTRAINT ck_unreadable_cell_profile_max_cells CHECK (((max_cells_per_page >= 1) AND (max_cells_per_page <= 10000))),
    CONSTRAINT ck_unreadable_cell_profile_max_corpus_reads CHECK (((max_corpus_reads_per_cell >= 1) AND (max_corpus_reads_per_cell <= 100))),
    CONSTRAINT ck_unreadable_cell_profile_max_image_ops CHECK (((max_image_ops_per_cell >= 1) AND (max_image_ops_per_cell <= 100))),
    CONSTRAINT ck_unreadable_cell_profile_max_reads CHECK (((max_reads_per_cell >= 1) AND (max_reads_per_cell <= 100))),
    CONSTRAINT ck_unreadable_cell_profile_min_chars CHECK (((min_readable_text_chars >= 1) AND (min_readable_text_chars <= 100000))),
    CONSTRAINT ck_unreadable_cell_profile_page_scope CHECK ((jsonb_typeof(page_scope_json) = 'array'::text)),
    CONSTRAINT ck_unreadable_cell_profile_reads CHECK ((jsonb_typeof(read_identities_json) = 'array'::text)),
    CONSTRAINT ck_unreadable_cell_profile_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600))),
    CONSTRAINT ck_unreadable_cell_profile_version CHECK ((length(TRIM(BOTH FROM profile_version)) > 0))
);


ALTER TABLE public.unreadable_cell_reading_profiles OWNER TO corridor;

--
-- Name: unreadable_cell_reading_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.unreadable_cell_reading_profiles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.unreadable_cell_reading_profiles_id_seq OWNER TO corridor;

--
-- Name: unreadable_cell_reading_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.unreadable_cell_reading_profiles_id_seq OWNED BY public.unreadable_cell_reading_profiles.id;


--
-- Name: unreadable_cell_reading_runs; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.unreadable_cell_reading_runs (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    cell_key character varying(128) NOT NULL,
    profile_id bigint,
    profile_version character varying(64),
    page_image_sha256 character varying(64) NOT NULL,
    read_fingerprint character varying(64),
    terminal_state character varying(32) NOT NULL,
    reason character varying(160),
    outcome_json jsonb,
    validator_outcome character varying(32) NOT NULL,
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_unreadable_cell_run_cell_key CHECK ((length(TRIM(BOTH FROM cell_key)) > 0)),
    CONSTRAINT ck_unreadable_cell_run_fingerprint CHECK (((read_fingerprint IS NULL) OR ((read_fingerprint)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_unreadable_cell_run_image_sha CHECK (((page_image_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_unreadable_cell_run_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_unreadable_cell_run_state CHECK (((terminal_state)::text = ANY (ARRAY[('rescued'::character varying)::text, ('corroborated'::character varying)::text, ('reading_only'::character varying)::text, ('failure'::character varying)::text, ('stale_input'::character varying)::text, ('budget_exhausted'::character varying)::text, ('refused'::character varying)::text, ('validation_refused'::character varying)::text, ('runtime_failure'::character varying)::text])))
);


ALTER TABLE public.unreadable_cell_reading_runs OWNER TO corridor;

--
-- Name: unreadable_cell_reading_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.unreadable_cell_reading_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.unreadable_cell_reading_runs_id_seq OWNER TO corridor;

--
-- Name: unreadable_cell_reading_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.unreadable_cell_reading_runs_id_seq OWNED BY public.unreadable_cell_reading_runs.id;


--
-- Name: unreadable_cell_reading_steps; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.unreadable_cell_reading_steps (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    ordinal integer NOT NULL,
    step_type character varying(24) NOT NULL,
    name character varying(128) NOT NULL,
    arguments_json jsonb NOT NULL,
    result_summary_json jsonb NOT NULL,
    request_sha256 character varying(64) NOT NULL,
    result_sha256 character varying(64) NOT NULL,
    CONSTRAINT ck_unreadable_cell_step_type CHECK (((step_type)::text = ANY (ARRAY[('image_op'::character varying)::text, ('read'::character varying)::text, ('corpus_read'::character varying)::text])))
);


ALTER TABLE public.unreadable_cell_reading_steps OWNER TO corridor;

--
-- Name: unreadable_cell_reading_steps_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.unreadable_cell_reading_steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.unreadable_cell_reading_steps_id_seq OWNER TO corridor;

--
-- Name: unreadable_cell_reading_steps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.unreadable_cell_reading_steps_id_seq OWNED BY public.unreadable_cell_reading_steps.id;


--
-- Name: unreadable_cell_resolutions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.unreadable_cell_resolutions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    cell_key character varying(128) NOT NULL,
    state character varying(24) NOT NULL,
    value text,
    run_id bigint,
    corroboration_document_id bigint,
    corroboration_page_no integer,
    corroboration_quote text,
    origin character varying(32) NOT NULL,
    policy_version character varying(64),
    policy_sha256 character varying(64),
    recorded_by character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_unreadable_cell_resolution_cell_key CHECK ((length(TRIM(BOTH FROM cell_key)) > 0)),
    CONSTRAINT ck_unreadable_cell_resolution_origin CHECK (((origin)::text = ANY (ARRAY[('harness'::character varying)::text, ('corroboration_upgrade'::character varying)::text, ('admission'::character varying)::text, ('human_decision'::character varying)::text]))),
    CONSTRAINT ck_unreadable_cell_resolution_sha CHECK (((policy_sha256 IS NULL) OR ((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_unreadable_cell_resolution_state CHECK (((state)::text = ANY (ARRAY[('unconfirmed'::character varying)::text, ('corroborated'::character varying)::text, ('absent'::character varying)::text, ('admitted'::character varying)::text])))
);


ALTER TABLE public.unreadable_cell_resolutions OWNER TO corridor;

--
-- Name: unreadable_cell_resolutions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.unreadable_cell_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.unreadable_cell_resolutions_id_seq OWNER TO corridor;

--
-- Name: unreadable_cell_resolutions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.unreadable_cell_resolutions_id_seq OWNED BY public.unreadable_cell_resolutions.id;


--
-- Name: web_sessions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.web_sessions (
    id bigint NOT NULL,
    session_sha256 character varying(64) NOT NULL,
    csrf_sha256 character varying(64) NOT NULL,
    principal_subject character varying(128) NOT NULL,
    email_normalized text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_web_session_csrf CHECK ((length((csrf_sha256)::text) = 64)),
    CONSTRAINT ck_web_session_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT ck_web_session_hash CHECK ((length((session_sha256)::text) = 64)),
    CONSTRAINT ck_web_session_principal CHECK ((length(TRIM(BOTH FROM principal_subject)) > 0))
);


ALTER TABLE public.web_sessions OWNER TO corridor;

--
-- Name: web_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.web_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.web_sessions_id_seq OWNER TO corridor;

--
-- Name: web_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.web_sessions_id_seq OWNED BY public.web_sessions.id;


--
-- Name: work_decision_milestone_impacts; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.work_decision_milestone_impacts (
    id bigint NOT NULL,
    work_decision_id bigint NOT NULL,
    milestone_id bigint NOT NULL
);


ALTER TABLE public.work_decision_milestone_impacts OWNER TO corridor;

--
-- Name: work_decision_milestone_impacts_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.work_decision_milestone_impacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.work_decision_milestone_impacts_id_seq OWNER TO corridor;

--
-- Name: work_decision_milestone_impacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.work_decision_milestone_impacts_id_seq OWNED BY public.work_decision_milestone_impacts.id;


--
-- Name: work_decisions; Type: TABLE; Schema: public; Owner: corridor
--

CREATE TABLE public.work_decisions (
    id bigint NOT NULL,
    dependency_id bigint,
    decision_type character varying(32) NOT NULL,
    field character varying(32) NOT NULL,
    before_value text,
    after_value text,
    recorded_by character varying(128) NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    predecessor_decision_id bigint,
    commitment_lineage_id bigint,
    action_due_date_reason character varying(64),
    no_follow_up_reason character varying(64),
    cancellation_reason character varying(64),
    note text,
    deferral_reason character varying(64),
    deferral_return_date date,
    observed_statement_event_id bigint,
    observed_scope_decision_id bigint,
    observed_milestone_impact_decision_id bigint,
    CONSTRAINT ck_work_decisions_deferral_shape CHECK (((((field)::text <> 'deferral'::text) AND (deferral_reason IS NULL) AND (deferral_return_date IS NULL)) OR (((field)::text = 'deferral'::text) AND (((after_value IS NULL) AND (deferral_reason IS NULL) AND (deferral_return_date IS NULL)) OR ((after_value IS NOT NULL) AND ((deferral_reason)::text = ANY (ARRAY[('waiting_for_information'::character varying)::text, ('waiting_for_external_party'::character varying)::text, ('assigned_to_someone_else'::character varying)::text])) AND (deferral_return_date IS NOT NULL)))))),
    CONSTRAINT ck_work_decisions_exactly_one_subject CHECK ((((dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR ((dependency_id IS NULL) AND (commitment_lineage_id IS NOT NULL)))),
    CONSTRAINT ck_work_decisions_field CHECK (((field)::text = ANY (ARRAY[('internal_owner'::character varying)::text, ('next_action'::character varying)::text, ('milestone_impact'::character varying)::text, ('deferral'::character varying)::text])))
);


ALTER TABLE public.work_decisions OWNER TO corridor;

--
-- Name: work_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: corridor
--

CREATE SEQUENCE public.work_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.work_decisions_id_seq OWNER TO corridor;

--
-- Name: work_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: corridor
--

ALTER SEQUENCE public.work_decisions_id_seq OWNED BY public.work_decisions.id;


--
-- Name: active_run_declarations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations ALTER COLUMN id SET DEFAULT nextval('public.active_run_declarations_id_seq'::regclass);


--
-- Name: assertions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assertions ALTER COLUMN id SET DEFAULT nextval('public.assertions_id_seq'::regclass);


--
-- Name: assignment_notification_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_attempts_id_seq'::regclass);


--
-- Name: assignment_notification_dispatches id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_dispatches_id_seq'::regclass);


--
-- Name: assignment_notification_feedback id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_feedback_id_seq'::regclass);


--
-- Name: assignment_notifications id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications ALTER COLUMN id SET DEFAULT nextval('public.assignment_notifications_id_seq'::regclass);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: automatic_carry_forward_outcomes id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes ALTER COLUMN id SET DEFAULT nextval('public.automatic_carry_forward_outcomes_id_seq'::regclass);


--
-- Name: candidate_dispositions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidate_dispositions ALTER COLUMN id SET DEFAULT nextval('public.candidate_dispositions_id_seq'::regclass);


--
-- Name: candidates id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates ALTER COLUMN id SET DEFAULT nextval('public.candidates_id_seq'::regclass);


--
-- Name: cohort_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.cohort_receipts ALTER COLUMN id SET DEFAULT nextval('public.cohort_receipts_id_seq'::regclass);


--
-- Name: commitment_lineages id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.commitment_lineages ALTER COLUMN id SET DEFAULT nextval('public.commitment_lineages_id_seq'::regclass);


--
-- Name: condition_resolutions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.condition_resolutions ALTER COLUMN id SET DEFAULT nextval('public.condition_resolutions_id_seq'::regclass);


--
-- Name: coordination_summary_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_configurations ALTER COLUMN id SET DEFAULT nextval('public.coordination_summary_configurations_id_seq'::regclass);


--
-- Name: coordination_summary_requests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests ALTER COLUMN id SET DEFAULT nextval('public.coordination_summary_requests_id_seq'::regclass);


--
-- Name: dependencies id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies ALTER COLUMN id SET DEFAULT nextval('public.dependencies_id_seq'::regclass);


--
-- Name: dependency_admission_outcomes id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_admission_outcomes ALTER COLUMN id SET DEFAULT nextval('public.dependency_admission_outcomes_id_seq'::regclass);


--
-- Name: dependency_dismissals id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_dismissals ALTER COLUMN id SET DEFAULT nextval('public.dependency_dismissals_id_seq'::regclass);


--
-- Name: dependency_event_scope_decisions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scope_decisions ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_scope_decisions_id_seq'::regclass);


--
-- Name: dependency_event_scopes id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_scopes_id_seq'::regclass);


--
-- Name: dependency_event_timings id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_timings ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_timings_id_seq'::regclass);


--
-- Name: dependency_events id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events ALTER COLUMN id SET DEFAULT nextval('public.dependency_events_id_seq'::regclass);


--
-- Name: dependency_evidence_sufficiencies id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies ALTER COLUMN id SET DEFAULT nextval('public.dependency_evidence_sufficiencies_id_seq'::regclass);


--
-- Name: discovered_references id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.discovered_references ALTER COLUMN id SET DEFAULT nextval('public.discovered_references_id_seq'::regclass);


--
-- Name: dispute_history_resolutions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions ALTER COLUMN id SET DEFAULT nextval('public.dispute_history_resolutions_id_seq'::regclass);


--
-- Name: dispute_settlements id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_settlements ALTER COLUMN id SET DEFAULT nextval('public.dispute_settlements_id_seq'::regclass);


--
-- Name: doc_pages id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.doc_pages ALTER COLUMN id SET DEFAULT nextval('public.doc_pages_id_seq'::regclass);


--
-- Name: document_notification_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.document_notification_attempts_id_seq'::regclass);


--
-- Name: document_notification_dispatches id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.document_notification_dispatches_id_seq'::regclass);


--
-- Name: document_notifications id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications ALTER COLUMN id SET DEFAULT nextval('public.document_notifications_id_seq'::regclass);


--
-- Name: document_rendition_derivations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations ALTER COLUMN id SET DEFAULT nextval('public.document_rendition_derivations_id_seq'::regclass);


--
-- Name: documentation_field_confirmations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documentation_field_confirmations ALTER COLUMN id SET DEFAULT nextval('public.documentation_field_confirmations_id_seq'::regclass);


--
-- Name: documents id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);


--
-- Name: due_action_notification_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.due_action_notification_attempts_id_seq'::regclass);


--
-- Name: due_action_notification_dispatches id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.due_action_notification_dispatches_id_seq'::regclass);


--
-- Name: due_action_notifications id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications ALTER COLUMN id SET DEFAULT nextval('public.due_action_notifications_id_seq'::regclass);


--
-- Name: due_work_occurrences id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_occurrences ALTER COLUMN id SET DEFAULT nextval('public.due_work_occurrences_id_seq'::regclass);


--
-- Name: due_work_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts ALTER COLUMN id SET DEFAULT nextval('public.due_work_receipts_id_seq'::regclass);


--
-- Name: due_work_schedules id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_schedules ALTER COLUMN id SET DEFAULT nextval('public.due_work_schedules_id_seq'::regclass);


--
-- Name: event_admission_acceptance_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_acceptance_receipts ALTER COLUMN id SET DEFAULT nextval('public.event_admission_acceptance_receipts_id_seq'::regclass);


--
-- Name: event_admission_activations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_activations ALTER COLUMN id SET DEFAULT nextval('public.event_admission_activations_id_seq'::regclass);


--
-- Name: event_admission_outcomes id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes ALTER COLUMN id SET DEFAULT nextval('public.event_admission_outcomes_id_seq'::regclass);


--
-- Name: event_cohort_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_cohort_receipts ALTER COLUMN id SET DEFAULT nextval('public.event_cohort_receipts_id_seq'::regclass);


--
-- Name: evidence_investigation_candidate_review_starts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_candidate_review_starts_id_seq'::regclass);


--
-- Name: evidence_investigation_capture_contracts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_contracts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_capture_contracts_id_seq'::regclass);


--
-- Name: evidence_investigation_capture_results id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_capture_results_id_seq'::regclass);


--
-- Name: evidence_investigation_evaluation_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_evaluation_receipts_id_seq'::regclass);


--
-- Name: evidence_investigation_packet_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_packet_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_packet_receipts_id_seq'::regclass);


--
-- Name: evidence_investigation_review_observations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_review_observations ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_review_observations_id_seq'::regclass);


--
-- Name: evidence_investigation_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_runs_id_seq'::regclass);


--
-- Name: evidence_investigation_shadow_cases id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_cases_id_seq'::regclass);


--
-- Name: evidence_investigation_shadow_executions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_executions_id_seq'::regclass);


--
-- Name: evidence_investigation_shadow_outcomes id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_outcomes_id_seq'::regclass);


--
-- Name: evidence_investigation_step_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_step_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_step_receipts_id_seq'::regclass);


--
-- Name: evidence_links id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_links ALTER COLUMN id SET DEFAULT nextval('public.evidence_links_id_seq'::regclass);


--
-- Name: external_orgs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_orgs ALTER COLUMN id SET DEFAULT nextval('public.external_orgs_id_seq'::regclass);


--
-- Name: external_report_artifacts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_artifacts ALTER COLUMN id SET DEFAULT nextval('public.external_report_artifacts_id_seq'::regclass);


--
-- Name: external_report_releases id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_releases ALTER COLUMN id SET DEFAULT nextval('public.external_report_releases_id_seq'::regclass);


--
-- Name: extracted_proposal_facts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts ALTER COLUMN id SET DEFAULT nextval('public.extracted_proposal_facts_id_seq'::regclass);


--
-- Name: extracted_proposals id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals ALTER COLUMN id SET DEFAULT nextval('public.extracted_proposals_id_seq'::regclass);


--
-- Name: extraction_failure_diagnosis_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations ALTER COLUMN id SET DEFAULT nextval('public.extraction_failure_diagnosis_configurations_id_seq'::regclass);


--
-- Name: extraction_failure_diagnosis_requests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests ALTER COLUMN id SET DEFAULT nextval('public.extraction_failure_diagnosis_requests_id_seq'::regclass);


--
-- Name: extraction_measurement_case_states id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states ALTER COLUMN id SET DEFAULT nextval('public.extraction_measurement_case_states_id_seq'::regclass);


--
-- Name: extraction_run_candidates id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates ALTER COLUMN id SET DEFAULT nextval('public.extraction_run_candidates_id_seq'::regclass);


--
-- Name: extraction_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_runs ALTER COLUMN id SET DEFAULT nextval('public.extraction_runs_id_seq'::regclass);


--
-- Name: fact_applies_to id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to ALTER COLUMN id SET DEFAULT nextval('public.fact_applies_to_id_seq'::regclass);


--
-- Name: fact_closure_results id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results ALTER COLUMN id SET DEFAULT nextval('public.fact_closure_results_id_seq'::regclass);


--
-- Name: fact_closure_sources id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources ALTER COLUMN id SET DEFAULT nextval('public.fact_closure_sources_id_seq'::regclass);


--
-- Name: fact_decisions id; Type: DEFAULT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions ALTER COLUMN id SET DEFAULT nextval('public.fact_decisions_id_seq'::regclass);


--
-- Name: fact_dispositions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions ALTER COLUMN id SET DEFAULT nextval('public.fact_dispositions_id_seq'::regclass);


--
-- Name: fact_sources id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources ALTER COLUMN id SET DEFAULT nextval('public.fact_sources_id_seq'::regclass);


--
-- Name: fact_statement_timings id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_statement_timings ALTER COLUMN id SET DEFAULT nextval('public.fact_statement_timings_id_seq'::regclass);


--
-- Name: facts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts ALTER COLUMN id SET DEFAULT nextval('public.facts_id_seq'::regclass);


--
-- Name: follow_up_plan_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts ALTER COLUMN id SET DEFAULT nextval('public.follow_up_plan_receipts_id_seq'::regclass);


--
-- Name: follow_up_plan_reversals id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals ALTER COLUMN id SET DEFAULT nextval('public.follow_up_plan_reversals_id_seq'::regclass);


--
-- Name: inbound_messages id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages ALTER COLUMN id SET DEFAULT nextval('public.inbound_messages_id_seq'::regclass);


--
-- Name: inbound_route_triage id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_route_triage ALTER COLUMN id SET DEFAULT nextval('public.inbound_route_triage_id_seq'::regclass);


--
-- Name: inbound_thread_readings id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings ALTER COLUMN id SET DEFAULT nextval('public.inbound_thread_readings_id_seq'::regclass);


--
-- Name: inbound_threads id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_threads ALTER COLUMN id SET DEFAULT nextval('public.inbound_threads_id_seq'::regclass);


--
-- Name: intake_project_identifiers id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.intake_project_identifiers ALTER COLUMN id SET DEFAULT nextval('public.intake_project_identifiers_id_seq'::regclass);


--
-- Name: key_date_draft_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_receipts ALTER COLUMN id SET DEFAULT nextval('public.key_date_draft_receipts_id_seq'::regclass);


--
-- Name: key_date_draft_row_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_row_receipts ALTER COLUMN id SET DEFAULT nextval('public.key_date_draft_row_receipts_id_seq'::regclass);


--
-- Name: legacy_ledger_archives id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.legacy_ledger_archives ALTER COLUMN id SET DEFAULT nextval('public.legacy_ledger_archives_id_seq'::regclass);


--
-- Name: milestone_registrations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestone_registrations ALTER COLUMN id SET DEFAULT nextval('public.milestone_registrations_id_seq'::regclass);


--
-- Name: milestones id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestones ALTER COLUMN id SET DEFAULT nextval('public.milestones_id_seq'::regclass);


--
-- Name: operative_support id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.operative_support ALTER COLUMN id SET DEFAULT nextval('public.operative_support_id_seq'::regclass);


--
-- Name: organization_identity_activations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_activations ALTER COLUMN id SET DEFAULT nextval('public.organization_identity_activations_id_seq'::regclass);


--
-- Name: organization_identity_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts ALTER COLUMN id SET DEFAULT nextval('public.organization_identity_receipts_id_seq'::regclass);


--
-- Name: page_processing_failures id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_processing_failures ALTER COLUMN id SET DEFAULT nextval('public.page_processing_failures_id_seq'::regclass);


--
-- Name: page_render_derivatives id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_render_derivatives ALTER COLUMN id SET DEFAULT nextval('public.page_render_derivatives_id_seq'::regclass);


--
-- Name: person_identities id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.person_identities ALTER COLUMN id SET DEFAULT nextval('public.person_identities_id_seq'::regclass);


--
-- Name: policy_approvals id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals ALTER COLUMN id SET DEFAULT nextval('public.policy_approvals_id_seq'::regclass);


--
-- Name: policy_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs ALTER COLUMN id SET DEFAULT nextval('public.policy_runs_id_seq'::regclass);


--
-- Name: processing_artifacts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.processing_artifacts ALTER COLUMN id SET DEFAULT nextval('public.processing_artifacts_id_seq'::regclass);


--
-- Name: production_run_explanation_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_configurations ALTER COLUMN id SET DEFAULT nextval('public.production_run_explanation_configurations_id_seq'::regclass);


--
-- Name: production_run_explanation_requests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests ALTER COLUMN id SET DEFAULT nextval('public.production_run_explanation_requests_id_seq'::regclass);


--
-- Name: project_check_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_check_configurations ALTER COLUMN id SET DEFAULT nextval('public.project_check_configurations_id_seq'::regclass);


--
-- Name: project_record_revisions id; Type: DEFAULT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.project_record_revisions ALTER COLUMN id SET DEFAULT nextval('public.project_record_revisions_id_seq'::regclass);


--
-- Name: project_roster_entries id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_roster_entries ALTER COLUMN id SET DEFAULT nextval('public.project_roster_entries_id_seq'::regclass);


--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- Name: report_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.report_runs ALTER COLUMN id SET DEFAULT nextval('public.report_runs_id_seq'::regclass);


--
-- Name: retention_holds id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_holds ALTER COLUMN id SET DEFAULT nextval('public.retention_holds_id_seq'::regclass);


--
-- Name: retention_manifest_items id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifest_items ALTER COLUMN id SET DEFAULT nextval('public.retention_manifest_items_id_seq'::regclass);


--
-- Name: retention_manifests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifests ALTER COLUMN id SET DEFAULT nextval('public.retention_manifests_id_seq'::regclass);


--
-- Name: retention_references id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_references ALTER COLUMN id SET DEFAULT nextval('public.retention_references_id_seq'::regclass);


--
-- Name: revision_change_explanation_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_configurations ALTER COLUMN id SET DEFAULT nextval('public.revision_change_explanation_configurations_id_seq'::regclass);


--
-- Name: revision_change_explanation_requests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests ALTER COLUMN id SET DEFAULT nextval('public.revision_change_explanation_requests_id_seq'::regclass);


--
-- Name: revision_comparison_findings id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_findings ALTER COLUMN id SET DEFAULT nextval('public.revision_comparison_findings_id_seq'::regclass);


--
-- Name: revision_comparison_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs ALTER COLUMN id SET DEFAULT nextval('public.revision_comparison_runs_id_seq'::regclass);


--
-- Name: schedule_governing_derivations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_governing_derivations ALTER COLUMN id SET DEFAULT nextval('public.schedule_governing_derivations_id_seq'::regclass);


--
-- Name: schedule_link_activations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_activations ALTER COLUMN id SET DEFAULT nextval('public.schedule_link_activations_id_seq'::regclass);


--
-- Name: schedule_link_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts ALTER COLUMN id SET DEFAULT nextval('public.schedule_link_receipts_id_seq'::regclass);


--
-- Name: scheduled_report_publications id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications ALTER COLUMN id SET DEFAULT nextval('public.scheduled_report_publications_id_seq'::regclass);


--
-- Name: sign_in_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.sign_in_attempts ALTER COLUMN id SET DEFAULT nextval('public.sign_in_attempts_id_seq'::regclass);


--
-- Name: sign_in_tokens id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.sign_in_tokens ALTER COLUMN id SET DEFAULT nextval('public.sign_in_tokens_id_seq'::regclass);


--
-- Name: source_fact_append_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts ALTER COLUMN id SET DEFAULT nextval('public.source_fact_append_receipts_id_seq'::regclass);


--
-- Name: source_fetch_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fetch_attempts ALTER COLUMN id SET DEFAULT nextval('public.source_fetch_attempts_id_seq'::regclass);


--
-- Name: source_intake_draft_configurations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_configurations ALTER COLUMN id SET DEFAULT nextval('public.source_intake_draft_configurations_id_seq'::regclass);


--
-- Name: source_intake_draft_requests id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests ALTER COLUMN id SET DEFAULT nextval('public.source_intake_draft_requests_id_seq'::regclass);


--
-- Name: source_segments id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments ALTER COLUMN id SET DEFAULT nextval('public.source_segments_id_seq'::regclass);


--
-- Name: stated_by_people id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.stated_by_people ALTER COLUMN id SET DEFAULT nextval('public.stated_by_people_id_seq'::regclass);


--
-- Name: statement_coordination_receipts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_receipts_id_seq'::regclass);


--
-- Name: statement_coordination_reversal_effects id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversal_effects ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_reversal_effects_id_seq'::regclass);


--
-- Name: statement_coordination_reversals id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_reversals_id_seq'::regclass);


--
-- Name: statement_suggestion_eligibility_declarations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_eligibility_declarations_id_seq'::regclass);


--
-- Name: statement_suggestion_protection_ends id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protection_ends ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_protection_ends_id_seq'::regclass);


--
-- Name: statement_suggestion_protections id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protections ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_protections_id_seq'::regclass);


--
-- Name: subject_candidate_suggestions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions ALTER COLUMN id SET DEFAULT nextval('public.subject_candidate_suggestions_id_seq'::regclass);


--
-- Name: subject_resolution_attempts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts ALTER COLUMN id SET DEFAULT nextval('public.subject_resolution_attempts_id_seq'::regclass);


--
-- Name: subject_resolution_candidates id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates ALTER COLUMN id SET DEFAULT nextval('public.subject_resolution_candidates_id_seq'::regclass);


--
-- Name: subject_resolution_decisions id; Type: DEFAULT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions ALTER COLUMN id SET DEFAULT nextval('public.subject_resolution_decisions_id_seq'::regclass);


--
-- Name: token_layers id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.token_layers ALTER COLUMN id SET DEFAULT nextval('public.token_layers_id_seq'::regclass);


--
-- Name: unreadable_cell_admission_activations id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_admission_activations ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_admission_activations_id_seq'::regclass);


--
-- Name: unreadable_cell_reading_profiles id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_profiles ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_profiles_id_seq'::regclass);


--
-- Name: unreadable_cell_reading_runs id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_runs_id_seq'::regclass);


--
-- Name: unreadable_cell_reading_steps id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_steps ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_steps_id_seq'::regclass);


--
-- Name: unreadable_cell_resolutions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_resolutions_id_seq'::regclass);


--
-- Name: web_sessions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.web_sessions ALTER COLUMN id SET DEFAULT nextval('public.web_sessions_id_seq'::regclass);


--
-- Name: work_decision_milestone_impacts id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decision_milestone_impacts ALTER COLUMN id SET DEFAULT nextval('public.work_decision_milestone_impacts_id_seq'::regclass);


--
-- Name: work_decisions id; Type: DEFAULT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions ALTER COLUMN id SET DEFAULT nextval('public.work_decisions_id_seq'::regclass);


--
-- Name: retired_automatic_carry_forward_policy_activations active_automatic_carry_forward_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_pkey PRIMARY KEY (project_id);


--
-- Name: retired_automatic_carry_forward_policy_activations active_automatic_carry_forward_policies_policy_approval_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_policy_approval_id_key UNIQUE (policy_approval_id);


--
-- Name: active_extraction_runs active_extraction_runs_extraction_run_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_extraction_run_id_key UNIQUE (extraction_run_id);


--
-- Name: active_extraction_runs active_extraction_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_pkey PRIMARY KEY (document_id);


--
-- Name: active_run_declarations active_run_declarations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_pkey PRIMARY KEY (id);


--
-- Name: assertions assertions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_pkey PRIMARY KEY (id);


--
-- Name: assignment_notification_attempts assignment_notification_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_pkey PRIMARY KEY (id);


--
-- Name: assignment_notification_attempts assignment_notification_attempts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_public_id_key UNIQUE (public_id);


--
-- Name: assignment_notification_dispatches assignment_notification_dispatches_notification_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_notification_id_key UNIQUE (notification_id);


--
-- Name: assignment_notification_dispatches assignment_notification_dispatches_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_pkey PRIMARY KEY (id);


--
-- Name: assignment_notification_dispatches assignment_notification_dispatches_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_public_id_key UNIQUE (public_id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_audit_log_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_audit_log_id_key UNIQUE (audit_log_id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_pkey PRIMARY KEY (id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_public_id_key UNIQUE (public_id);


--
-- Name: assignment_notifications assignment_notifications_occurrence_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_occurrence_key_key UNIQUE (occurrence_key);


--
-- Name: assignment_notifications assignment_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_pkey PRIMARY KEY (id);


--
-- Name: assignment_notifications assignment_notifications_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_public_id_key UNIQUE (public_id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_pkey PRIMARY KEY (id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_pkey PRIMARY KEY (audit_log_id);


--
-- Name: candidate_dispositions candidate_dispositions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidate_dispositions
    ADD CONSTRAINT candidate_dispositions_pkey PRIMARY KEY (id);


--
-- Name: candidates candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_pkey PRIMARY KEY (id);


--
-- Name: external_report_releases ck_external_report_releases_released_by_display; Type: CHECK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE public.external_report_releases
    ADD CONSTRAINT ck_external_report_releases_released_by_display CHECK (((released_by_display IS NOT NULL) AND (length(TRIM(BOTH FROM released_by_display)) > 0))) NOT VALID;


--
-- Name: cohort_receipts cohort_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_pkey PRIMARY KEY (id);


--
-- Name: commitment_lineages commitment_lineages_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.commitment_lineages
    ADD CONSTRAINT commitment_lineages_pkey PRIMARY KEY (id);


--
-- Name: condition_resolutions condition_resolutions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT condition_resolutions_pkey PRIMARY KEY (id);


--
-- Name: coordination_summary_configurations coordination_summary_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_configurations
    ADD CONSTRAINT coordination_summary_configurations_pkey PRIMARY KEY (id);


--
-- Name: coordination_summary_requests coordination_summary_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_pkey PRIMARY KEY (id);


--
-- Name: coordination_summary_requests coordination_summary_requests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_public_id_key UNIQUE (public_id);


--
-- Name: dependencies dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_pkey PRIMARY KEY (id);


--
-- Name: dependencies dependencies_project_id_ref_code_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_project_id_ref_code_key UNIQUE (project_id, ref_code);


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_pkey PRIMARY KEY (id);


--
-- Name: dependency_dismissals dependency_dismissals_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_dismissals
    ADD CONSTRAINT dependency_dismissals_pkey PRIMARY KEY (id);


--
-- Name: dependency_event_evidence dependency_event_evidence_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_pkey PRIMARY KEY (evidence_link_id);


--
-- Name: dependency_event_migration_receipts dependency_event_migration_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_migration_receipts
    ADD CONSTRAINT dependency_event_migration_receipts_pkey PRIMARY KEY (event_id);


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisions_pkey PRIMARY KEY (id);


--
-- Name: dependency_event_scopes dependency_event_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_pkey PRIMARY KEY (id);


--
-- Name: dependency_event_timings dependency_event_timings_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT dependency_event_timings_pkey PRIMARY KEY (id);


--
-- Name: dependency_events dependency_events_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT dependency_events_pkey PRIMARY KEY (id);


--
-- Name: dependency_evidence_sufficiencies dependency_evidence_sufficiencies_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_pkey PRIMARY KEY (id);


--
-- Name: discovered_references discovered_references_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT discovered_references_pkey PRIMARY KEY (id);


--
-- Name: dispute_history_resolutions dispute_history_resolutions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_pkey PRIMARY KEY (id);


--
-- Name: dispute_settlements dispute_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_settlements
    ADD CONSTRAINT dispute_settlements_pkey PRIMARY KEY (id);


--
-- Name: doc_pages doc_pages_document_id_page_no_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_document_id_page_no_key UNIQUE (document_id, page_no);


--
-- Name: doc_pages doc_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_pkey PRIMARY KEY (id);


--
-- Name: document_notification_attempts document_notification_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_pkey PRIMARY KEY (id);


--
-- Name: document_notification_attempts document_notification_attempts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_public_id_key UNIQUE (public_id);


--
-- Name: document_notification_dispatches document_notification_dispatches_notification_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_notification_id_key UNIQUE (notification_id);


--
-- Name: document_notification_dispatches document_notification_dispatches_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_pkey PRIMARY KEY (id);


--
-- Name: document_notification_dispatches document_notification_dispatches_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_public_id_key UNIQUE (public_id);


--
-- Name: document_notifications document_notifications_occurrence_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_occurrence_key_key UNIQUE (occurrence_key);


--
-- Name: document_notifications document_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_pkey PRIMARY KEY (id);


--
-- Name: document_notifications document_notifications_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_public_id_key UNIQUE (public_id);


--
-- Name: document_quarantines document_quarantines_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_quarantines
    ADD CONSTRAINT document_quarantines_pkey PRIMARY KEY (document_id);


--
-- Name: document_rendition_derivations document_rendition_derivations_derived_document_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_derived_document_id_key UNIQUE (derived_document_id);


--
-- Name: document_rendition_derivations document_rendition_derivations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_pkey PRIMARY KEY (id);


--
-- Name: documentation_field_confirmations documentation_field_confirmations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT documentation_field_confirmations_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: documents documents_project_id_sha256_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_project_id_sha256_key UNIQUE (project_id, sha256);


--
-- Name: due_action_notification_attempts due_action_notification_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_pkey PRIMARY KEY (id);


--
-- Name: due_action_notification_attempts due_action_notification_attempts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_public_id_key UNIQUE (public_id);


--
-- Name: due_action_notification_dispatches due_action_notification_dispatches_notification_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_notification_id_key UNIQUE (notification_id);


--
-- Name: due_action_notification_dispatches due_action_notification_dispatches_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_pkey PRIMARY KEY (id);


--
-- Name: due_action_notification_dispatches due_action_notification_dispatches_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_public_id_key UNIQUE (public_id);


--
-- Name: due_action_notifications due_action_notifications_occurrence_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_occurrence_key_key UNIQUE (occurrence_key);


--
-- Name: due_action_notifications due_action_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_pkey PRIMARY KEY (id);


--
-- Name: due_action_notifications due_action_notifications_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_public_id_key UNIQUE (public_id);


--
-- Name: due_work_occurrences due_work_occurrences_occurrence_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_occurrence_key_key UNIQUE (occurrence_key);


--
-- Name: due_work_occurrences due_work_occurrences_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_pkey PRIMARY KEY (id);


--
-- Name: due_work_occurrences due_work_occurrences_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_public_id_key UNIQUE (public_id);


--
-- Name: due_work_receipts due_work_receipts_attempt_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_attempt_id_key UNIQUE (attempt_id);


--
-- Name: due_work_receipts due_work_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_pkey PRIMARY KEY (id);


--
-- Name: due_work_receipts due_work_receipts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_public_id_key UNIQUE (public_id);


--
-- Name: due_work_schedules due_work_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_pkey PRIMARY KEY (id);


--
-- Name: due_work_schedules due_work_schedules_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_public_id_key UNIQUE (public_id);


--
-- Name: event_admission_acceptance_receipts event_admission_acceptance_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_acceptance_receipts
    ADD CONSTRAINT event_admission_acceptance_receipts_pkey PRIMARY KEY (id);


--
-- Name: event_admission_activations event_admission_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_pkey PRIMARY KEY (id);


--
-- Name: event_admission_outcomes event_admission_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_pkey PRIMARY KEY (id);


--
-- Name: event_cohort_receipts event_cohort_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT event_cohort_receipts_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_candidate_review_starts evidence_investigation_candidate_review_starts_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_candidate_id_key UNIQUE (candidate_id);


--
-- Name: evidence_investigation_candidate_review_starts evidence_investigation_candidate_review_starts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_contract_sha256_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_contract_sha256_key UNIQUE (contract_sha256);


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_public_id_key UNIQUE (public_id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_public_id_key UNIQUE (public_id);


--
-- Name: evidence_investigation_evaluation_receipts evidence_investigation_evaluation_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts
    ADD CONSTRAINT evidence_investigation_evaluation_receipts_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_evaluation_receipts evidence_investigation_evaluation_receipts_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts
    ADD CONSTRAINT evidence_investigation_evaluation_receipts_public_id_key UNIQUE (public_id);


--
-- Name: evidence_investigation_packet_receipts evidence_investigation_packet_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_packet_receipts evidence_investigation_packet_receipts_run_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_run_id_key UNIQUE (run_id);


--
-- Name: evidence_investigation_review_observations evidence_investigation_review_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT evidence_investigation_review_observations_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_runs evidence_investigation_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_runs evidence_investigation_runs_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_public_id_key UNIQUE (public_id);


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_public_id_key UNIQUE (public_id);


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_run_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_run_id_key UNIQUE (run_id);


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_shadow_case_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_shadow_case_id_key UNIQUE (shadow_case_id);


--
-- Name: evidence_investigation_shadow_outcomes evidence_investigation_shadow_outcomes_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_pkey PRIMARY KEY (id);


--
-- Name: evidence_investigation_shadow_outcomes evidence_investigation_shadow_outcomes_shadow_case_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_shadow_case_id_key UNIQUE (shadow_case_id);


--
-- Name: evidence_investigation_step_receipts evidence_investigation_step_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT evidence_investigation_step_receipts_pkey PRIMARY KEY (id);


--
-- Name: evidence_links evidence_links_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_pkey PRIMARY KEY (id);


--
-- Name: external_orgs external_orgs_name_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_orgs
    ADD CONSTRAINT external_orgs_name_key UNIQUE (name);


--
-- Name: external_orgs external_orgs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_orgs
    ADD CONSTRAINT external_orgs_pkey PRIMARY KEY (id);


--
-- Name: external_report_artifacts external_report_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_artifacts
    ADD CONSTRAINT external_report_artifacts_pkey PRIMARY KEY (id);


--
-- Name: external_report_releases external_report_releases_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT external_report_releases_pkey PRIMARY KEY (id);


--
-- Name: extracted_proposal_facts extracted_proposal_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT extracted_proposal_facts_pkey PRIMARY KEY (id);


--
-- Name: extracted_proposals extracted_proposals_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT extracted_proposals_candidate_id_key UNIQUE (candidate_id);


--
-- Name: extracted_proposals extracted_proposals_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT extracted_proposals_pkey PRIMARY KEY (id);


--
-- Name: extraction_failure_diagnosis_configurations extraction_failure_diagnosis_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations
    ADD CONSTRAINT extraction_failure_diagnosis_configurations_pkey PRIMARY KEY (id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_pkey PRIMARY KEY (id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_public_id_key UNIQUE (public_id);


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_pkey PRIMARY KEY (id);


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_predecessor_state_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_predecessor_state_id_key UNIQUE (predecessor_state_id);


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_public_id_key UNIQUE (public_id);


--
-- Name: extraction_run_candidates extraction_run_candidates_candidate_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates
    ADD CONSTRAINT extraction_run_candidates_candidate_id_key UNIQUE (candidate_id);


--
-- Name: extraction_run_candidates extraction_run_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates
    ADD CONSTRAINT extraction_run_candidates_pkey PRIMARY KEY (id);


--
-- Name: extraction_runs extraction_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_pkey PRIMARY KEY (id);


--
-- Name: fact_applies_to fact_applies_to_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT fact_applies_to_pkey PRIMARY KEY (id);


--
-- Name: fact_closure_results fact_closure_results_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results
    ADD CONSTRAINT fact_closure_results_pkey PRIMARY KEY (id);


--
-- Name: fact_closure_sources fact_closure_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT fact_closure_sources_pkey PRIMARY KEY (id);


--
-- Name: fact_decisions fact_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions
    ADD CONSTRAINT fact_decisions_pkey PRIMARY KEY (id);


--
-- Name: fact_dispositions fact_dispositions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_pkey PRIMARY KEY (id);


--
-- Name: fact_dispositions fact_dispositions_predecessor_fact_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_predecessor_fact_id_key UNIQUE (predecessor_fact_id);


--
-- Name: fact_dispositions fact_dispositions_successor_fact_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_successor_fact_id_key UNIQUE (successor_fact_id);


--
-- Name: fact_sources fact_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fact_sources_pkey PRIMARY KEY (id);


--
-- Name: fact_statement_timings fact_statement_timings_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_statement_timings
    ADD CONSTRAINT fact_statement_timings_pkey PRIMARY KEY (id);


--
-- Name: facts facts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_pkey PRIMARY KEY (id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_audit_log_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_audit_log_id_key UNIQUE (audit_log_id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_internal_owner_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_decision_id_key UNIQUE (internal_owner_decision_id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_next_action_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_next_action_decision_id_key UNIQUE (next_action_decision_id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_pkey PRIMARY KEY (id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_resumed_deferral_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_resumed_deferral_decision_id_key UNIQUE (resumed_deferral_decision_id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_audit_log_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_audit_log_id_key UNIQUE (audit_log_id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_deferral_reversal_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_deferral_reversal_decision_id_key UNIQUE (deferral_reversal_decision_id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_internal_owner_reversal_decision_i_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_internal_owner_reversal_decision_i_key UNIQUE (internal_owner_reversal_decision_id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_next_action_reversal_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_next_action_reversal_decision_id_key UNIQUE (next_action_reversal_decision_id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_pkey PRIMARY KEY (id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_receipt_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_receipt_id_key UNIQUE (receipt_id);


--
-- Name: inbound_messages inbound_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_pkey PRIMARY KEY (id);


--
-- Name: inbound_messages inbound_messages_raw_sha256_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_raw_sha256_key UNIQUE (raw_sha256);


--
-- Name: inbound_route_triage inbound_route_triage_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_pkey PRIMARY KEY (id);


--
-- Name: inbound_route_triage inbound_route_triage_thread_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_thread_id_key UNIQUE (thread_id);


--
-- Name: inbound_thread_readings inbound_thread_readings_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_pkey PRIMARY KEY (id);


--
-- Name: inbound_thread_readings inbound_thread_readings_thread_id_closing_message_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_thread_id_closing_message_id_key UNIQUE (thread_id, closing_message_id);


--
-- Name: inbound_threads inbound_threads_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_pkey PRIMARY KEY (id);


--
-- Name: intake_project_identifiers intake_project_identifiers_kind_value_normalized_project_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_kind_value_normalized_project_id_key UNIQUE (kind, value_normalized, project_id);


--
-- Name: intake_project_identifiers intake_project_identifiers_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_pkey PRIMARY KEY (id);


--
-- Name: key_date_draft_receipts key_date_draft_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_receipts
    ADD CONSTRAINT key_date_draft_receipts_pkey PRIMARY KEY (id);


--
-- Name: key_date_draft_row_receipts key_date_draft_row_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT key_date_draft_row_receipts_pkey PRIMARY KEY (id);


--
-- Name: legacy_ledger_archives legacy_ledger_archives_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_pkey PRIMARY KEY (id);


--
-- Name: legacy_ledger_archives legacy_ledger_archives_project_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_project_id_key UNIQUE (project_id);


--
-- Name: milestone_registrations milestone_registrations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_pkey PRIMARY KEY (id);


--
-- Name: milestones milestones_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_pkey PRIMARY KEY (id);


--
-- Name: milestones milestones_project_id_code_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_project_id_code_key UNIQUE (project_id, code);


--
-- Name: operative_support operative_support_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT operative_support_pkey PRIMARY KEY (id);


--
-- Name: organization_identity_activations organization_identity_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_activations
    ADD CONSTRAINT organization_identity_activations_pkey PRIMARY KEY (id);


--
-- Name: organization_identity_receipts organization_identity_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_pkey PRIMARY KEY (id);


--
-- Name: page_processing_failures page_processing_failures_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_processing_failures
    ADD CONSTRAINT page_processing_failures_pkey PRIMARY KEY (id);


--
-- Name: page_render_derivatives page_render_derivatives_derivative_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_render_derivatives
    ADD CONSTRAINT page_render_derivatives_derivative_key_key UNIQUE (derivative_key);


--
-- Name: page_render_derivatives page_render_derivatives_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_render_derivatives
    ADD CONSTRAINT page_render_derivatives_pkey PRIMARY KEY (id);


--
-- Name: person_identities person_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT person_identities_pkey PRIMARY KEY (id);


--
-- Name: policy_approvals policy_approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT policy_approvals_pkey PRIMARY KEY (id);


--
-- Name: policy_runs policy_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_pkey PRIMARY KEY (id);


--
-- Name: processing_artifacts processing_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.processing_artifacts
    ADD CONSTRAINT processing_artifacts_pkey PRIMARY KEY (id);


--
-- Name: production_run_explanation_configurations production_run_explanation_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_configurations
    ADD CONSTRAINT production_run_explanation_configurations_pkey PRIMARY KEY (id);


--
-- Name: production_run_explanation_requests production_run_explanation_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_pkey PRIMARY KEY (id);


--
-- Name: production_run_explanation_requests production_run_explanation_requests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_public_id_key UNIQUE (public_id);


--
-- Name: project_check_configurations project_check_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_check_configurations
    ADD CONSTRAINT project_check_configurations_pkey PRIMARY KEY (id);


--
-- Name: project_record_revisions project_record_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.project_record_revisions
    ADD CONSTRAINT project_record_revisions_pkey PRIMARY KEY (id);


--
-- Name: project_roster_entries project_roster_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT project_roster_entries_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: projects projects_slug_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_slug_key UNIQUE (slug);


--
-- Name: reconfirmation_receipts reconfirmation_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_pkey PRIMARY KEY (audit_log_id);


--
-- Name: record_inclusion_requests record_inclusion_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.record_inclusion_requests
    ADD CONSTRAINT record_inclusion_requests_pkey PRIMARY KEY (project_id);


--
-- Name: report_runs report_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_pkey PRIMARY KEY (id);


--
-- Name: retention_holds retention_holds_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_holds
    ADD CONSTRAINT retention_holds_pkey PRIMARY KEY (id);


--
-- Name: retention_manifest_items retention_manifest_items_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifest_items
    ADD CONSTRAINT retention_manifest_items_pkey PRIMARY KEY (id);


--
-- Name: retention_manifests retention_manifests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifests
    ADD CONSTRAINT retention_manifests_pkey PRIMARY KEY (id);


--
-- Name: retention_manifests retention_manifests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifests
    ADD CONSTRAINT retention_manifests_public_id_key UNIQUE (public_id);


--
-- Name: retention_references retention_references_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_references
    ADD CONSTRAINT retention_references_pkey PRIMARY KEY (id);


--
-- Name: retired_dependency_statuses retired_dependency_statuses_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_dependency_statuses
    ADD CONSTRAINT retired_dependency_statuses_pkey PRIMARY KEY (dependency_id);


--
-- Name: revision_change_explanation_configurations revision_change_explanation_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_configurations
    ADD CONSTRAINT revision_change_explanation_configurations_pkey PRIMARY KEY (id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_pkey PRIMARY KEY (id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_public_id_key UNIQUE (public_id);


--
-- Name: revision_comparison_findings revision_comparison_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_pkey PRIMARY KEY (id);


--
-- Name: revision_comparison_findings revision_comparison_findings_revision_comparison_run_id_ord_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_revision_comparison_run_id_ord_key UNIQUE (revision_comparison_run_id, ordinal);


--
-- Name: revision_comparison_runs revision_comparison_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT revision_comparison_runs_pkey PRIMARY KEY (id);


--
-- Name: revision_reconciliation_requests revision_reconciliation_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_reconciliation_requests
    ADD CONSTRAINT revision_reconciliation_requests_pkey PRIMARY KEY (project_id);


--
-- Name: schedule_governing_derivations schedule_governing_derivations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_governing_derivations
    ADD CONSTRAINT schedule_governing_derivations_pkey PRIMARY KEY (id);


--
-- Name: schedule_link_activations schedule_link_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_activations
    ADD CONSTRAINT schedule_link_activations_pkey PRIMARY KEY (id);


--
-- Name: schedule_link_receipts schedule_link_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_pkey PRIMARY KEY (id);


--
-- Name: scheduled_report_publications scheduled_report_publications_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_pkey PRIMARY KEY (id);


--
-- Name: scheduled_report_publications scheduled_report_publications_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_public_id_key UNIQUE (public_id);


--
-- Name: sign_in_attempts sign_in_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.sign_in_attempts
    ADD CONSTRAINT sign_in_attempts_pkey PRIMARY KEY (id);


--
-- Name: sign_in_tokens sign_in_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.sign_in_tokens
    ADD CONSTRAINT sign_in_tokens_pkey PRIMARY KEY (id);


--
-- Name: source_fact_append_receipts source_fact_append_receipts_extraction_run_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT source_fact_append_receipts_extraction_run_id_key UNIQUE (extraction_run_id);


--
-- Name: source_fact_append_receipts source_fact_append_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT source_fact_append_receipts_pkey PRIMARY KEY (id);


--
-- Name: source_fetch_attempts source_fetch_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fetch_attempts
    ADD CONSTRAINT source_fetch_attempts_pkey PRIMARY KEY (id);


--
-- Name: source_intake_draft_configurations source_intake_draft_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_configurations
    ADD CONSTRAINT source_intake_draft_configurations_pkey PRIMARY KEY (id);


--
-- Name: source_intake_draft_requests source_intake_draft_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_pkey PRIMARY KEY (id);


--
-- Name: source_intake_draft_requests source_intake_draft_requests_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_public_id_key UNIQUE (public_id);


--
-- Name: source_segments source_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT source_segments_pkey PRIMARY KEY (id);


--
-- Name: stated_by_people stated_by_people_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.stated_by_people
    ADD CONSTRAINT stated_by_people_pkey PRIMARY KEY (id);


--
-- Name: statement_coordination_receipts statement_coordination_receipt_milestone_impact_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipt_milestone_impact_decision_id_key UNIQUE (milestone_impact_decision_id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_audit_log_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_audit_log_id_key UNIQUE (audit_log_id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_dependency_event_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_dependency_event_id_key UNIQUE (dependency_event_id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_internal_owner_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_internal_owner_decision_id_key UNIQUE (internal_owner_decision_id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_next_action_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_next_action_decision_id_key UNIQUE (next_action_decision_id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_pkey PRIMARY KEY (id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_scope_decision_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_scope_decision_id_key UNIQUE (scope_decision_id);


--
-- Name: statement_coordination_reversal_effects statement_coordination_reversal_effects_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT statement_coordination_reversal_effects_pkey PRIMARY KEY (id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_audit_log_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_audit_log_id_key UNIQUE (audit_log_id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_candidate_disposition_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_disposition_id_key UNIQUE (candidate_disposition_id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_pkey PRIMARY KEY (id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_receipt_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_receipt_id_key UNIQUE (receipt_id);


--
-- Name: statement_suggestion_eligibility_declarations statement_suggestion_eligibility_declarations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_pkey PRIMARY KEY (id);


--
-- Name: statement_suggestion_protection_ends statement_suggestion_protection_ends_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT statement_suggestion_protection_ends_pkey PRIMARY KEY (id);


--
-- Name: statement_suggestion_protections statement_suggestion_protections_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_pkey PRIMARY KEY (id);


--
-- Name: subject_candidate_suggestions subject_candidate_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT subject_candidate_suggestions_pkey PRIMARY KEY (id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_pkey PRIMARY KEY (id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_pkey PRIMARY KEY (id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_pkey PRIMARY KEY (id);


--
-- Name: token_layers token_layers_layer_key_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.token_layers
    ADD CONSTRAINT token_layers_layer_key_key UNIQUE (layer_key);


--
-- Name: token_layers token_layers_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.token_layers
    ADD CONSTRAINT token_layers_pkey PRIMARY KEY (id);


--
-- Name: unreadable_cell_admission_activations unreadable_cell_admission_activations_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_admission_activations
    ADD CONSTRAINT unreadable_cell_admission_activations_pkey PRIMARY KEY (id);


--
-- Name: unreadable_cell_reading_profiles unreadable_cell_reading_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_profiles
    ADD CONSTRAINT unreadable_cell_reading_profiles_pkey PRIMARY KEY (id);


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_pkey PRIMARY KEY (id);


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_public_id_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_public_id_key UNIQUE (public_id);


--
-- Name: unreadable_cell_reading_steps unreadable_cell_reading_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT unreadable_cell_reading_steps_pkey PRIMARY KEY (id);


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_pkey PRIMARY KEY (id);


--
-- Name: active_run_declarations uq_active_run_declarations_document_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT uq_active_run_declarations_document_id_id UNIQUE (document_id, id);


--
-- Name: active_run_declarations uq_active_run_declarations_predecessor; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT uq_active_run_declarations_predecessor UNIQUE (predecessor_declaration_id);


--
-- Name: assignment_notification_attempts uq_assignment_attempt_number; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT uq_assignment_attempt_number UNIQUE (dispatch_id, attempt_number);


--
-- Name: assignment_notification_feedback uq_assignment_feedback_person; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT uq_assignment_feedback_person UNIQUE (notification_id, flagged_by);


--
-- Name: automatic_carry_forward_receipts uq_automatic_carry_forward_dependency_successor; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT uq_automatic_carry_forward_dependency_successor UNIQUE (dependency_id, successor_candidate_id);


--
-- Name: automatic_carry_forward_receipts uq_automatic_carry_forward_new_evidence; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT uq_automatic_carry_forward_new_evidence UNIQUE (new_evidence_link_id);


--
-- Name: automatic_carry_forward_outcomes uq_automatic_carry_forward_outcome_project_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT uq_automatic_carry_forward_outcome_project_id UNIQUE (project_id, id);


--
-- Name: automatic_carry_forward_outcomes uq_automatic_carry_forward_outcome_receipt_audit; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT uq_automatic_carry_forward_outcome_receipt_audit UNIQUE (receipt_audit_log_id);


--
-- Name: evidence_investigation_capture_results uq_capture_result_case; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT uq_capture_result_case UNIQUE (capture_contract_id, shadow_case_id);


--
-- Name: cohort_receipts uq_cohort_receipts_one_per_rule; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT uq_cohort_receipts_one_per_rule UNIQUE (revision_comparison_run_id, rule_version, external_org);


--
-- Name: dependencies uq_dependencies_project_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT uq_dependencies_project_id UNIQUE (project_id, id);


--
-- Name: dependency_event_scope_decisions uq_dependency_event_scope_decision_supersedes; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT uq_dependency_event_scope_decision_supersedes UNIQUE (supersedes_scope_decision_id);


--
-- Name: dependency_event_scopes uq_dependency_event_scopes_decision_dependency; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT uq_dependency_event_scopes_decision_dependency UNIQUE (scope_decision_id, dependency_id);


--
-- Name: dependency_event_timings uq_dependency_event_timing_kind; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT uq_dependency_event_timing_kind UNIQUE (event_id, kind);


--
-- Name: dependency_events uq_dependency_events_supersedes_event; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT uq_dependency_events_supersedes_event UNIQUE (supersedes_event_id);


--
-- Name: dependency_evidence_sufficiencies uq_dependency_evidence_sufficiency_scope_evidence; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT uq_dependency_evidence_sufficiency_scope_evidence UNIQUE (scope_link_id, evidence_link_id);


--
-- Name: discovered_references uq_discovered_reference_identity; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT uq_discovered_reference_identity UNIQUE (project_id, reference_key);


--
-- Name: dispute_history_resolutions uq_dispute_history_resolutions_coverage; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT uq_dispute_history_resolutions_coverage UNIQUE (dependency_id, field_name, covers_assertion_id);


--
-- Name: document_notification_attempts uq_document_attempt_number; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT uq_document_attempt_number UNIQUE (dispatch_id, attempt_number);


--
-- Name: documents uq_documents_project_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_project_id_id UNIQUE (project_id, id);


--
-- Name: documents uq_documents_project_registry_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_project_registry_id UNIQUE (project_id, registry_id);


--
-- Name: due_action_notification_attempts uq_due_action_attempt_number; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT uq_due_action_attempt_number UNIQUE (dispatch_id, attempt_number);


--
-- Name: due_work_receipts uq_due_work_receipt_attempt; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT uq_due_work_receipt_attempt UNIQUE (occurrence_id, attempt_number);


--
-- Name: due_work_schedules uq_due_work_schedule_identity; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT uq_due_work_schedule_identity UNIQUE (project_id, handler_key, configuration_version, input_identity_sha256);


--
-- Name: event_cohort_receipts uq_event_cohort_receipts_one_per_rule; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT uq_event_cohort_receipts_one_per_rule UNIQUE (project_id, rule_version);


--
-- Name: evidence_investigation_shadow_cases uq_evidence_investigation_shadow_case_identity; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT uq_evidence_investigation_shadow_case_identity UNIQUE (candidate_id, read_fingerprint, model, prompt_version, prompt_sha256, adapter_contract_version, tool_contract_version);


--
-- Name: evidence_investigation_shadow_outcomes uq_evidence_investigation_shadow_human_outcome; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT uq_evidence_investigation_shadow_human_outcome UNIQUE (human_outcome_identity);


--
-- Name: evidence_links uq_evidence_links_dependency_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT uq_evidence_links_dependency_id_id UNIQUE (dependency_id, id);


--
-- Name: external_report_releases uq_external_report_releases_artifact_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT uq_external_report_releases_artifact_id UNIQUE (artifact_id);


--
-- Name: extracted_proposal_facts uq_extracted_proposal_fact; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT uq_extracted_proposal_fact UNIQUE (proposal_id, fact_id);


--
-- Name: extracted_proposal_facts uq_extracted_proposal_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT uq_extracted_proposal_ordinal UNIQUE (proposal_id, ordinal);


--
-- Name: extracted_proposals uq_extracted_proposal_scope_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT uq_extracted_proposal_scope_id UNIQUE (project_id, document_id, extraction_run_id, id);


--
-- Name: extracted_proposals uq_extracted_proposal_subject; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT uq_extracted_proposal_subject UNIQUE (extraction_run_id, subject_key);


--
-- Name: extraction_measurement_case_states uq_extraction_measurement_case_states_ruling; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT uq_extraction_measurement_case_states_ruling UNIQUE (ruling_type, ruling_id);


--
-- Name: extraction_run_candidates uq_extraction_run_candidate; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates
    ADD CONSTRAINT uq_extraction_run_candidate UNIQUE (extraction_run_id, candidate_id);


--
-- Name: extraction_runs uq_extraction_runs_document_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT uq_extraction_runs_document_id_id UNIQUE (document_id, id);


--
-- Name: fact_applies_to uq_fact_applies_to_member; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT uq_fact_applies_to_member UNIQUE (fact_id, dependency_id);


--
-- Name: fact_applies_to uq_fact_applies_to_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT uq_fact_applies_to_ordinal UNIQUE (fact_id, ordinal);


--
-- Name: fact_closure_results uq_fact_closure_result_fact; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results
    ADD CONSTRAINT uq_fact_closure_result_fact UNIQUE (fact_id);


--
-- Name: fact_closure_sources uq_fact_closure_source_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT uq_fact_closure_source_ordinal UNIQUE (fact_id, ordinal);


--
-- Name: fact_closure_sources uq_fact_closure_source_segment; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT uq_fact_closure_source_segment UNIQUE (fact_id, source_segment_id);


--
-- Name: fact_sources uq_fact_sources_link; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT uq_fact_sources_link UNIQUE (fact_id, source_segment_id, role);


--
-- Name: fact_sources uq_fact_sources_role_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT uq_fact_sources_role_ordinal UNIQUE (fact_id, role, ordinal);


--
-- Name: fact_statement_timings uq_fact_statement_timing_role; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_statement_timings
    ADD CONSTRAINT uq_fact_statement_timing_role UNIQUE (fact_id, timing_role);


--
-- Name: facts uq_facts_project_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT uq_facts_project_id UNIQUE (project_id, id);


--
-- Name: facts uq_facts_run_scope_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT uq_facts_run_scope_id UNIQUE (project_id, document_id, extraction_run_id, id);


--
-- Name: facts uq_facts_scope_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT uq_facts_scope_id UNIQUE (project_id, document_id, id);


--
-- Name: extraction_failure_diagnosis_requests uq_failure_diagnosis_request_input; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT uq_failure_diagnosis_request_input UNIQUE (configuration_id, input_sha256);


--
-- Name: inbound_messages uq_inbound_message_message_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT uq_inbound_message_message_id UNIQUE (message_id);


--
-- Name: source_intake_draft_requests uq_intake_draft_request_source; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT uq_intake_draft_request_source UNIQUE (configuration_id, source_sha256);


--
-- Name: evidence_investigation_step_receipts uq_investigation_step_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT uq_investigation_step_ordinal UNIQUE (run_id, ordinal);


--
-- Name: key_date_draft_row_receipts uq_key_date_draft_row_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT uq_key_date_draft_row_ordinal UNIQUE (receipt_id, ordinal);


--
-- Name: milestone_registrations uq_milestone_registrations_predecessor; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT uq_milestone_registrations_predecessor UNIQUE (predecessor_registration_id);


--
-- Name: organization_identity_receipts uq_organization_identity_receipt_candidate_method; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT uq_organization_identity_receipt_candidate_method UNIQUE (candidate_id, method);


--
-- Name: person_identities uq_person_identity_email; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT uq_person_identity_email UNIQUE (email_normalized);


--
-- Name: person_identities uq_person_identity_principal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT uq_person_identity_principal UNIQUE (principal_subject);


--
-- Name: policy_approvals uq_policy_approvals_family_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_family_id UNIQUE (family, id);


--
-- Name: policy_approvals uq_policy_approvals_project_family_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_project_family_id UNIQUE (project_id, family, id);


--
-- Name: policy_approvals uq_policy_approvals_project_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_project_id UNIQUE (project_id, id);


--
-- Name: policy_runs uq_policy_runs_family_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT uq_policy_runs_family_id UNIQUE (family, id);


--
-- Name: policy_runs uq_policy_runs_project_family_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT uq_policy_runs_project_family_id UNIQUE (project_id, family, id);


--
-- Name: processing_artifacts uq_processing_artifact_path; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.processing_artifacts
    ADD CONSTRAINT uq_processing_artifact_path UNIQUE (storage_path);


--
-- Name: project_record_revisions uq_project_record_revision_key; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.project_record_revisions
    ADD CONSTRAINT uq_project_record_revision_key UNIQUE (project_id, idempotency_key);


--
-- Name: project_roster_entries uq_project_roster_principal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT uq_project_roster_principal UNIQUE (project_id, principal_subject);


--
-- Name: retention_manifest_items uq_retention_manifest_item; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifest_items
    ADD CONSTRAINT uq_retention_manifest_item UNIQUE (manifest_id, family, source_row_id);


--
-- Name: revision_change_explanation_requests uq_rev_change_expl_req_comparison; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT uq_rev_change_expl_req_comparison UNIQUE (configuration_id, comparison_sha256);


--
-- Name: production_run_explanation_requests uq_run_explanation_request_comparison; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT uq_run_explanation_request_comparison UNIQUE (configuration_id, comparison_sha256);


--
-- Name: schedule_link_receipts uq_schedule_link_receipts_audit; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT uq_schedule_link_receipts_audit UNIQUE (audit_log_id);


--
-- Name: scheduled_report_publications uq_scheduled_report_publication_occurrence; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT uq_scheduled_report_publication_occurrence UNIQUE (occurrence_id);


--
-- Name: evidence_investigation_review_observations uq_shadow_review_boundary; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT uq_shadow_review_boundary UNIQUE (shadow_case_id, boundary);


--
-- Name: sign_in_tokens uq_sign_in_token_hash; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.sign_in_tokens
    ADD CONSTRAINT uq_sign_in_token_hash UNIQUE (token_sha256);


--
-- Name: source_fact_append_receipts uq_source_fact_append_content; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT uq_source_fact_append_content UNIQUE (project_id, content_sha256);


--
-- Name: source_fact_append_receipts uq_source_fact_append_key; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT uq_source_fact_append_key UNIQUE (project_id, idempotency_key);


--
-- Name: source_segments uq_source_segments_document_kind_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT uq_source_segments_document_kind_ordinal UNIQUE (document_id, kind, ordinal);


--
-- Name: source_segments uq_source_segments_project_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT uq_source_segments_project_id UNIQUE (project_id, id);


--
-- Name: source_segments uq_source_segments_prose_locator; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT uq_source_segments_prose_locator UNIQUE (document_id, kind, page_no, start_offset, end_offset);


--
-- Name: source_segments uq_source_segments_scope_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT uq_source_segments_scope_id UNIQUE (project_id, document_id, id);


--
-- Name: source_segments uq_source_segments_spreadsheet_locator; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT uq_source_segments_spreadsheet_locator UNIQUE (document_id, kind, sheet_name, cell_range);


--
-- Name: statement_coordination_receipts uq_statement_coordination_receipt_disposition; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT uq_statement_coordination_receipt_disposition UNIQUE (candidate_disposition_id);


--
-- Name: statement_coordination_reversal_effects uq_statement_coordination_reversal_effect; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT uq_statement_coordination_reversal_effect UNIQUE (reversal_id, effect_kind, target_id);


--
-- Name: statement_suggestion_eligibility_declarations uq_statement_suggestion_eligibility_candidate; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT uq_statement_suggestion_eligibility_candidate UNIQUE (candidate_id);


--
-- Name: statement_suggestion_protection_ends uq_statement_suggestion_protection_end; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT uq_statement_suggestion_protection_end UNIQUE (protection_id);


--
-- Name: statement_suggestion_protections uq_statement_suggestion_protection_window; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT uq_statement_suggestion_protection_window UNIQUE (candidate_id, kind, observation_contract);


--
-- Name: subject_candidate_suggestions uq_subject_candidate_suggestion_candidate; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT uq_subject_candidate_suggestion_candidate UNIQUE (attempt_id, candidate_id);


--
-- Name: subject_candidate_suggestions uq_subject_candidate_suggestion_rank; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT uq_subject_candidate_suggestion_rank UNIQUE (attempt_id, rank);


--
-- Name: subject_resolution_candidates uq_subject_resolution_candidate; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT uq_subject_resolution_candidate UNIQUE (attempt_id, subject_key);


--
-- Name: subject_resolution_candidates uq_subject_resolution_candidate_scope; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT uq_subject_resolution_candidate_scope UNIQUE (attempt_id, id);


--
-- Name: subject_resolution_attempts uq_subject_resolution_content; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT uq_subject_resolution_content UNIQUE (content_sha256);


--
-- Name: subject_resolution_decisions uq_subject_resolution_decision_attempt; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT uq_subject_resolution_decision_attempt UNIQUE (attempt_id);


--
-- Name: subject_resolution_decisions uq_subject_resolution_decision_revision; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT uq_subject_resolution_decision_revision UNIQUE (revision_id);


--
-- Name: subject_resolution_decisions uq_subject_resolution_registered_alias; Type: CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT uq_subject_resolution_registered_alias UNIQUE (project_id, reference_kind, normalized_reference);


--
-- Name: coordination_summary_requests uq_summary_request_reading; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT uq_summary_request_reading UNIQUE (configuration_id, reading_sha256);


--
-- Name: unreadable_cell_reading_steps uq_unreadable_cell_step_ordinal; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT uq_unreadable_cell_step_ordinal UNIQUE (run_id, ordinal);


--
-- Name: web_sessions uq_web_session_hash; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.web_sessions
    ADD CONSTRAINT uq_web_session_hash UNIQUE (session_sha256);


--
-- Name: work_decision_milestone_impacts uq_work_decision_milestone_impact; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT uq_work_decision_milestone_impact UNIQUE (work_decision_id, milestone_id);


--
-- Name: work_decisions uq_work_decisions_commitment_lineage_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_commitment_lineage_id_id UNIQUE (commitment_lineage_id, id);


--
-- Name: work_decisions uq_work_decisions_dependency_id_id; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_dependency_id_id UNIQUE (dependency_id, id);


--
-- Name: work_decisions uq_work_decisions_predecessor; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_predecessor UNIQUE (predecessor_decision_id);


--
-- Name: web_sessions web_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.web_sessions
    ADD CONSTRAINT web_sessions_pkey PRIMARY KEY (id);


--
-- Name: work_decision_milestone_impacts work_decision_milestone_impacts_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_pkey PRIMARY KEY (id);


--
-- Name: work_decisions work_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT work_decisions_pkey PRIMARY KEY (id);


--
-- Name: ix_assignment_attempts_dispatch; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_attempts_dispatch ON public.assignment_notification_attempts USING btree (dispatch_id);


--
-- Name: ix_assignment_attempts_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_attempts_project ON public.assignment_notification_attempts USING btree (project_id);


--
-- Name: ix_assignment_dispatches_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_dispatches_project ON public.assignment_notification_dispatches USING btree (project_id);


--
-- Name: ix_assignment_feedback_notification; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_feedback_notification ON public.assignment_notification_feedback USING btree (notification_id);


--
-- Name: ix_assignment_feedback_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_feedback_project ON public.assignment_notification_feedback USING btree (project_id);


--
-- Name: ix_assignment_notifications_decision; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_notifications_decision ON public.assignment_notifications USING btree (assignment_decision_id);


--
-- Name: ix_assignment_notifications_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_notifications_project ON public.assignment_notifications USING btree (project_id);


--
-- Name: ix_assignment_notifications_recipient; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_assignment_notifications_recipient ON public.assignment_notifications USING btree (project_id, recipient_principal_subject);


--
-- Name: ix_automatic_carry_forward_outcomes_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_automatic_carry_forward_outcomes_project_id ON public.automatic_carry_forward_outcomes USING btree (project_id);


--
-- Name: ix_automatic_carry_forward_receipts_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_automatic_carry_forward_receipts_dependency_id ON public.automatic_carry_forward_receipts USING btree (dependency_id);


--
-- Name: ix_capture_contracts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_capture_contracts_project_id ON public.evidence_investigation_capture_contracts USING btree (project_id);


--
-- Name: ix_capture_results_contract_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_capture_results_contract_id ON public.evidence_investigation_capture_results USING btree (capture_contract_id);


--
-- Name: ix_capture_results_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_capture_results_project_id ON public.evidence_investigation_capture_results USING btree (project_id);


--
-- Name: ix_condition_resolutions_dependency; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_condition_resolutions_dependency ON public.condition_resolutions USING btree (dependency_id, evidence_link_id);


--
-- Name: ix_coordination_summary_configurations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_coordination_summary_configurations_project_id ON public.coordination_summary_configurations USING btree (project_id);


--
-- Name: ix_coordination_summary_requests_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_coordination_summary_requests_project_id ON public.coordination_summary_requests USING btree (project_id);


--
-- Name: ix_dependency_admission_outcomes_dependency_admission_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_dependency_admission_outcomes_dependency_admission_run_id ON public.dependency_admission_outcomes USING btree (policy_run_id);


--
-- Name: ix_dependency_dismissals_dependency; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_dependency_dismissals_dependency ON public.dependency_dismissals USING btree (dependency_id);


--
-- Name: ix_dependency_events_closes_commitment_lineage; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_dependency_events_closes_commitment_lineage ON public.dependency_events USING btree (closes_commitment_lineage_id);


--
-- Name: ix_discovered_references_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_discovered_references_project_id ON public.discovered_references USING btree (project_id);


--
-- Name: ix_discovered_references_project_state; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_discovered_references_project_state ON public.discovered_references USING btree (project_id, state);


--
-- Name: ix_dispute_history_resolutions_dependency_field; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_dispute_history_resolutions_dependency_field ON public.dispute_history_resolutions USING btree (dependency_id, field_name);


--
-- Name: ix_dispute_settlements_dependency_field; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_dispute_settlements_dependency_field ON public.dispute_settlements USING btree (dependency_id, field_name);


--
-- Name: ix_document_attempts_dispatch; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_attempts_dispatch ON public.document_notification_attempts USING btree (dispatch_id);


--
-- Name: ix_document_attempts_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_attempts_project ON public.document_notification_attempts USING btree (project_id);


--
-- Name: ix_document_dispatches_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_dispatches_project ON public.document_notification_dispatches USING btree (project_id);


--
-- Name: ix_document_notifications_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_notifications_project ON public.document_notifications USING btree (project_id);


--
-- Name: ix_document_notifications_recipient; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_notifications_recipient ON public.document_notifications USING btree (project_id, recipient_principal_subject);


--
-- Name: ix_document_rendition_derivations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_document_rendition_derivations_project_id ON public.document_rendition_derivations USING btree (project_id);


--
-- Name: ix_documentation_confirmation_dependency_field; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_documentation_confirmation_dependency_field ON public.documentation_field_confirmations USING btree (dependency_id, field_name, id);


--
-- Name: ix_due_action_attempts_dispatch; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_action_attempts_dispatch ON public.due_action_notification_attempts USING btree (dispatch_id);


--
-- Name: ix_due_action_attempts_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_action_attempts_project ON public.due_action_notification_attempts USING btree (project_id);


--
-- Name: ix_due_action_dispatches_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_action_dispatches_project ON public.due_action_notification_dispatches USING btree (project_id);


--
-- Name: ix_due_action_notifications_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_action_notifications_project ON public.due_action_notifications USING btree (project_id);


--
-- Name: ix_due_action_notifications_recipient; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_action_notifications_recipient ON public.due_action_notifications USING btree (project_id, recipient_principal_subject);


--
-- Name: ix_due_work_occurrences_due; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_work_occurrences_due ON public.due_work_occurrences USING btree (due_at);


--
-- Name: ix_due_work_occurrences_job; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_work_occurrences_job ON public.due_work_occurrences USING btree (scheduled_job_id);


--
-- Name: ix_due_work_receipts_occurrence; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_work_receipts_occurrence ON public.due_work_receipts USING btree (occurrence_id);


--
-- Name: ix_due_work_receipts_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_work_receipts_project ON public.due_work_receipts USING btree (project_id);


--
-- Name: ix_due_work_schedules_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_due_work_schedules_project_id ON public.due_work_schedules USING btree (project_id);


--
-- Name: ix_event_admission_acceptance_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_event_admission_acceptance_project ON public.event_admission_acceptance_receipts USING btree (project_id, id);


--
-- Name: ix_event_admission_activation_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_event_admission_activation_project ON public.event_admission_activations USING btree (project_id, id);


--
-- Name: ix_event_admission_outcomes_event_admission_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_event_admission_outcomes_event_admission_run_id ON public.event_admission_outcomes USING btree (policy_run_id);


--
-- Name: ix_evidence_investigation_candidate_review_starts_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX ix_evidence_investigation_candidate_review_starts_candidate_id ON public.evidence_investigation_candidate_review_starts USING btree (candidate_id);


--
-- Name: ix_evidence_investigation_candidate_review_starts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_candidate_review_starts_project_id ON public.evidence_investigation_candidate_review_starts USING btree (project_id);


--
-- Name: ix_evidence_investigation_review_observations_shadow_case_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_review_observations_shadow_case_id ON public.evidence_investigation_review_observations USING btree (shadow_case_id);


--
-- Name: ix_evidence_investigation_runs_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_runs_candidate_id ON public.evidence_investigation_runs USING btree (candidate_id);


--
-- Name: ix_evidence_investigation_runs_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_runs_project_id ON public.evidence_investigation_runs USING btree (project_id);


--
-- Name: ix_evidence_investigation_shadow_cases_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_shadow_cases_candidate_id ON public.evidence_investigation_shadow_cases USING btree (candidate_id);


--
-- Name: ix_evidence_investigation_shadow_cases_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_shadow_cases_project_id ON public.evidence_investigation_shadow_cases USING btree (project_id);


--
-- Name: ix_evidence_investigation_step_receipts_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_evidence_investigation_step_receipts_run_id ON public.evidence_investigation_step_receipts USING btree (run_id);


--
-- Name: ix_external_report_artifacts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_external_report_artifacts_project_id ON public.external_report_artifacts USING btree (project_id);


--
-- Name: ix_external_report_releases_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_external_report_releases_project_id ON public.external_report_releases USING btree (project_id);


--
-- Name: ix_extracted_proposal_facts_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extracted_proposal_facts_fact_id ON public.extracted_proposal_facts USING btree (fact_id);


--
-- Name: ix_extracted_proposal_facts_proposal_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extracted_proposal_facts_proposal_id ON public.extracted_proposal_facts USING btree (proposal_id);


--
-- Name: ix_extracted_proposals_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extracted_proposals_document_id ON public.extracted_proposals USING btree (document_id);


--
-- Name: ix_extracted_proposals_extraction_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extracted_proposals_extraction_run_id ON public.extracted_proposals USING btree (extraction_run_id);


--
-- Name: ix_extracted_proposals_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extracted_proposals_project_id ON public.extracted_proposals USING btree (project_id);


--
-- Name: ix_extraction_measurement_case_states_case_key; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extraction_measurement_case_states_case_key ON public.extraction_measurement_case_states USING btree (case_key);


--
-- Name: ix_extraction_measurement_case_states_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extraction_measurement_case_states_project_id ON public.extraction_measurement_case_states USING btree (project_id);


--
-- Name: ix_extraction_run_candidates_extraction_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extraction_run_candidates_extraction_run_id ON public.extraction_run_candidates USING btree (extraction_run_id);


--
-- Name: ix_extraction_runs_completed_prompt_document; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_extraction_runs_completed_prompt_document ON public.extraction_runs USING btree (prompt_version, document_id) WHERE (((outcome)::text = 'completed'::text) AND (page_errors = 0));


--
-- Name: ix_fact_applies_to_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_applies_to_dependency_id ON public.fact_applies_to USING btree (dependency_id);


--
-- Name: ix_fact_applies_to_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_applies_to_fact_id ON public.fact_applies_to USING btree (fact_id);


--
-- Name: ix_fact_applies_to_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_applies_to_project_id ON public.fact_applies_to USING btree (project_id);


--
-- Name: ix_fact_closure_results_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_results_fact_id ON public.fact_closure_results USING btree (fact_id);


--
-- Name: ix_fact_closure_results_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_results_project_id ON public.fact_closure_results USING btree (project_id);


--
-- Name: ix_fact_closure_sources_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_sources_document_id ON public.fact_closure_sources USING btree (document_id);


--
-- Name: ix_fact_closure_sources_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_sources_fact_id ON public.fact_closure_sources USING btree (fact_id);


--
-- Name: ix_fact_closure_sources_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_sources_project_id ON public.fact_closure_sources USING btree (project_id);


--
-- Name: ix_fact_closure_sources_source_segment_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_closure_sources_source_segment_id ON public.fact_closure_sources USING btree (source_segment_id);


--
-- Name: ix_fact_decisions_project_id; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE INDEX ix_fact_decisions_project_id ON public.fact_decisions USING btree (project_id);


--
-- Name: ix_fact_dispositions_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_dispositions_project_id ON public.fact_dispositions USING btree (project_id);


--
-- Name: ix_fact_sources_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_sources_document_id ON public.fact_sources USING btree (document_id);


--
-- Name: ix_fact_sources_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_sources_fact_id ON public.fact_sources USING btree (fact_id);


--
-- Name: ix_fact_sources_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_sources_project_id ON public.fact_sources USING btree (project_id);


--
-- Name: ix_fact_sources_source_segment_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_sources_source_segment_id ON public.fact_sources USING btree (source_segment_id);


--
-- Name: ix_fact_statement_timings_fact_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_statement_timings_fact_id ON public.fact_statement_timings USING btree (fact_id);


--
-- Name: ix_fact_statement_timings_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_fact_statement_timings_project_id ON public.fact_statement_timings USING btree (project_id);


--
-- Name: ix_facts_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_facts_document_id ON public.facts USING btree (document_id);


--
-- Name: ix_facts_extraction_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_facts_extraction_run_id ON public.facts USING btree (extraction_run_id);


--
-- Name: ix_facts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_facts_project_id ON public.facts USING btree (project_id);


--
-- Name: ix_failure_diagnosis_configurations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_failure_diagnosis_configurations_project_id ON public.extraction_failure_diagnosis_configurations USING btree (project_id);


--
-- Name: ix_failure_diagnosis_requests_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_failure_diagnosis_requests_document_id ON public.extraction_failure_diagnosis_requests USING btree (document_id);


--
-- Name: ix_failure_diagnosis_requests_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_failure_diagnosis_requests_project_id ON public.extraction_failure_diagnosis_requests USING btree (project_id);


--
-- Name: ix_follow_up_plan_receipts_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_follow_up_plan_receipts_dependency_id ON public.follow_up_plan_receipts USING btree (dependency_id);


--
-- Name: ix_inbound_messages_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_inbound_messages_project_id ON public.inbound_messages USING btree (project_id);


--
-- Name: ix_inbound_messages_thread_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_inbound_messages_thread_id ON public.inbound_messages USING btree (thread_id);


--
-- Name: ix_inbound_thread_readings_thread_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_inbound_thread_readings_thread_id ON public.inbound_thread_readings USING btree (thread_id);


--
-- Name: ix_intake_draft_configurations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_intake_draft_configurations_project_id ON public.source_intake_draft_configurations USING btree (project_id);


--
-- Name: ix_intake_draft_requests_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_intake_draft_requests_project_id ON public.source_intake_draft_requests USING btree (project_id);


--
-- Name: ix_intake_draft_requests_staged_sha256; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_intake_draft_requests_staged_sha256 ON public.source_intake_draft_requests USING btree (staged_sha256);


--
-- Name: ix_intake_project_identifiers_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_intake_project_identifiers_project_id ON public.intake_project_identifiers USING btree (project_id);


--
-- Name: ix_key_date_draft_receipts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_key_date_draft_receipts_project_id ON public.key_date_draft_receipts USING btree (project_id);


--
-- Name: ix_key_date_draft_receipts_source_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_key_date_draft_receipts_source_document_id ON public.key_date_draft_receipts USING btree (source_document_id);


--
-- Name: ix_key_date_draft_row_receipts_receipt_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_key_date_draft_row_receipts_receipt_id ON public.key_date_draft_row_receipts USING btree (receipt_id);


--
-- Name: ix_milestone_registrations_milestone_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_milestone_registrations_milestone_id ON public.milestone_registrations USING btree (milestone_id);


--
-- Name: ix_organization_identity_activations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_organization_identity_activations_project_id ON public.organization_identity_activations USING btree (project_id);


--
-- Name: ix_organization_identity_receipts_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_organization_identity_receipts_candidate_id ON public.organization_identity_receipts USING btree (candidate_id);


--
-- Name: ix_organization_identity_receipts_external_org_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_organization_identity_receipts_external_org_id ON public.organization_identity_receipts USING btree (external_org_id);


--
-- Name: ix_organization_identity_receipts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_organization_identity_receipts_project_id ON public.organization_identity_receipts USING btree (project_id);


--
-- Name: ix_page_processing_failures_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_page_processing_failures_document_id ON public.page_processing_failures USING btree (document_id);


--
-- Name: ix_page_render_derivatives_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_page_render_derivatives_document_id ON public.page_render_derivatives USING btree (document_id);


--
-- Name: ix_policy_runs_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_policy_runs_project_id ON public.policy_runs USING btree (project_id);


--
-- Name: ix_processing_artifacts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_processing_artifacts_project_id ON public.processing_artifacts USING btree (project_id);


--
-- Name: ix_project_check_configurations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_project_check_configurations_project_id ON public.project_check_configurations USING btree (project_id);


--
-- Name: ix_project_record_revisions_project_id; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE INDEX ix_project_record_revisions_project_id ON public.project_record_revisions USING btree (project_id);


--
-- Name: ix_reconfirmation_receipts_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_reconfirmation_receipts_dependency_id ON public.reconfirmation_receipts USING btree (dependency_id);


--
-- Name: ix_retention_references_source; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_retention_references_source ON public.retention_references USING btree (family, source_row_id) WHERE (closed_at IS NULL);


--
-- Name: ix_rev_change_expl_cfg_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_rev_change_expl_cfg_project_id ON public.revision_change_explanation_configurations USING btree (project_id);


--
-- Name: ix_rev_change_expl_req_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_rev_change_expl_req_dependency_id ON public.revision_change_explanation_requests USING btree (dependency_id);


--
-- Name: ix_rev_change_expl_req_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_rev_change_expl_req_project_id ON public.revision_change_explanation_requests USING btree (project_id);


--
-- Name: ix_run_explanation_configurations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_run_explanation_configurations_project_id ON public.production_run_explanation_configurations USING btree (project_id);


--
-- Name: ix_run_explanation_requests_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_run_explanation_requests_document_id ON public.production_run_explanation_requests USING btree (document_id);


--
-- Name: ix_run_explanation_requests_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_run_explanation_requests_project_id ON public.production_run_explanation_requests USING btree (project_id);


--
-- Name: ix_schedule_governing_derivations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_schedule_governing_derivations_project_id ON public.schedule_governing_derivations USING btree (project_id);


--
-- Name: ix_schedule_link_activations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_schedule_link_activations_project_id ON public.schedule_link_activations USING btree (project_id);


--
-- Name: ix_schedule_link_receipts_dependency_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_schedule_link_receipts_dependency_id ON public.schedule_link_receipts USING btree (dependency_id);


--
-- Name: ix_schedule_link_receipts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_schedule_link_receipts_project_id ON public.schedule_link_receipts USING btree (project_id);


--
-- Name: ix_scheduled_report_publications_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_scheduled_report_publications_project_id ON public.scheduled_report_publications USING btree (project_id);


--
-- Name: ix_sign_in_attempt_scope; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_sign_in_attempt_scope ON public.sign_in_attempts USING btree (scope_kind, scope_value, occurred_at);


--
-- Name: ix_source_fact_append_receipts_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_fact_append_receipts_document_id ON public.source_fact_append_receipts USING btree (document_id);


--
-- Name: ix_source_fact_append_receipts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_fact_append_receipts_project_id ON public.source_fact_append_receipts USING btree (project_id);


--
-- Name: ix_source_fetch_attempts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_fetch_attempts_project_id ON public.source_fetch_attempts USING btree (project_id);


--
-- Name: ix_source_fetch_attempts_project_outcome; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_fetch_attempts_project_outcome ON public.source_fetch_attempts USING btree (project_id, outcome);


--
-- Name: ix_source_fetch_attempts_reference; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_fetch_attempts_reference ON public.source_fetch_attempts USING btree (project_id, reference_key);


--
-- Name: ix_source_segments_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_segments_document_id ON public.source_segments USING btree (document_id);


--
-- Name: ix_source_segments_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_source_segments_project_id ON public.source_segments USING btree (project_id);


--
-- Name: ix_stated_by_people_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_stated_by_people_project_id ON public.stated_by_people USING btree (project_id);


--
-- Name: ix_statement_suggestion_eligibility_declarations_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_statement_suggestion_eligibility_declarations_candidate_id ON public.statement_suggestion_eligibility_declarations USING btree (candidate_id);


--
-- Name: ix_statement_suggestion_eligibility_declarations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_statement_suggestion_eligibility_declarations_project_id ON public.statement_suggestion_eligibility_declarations USING btree (project_id);


--
-- Name: ix_statement_suggestion_protection_ends_protection_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_statement_suggestion_protection_ends_protection_id ON public.statement_suggestion_protection_ends USING btree (protection_id);


--
-- Name: ix_statement_suggestion_protections_candidate_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_statement_suggestion_protections_candidate_id ON public.statement_suggestion_protections USING btree (candidate_id);


--
-- Name: ix_statement_suggestion_protections_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_statement_suggestion_protections_project_id ON public.statement_suggestion_protections USING btree (project_id);


--
-- Name: ix_subject_candidate_suggestions_attempt_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_subject_candidate_suggestions_attempt_id ON public.subject_candidate_suggestions USING btree (attempt_id);


--
-- Name: ix_subject_resolution_attempts_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_subject_resolution_attempts_project_id ON public.subject_resolution_attempts USING btree (project_id);


--
-- Name: ix_subject_resolution_attempts_source_segment_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_subject_resolution_attempts_source_segment_id ON public.subject_resolution_attempts USING btree (source_segment_id);


--
-- Name: ix_subject_resolution_candidates_attempt_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_subject_resolution_candidates_attempt_id ON public.subject_resolution_candidates USING btree (attempt_id);


--
-- Name: ix_subject_resolution_decisions_attempt_id; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE INDEX ix_subject_resolution_decisions_attempt_id ON public.subject_resolution_decisions USING btree (attempt_id);


--
-- Name: ix_subject_resolution_decisions_project_id; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE INDEX ix_subject_resolution_decisions_project_id ON public.subject_resolution_decisions USING btree (project_id);


--
-- Name: ix_subject_resolution_decisions_revision_id; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE INDEX ix_subject_resolution_decisions_revision_id ON public.subject_resolution_decisions USING btree (revision_id);


--
-- Name: ix_token_layers_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_token_layers_document_id ON public.token_layers USING btree (document_id);


--
-- Name: ix_unreadable_cell_admission_activations_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_admission_activations_project_id ON public.unreadable_cell_admission_activations USING btree (project_id);


--
-- Name: ix_unreadable_cell_reading_profiles_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_reading_profiles_project_id ON public.unreadable_cell_reading_profiles USING btree (project_id);


--
-- Name: ix_unreadable_cell_reading_runs_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_reading_runs_document_id ON public.unreadable_cell_reading_runs USING btree (document_id);


--
-- Name: ix_unreadable_cell_reading_runs_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_reading_runs_project_id ON public.unreadable_cell_reading_runs USING btree (project_id);


--
-- Name: ix_unreadable_cell_reading_steps_run_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_reading_steps_run_id ON public.unreadable_cell_reading_steps USING btree (run_id);


--
-- Name: ix_unreadable_cell_resolutions_cell; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_resolutions_cell ON public.unreadable_cell_resolutions USING btree (project_id, document_id, page_no, cell_key);


--
-- Name: ix_unreadable_cell_resolutions_document_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_resolutions_document_id ON public.unreadable_cell_resolutions USING btree (document_id);


--
-- Name: ix_unreadable_cell_resolutions_project_id; Type: INDEX; Schema: public; Owner: corridor
--

CREATE INDEX ix_unreadable_cell_resolutions_project_id ON public.unreadable_cell_resolutions USING btree (project_id);


--
-- Name: uq_active_run_declarations_one_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_active_run_declarations_one_root ON public.active_run_declarations USING btree (document_id) WHERE (predecessor_declaration_id IS NULL);


--
-- Name: uq_automatic_carry_forward_outcome_abstained_identity; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_automatic_carry_forward_outcome_abstained_identity ON public.automatic_carry_forward_outcomes USING btree (project_id, COALESCE(policy_approval_id, (0)::bigint), dependency_id, COALESCE(comparison_id, ('-1'::integer)::bigint), COALESCE(finding_id, ('-1'::integer)::bigint), COALESCE(predecessor_candidate_id, ('-1'::integer)::bigint), COALESCE(successor_candidate_id, ('-1'::integer)::bigint), reason, reason_version) WHERE ((outcome)::text = 'abstained'::text);


--
-- Name: uq_dependency_event_scope_decision_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_dependency_event_scope_decision_root ON public.dependency_event_scope_decisions USING btree (event_id) WHERE (supersedes_scope_decision_id IS NULL);


--
-- Name: uq_dependency_evidence_sufficiency_direct_evidence; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_dependency_evidence_sufficiency_direct_evidence ON public.dependency_evidence_sufficiencies USING btree (evidence_link_id) WHERE (scope_link_id IS NULL);


--
-- Name: uq_extraction_measurement_case_states_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_extraction_measurement_case_states_root ON public.extraction_measurement_case_states USING btree (case_key) WHERE (predecessor_state_id IS NULL);


--
-- Name: uq_fact_decision_effective; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE UNIQUE INDEX uq_fact_decision_effective ON public.fact_decisions USING btree (project_id, subject_key, fact_type) WHERE ((superseded_by IS NULL) AND ((fact_type)::text = ANY ((ARRAY['utility_id'::character varying, 'external_org'::character varying, 'external_org_contact'::character varying, 'utility_type'::character varying, 'utility_subtype'::character varying, 'utility_function'::character varying, 'operational_status'::character varying, 'size'::character varying, 'material'::character varying, 'oh_ug'::character varying, 'row_placement'::character varying, 'orientation'::character varying, 'baseline'::character varying, 'station_from'::character varying, 'station_to'::character varying, 'offset_from'::character varying, 'offset_to'::character varying, 'sue_level'::character varying, 'conflict_description'::character varying, 'resolution_strategy'::character varying, 'notes'::character varying, 'alignment'::character varying, 'location_start'::character varying, 'location_end'::character varying, 'offset_side'::character varying, 'potential_conflict'::character varying, 'data_source'::character varying, 'marked_resolution'::character varying, 'committed_date'::character varying, 'action_due_date'::character varying, 'need_date'::character varying, 'closure_result'::character varying])::text[])));


--
-- Name: uq_fact_decision_fact; Type: INDEX; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE UNIQUE INDEX uq_fact_decision_fact ON public.fact_decisions USING btree (fact_id) WHERE (superseded_by IS NULL);


--
-- Name: uq_facts_content_sha256; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_facts_content_sha256 ON public.facts USING btree (content_sha256) WHERE (content_sha256 IS NOT NULL);


--
-- Name: uq_milestone_registrations_one_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_milestone_registrations_one_root ON public.milestone_registrations USING btree (milestone_id) WHERE (predecessor_registration_id IS NULL);


--
-- Name: uq_operative_support_field_role; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_operative_support_field_role ON public.operative_support USING btree (dependency_id, role, field_name) WHERE (field_name IS NOT NULL);


--
-- Name: uq_operative_support_record_role; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_operative_support_record_role ON public.operative_support USING btree (dependency_id, role) WHERE (field_name IS NULL);


--
-- Name: uq_retention_holds_active_project; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_retention_holds_active_project ON public.retention_holds USING btree (project_id) WHERE (lifted_at IS NULL);


--
-- Name: uq_source_segments_statement; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_source_segments_statement ON public.source_segments USING btree (statement_id) WHERE ((kind)::text = 'recorded_verbal_statement'::text);


--
-- Name: uq_work_decisions_commitment_lineage_one_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_work_decisions_commitment_lineage_one_root ON public.work_decisions USING btree (commitment_lineage_id, field) WHERE (predecessor_decision_id IS NULL);


--
-- Name: uq_work_decisions_one_root; Type: INDEX; Schema: public; Owner: corridor
--

CREATE UNIQUE INDEX uq_work_decisions_one_root ON public.work_decisions USING btree (dependency_id, field) WHERE (predecessor_decision_id IS NULL);


--
-- Name: coordination_summary_requests aa_coordination_summary_requests_retention_hold; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER aa_coordination_summary_requests_retention_hold BEFORE DELETE ON public.coordination_summary_requests FOR EACH ROW EXECUTE FUNCTION public.block_held_intermediary_delete();


--
-- Name: extraction_failure_diagnosis_requests aa_extraction_failure_diagnosis_requests_retention_hold; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER aa_extraction_failure_diagnosis_requests_retention_hold BEFORE DELETE ON public.extraction_failure_diagnosis_requests FOR EACH ROW EXECUTE FUNCTION public.block_held_intermediary_delete();


--
-- Name: production_run_explanation_requests aa_production_run_explanation_requests_retention_hold; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER aa_production_run_explanation_requests_retention_hold BEFORE DELETE ON public.production_run_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.block_held_intermediary_delete();


--
-- Name: revision_change_explanation_requests aa_revision_change_explanation_requests_retention_hold; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER aa_revision_change_explanation_requests_retention_hold BEFORE DELETE ON public.revision_change_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.block_held_intermediary_delete();


--
-- Name: source_intake_draft_requests aa_source_intake_draft_requests_retention_hold; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER aa_source_intake_draft_requests_retention_hold BEFORE DELETE ON public.source_intake_draft_requests FOR EACH ROW EXECUTE FUNCTION public.block_held_intermediary_delete();


--
-- Name: active_run_declarations active_run_declarations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER active_run_declarations_are_immutable BEFORE DELETE OR UPDATE ON public.active_run_declarations FOR EACH ROW EXECUTE FUNCTION public.enforce_active_run_declaration();


--
-- Name: active_run_declarations active_run_declarations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER active_run_declarations_reject_truncate BEFORE TRUNCATE ON public.active_run_declarations FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_active_run_declaration();


--
-- Name: assignment_notification_attempts assignment_attempts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_assignment_attempt_mutation();


--
-- Name: assignment_notification_attempts assignment_attempts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_attempts_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();


--
-- Name: assignment_notification_dispatches assignment_dispatch_identity_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_assignment_dispatch_identity();


--
-- Name: assignment_notification_dispatches assignment_dispatches_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_dispatches_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();


--
-- Name: assignment_notification_feedback assignment_feedback_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_feedback_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_feedback FOR EACH ROW EXECUTE FUNCTION public.refuse_assignment_feedback_mutation();


--
-- Name: assignment_notification_feedback assignment_feedback_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_feedback_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_feedback FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();


--
-- Name: assignment_notifications assignment_notifications_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_assignment_notification_immutable();


--
-- Name: assignment_notifications assignment_notifications_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER assignment_notifications_reject_truncate BEFORE TRUNCATE ON public.assignment_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER automatic_carry_forward_outcomes_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.automatic_carry_forward_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_automatic_carry_forward_outcome();


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_must_match_runs; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER automatic_carry_forward_outcomes_must_match_runs AFTER INSERT ON public.automatic_carry_forward_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_run_id();


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER automatic_carry_forward_outcomes_reject_truncate BEFORE TRUNCATE ON public.automatic_carry_forward_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_automatic_carry_forward_outcome();


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER automatic_carry_forward_receipts_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.automatic_carry_forward_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_automatic_carry_forward_receipt();


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER automatic_carry_forward_receipts_reject_truncate BEFORE TRUNCATE ON public.automatic_carry_forward_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_automatic_carry_forward_receipt();


--
-- Name: candidate_dispositions candidate_dispositions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER candidate_dispositions_are_immutable BEFORE DELETE OR UPDATE ON public.candidate_dispositions FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: candidate_dispositions candidate_dispositions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER candidate_dispositions_reject_truncate BEFORE TRUNCATE ON public.candidate_dispositions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: candidates candidate_run_lineage_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER candidate_run_lineage_is_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.candidates FOR EACH ROW EXECUTE FUNCTION public.enforce_candidate_run_lineage();


--
-- Name: candidates candidates_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER candidates_reject_truncate BEFORE TRUNCATE ON public.candidates FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_candidate_run_lineage();


--
-- Name: documents ck_documents_registered_supersession_participants; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER ck_documents_registered_supersession_participants AFTER INSERT OR UPDATE OF registry_id, superseded_by, superseded_on, supersession_source_document_id, supersession_source_page ON public.documents DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION public.enforce_document_supersession_registry();


--
-- Name: cohort_receipts cohort_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER cohort_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.cohort_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_cohort_receipt();


--
-- Name: cohort_receipts cohort_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER cohort_receipts_reject_truncate BEFORE TRUNCATE ON public.cohort_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_cohort_receipt();


--
-- Name: commitment_lineages commitment_lineages_have_current_statement; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER commitment_lineages_have_current_statement AFTER INSERT OR DELETE OR UPDATE ON public.commitment_lineages DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_lineage();


--
-- Name: condition_resolutions condition_resolutions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER condition_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.condition_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_condition_resolutions_mutation();


--
-- Name: condition_resolutions condition_resolutions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER condition_resolutions_reject_truncate BEFORE TRUNCATE ON public.condition_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_condition_resolutions_mutation();


--
-- Name: coordination_summary_configurations coordination_summary_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER coordination_summary_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.coordination_summary_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_coordination_summary_configurations_mutation();


--
-- Name: coordination_summary_configurations coordination_summary_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER coordination_summary_configurations_reject_truncate BEFORE TRUNCATE ON public.coordination_summary_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_coordination_summary_configurations_mutation();


--
-- Name: coordination_summary_requests coordination_summary_requests_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER coordination_summary_requests_reject_truncate BEFORE TRUNCATE ON public.coordination_summary_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_coordination_summary_requests_mutation();


--
-- Name: coordination_summary_requests coordination_summary_requests_retention_aware_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER coordination_summary_requests_retention_aware_immutable BEFORE DELETE OR UPDATE ON public.coordination_summary_requests FOR EACH ROW EXECUTE FUNCTION public.enforce_manifested_class_b_expiry();


--
-- Name: dependency_admission_outcomes dependency_admission_abstentions_require_eligibility; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_admission_abstentions_require_eligibility BEFORE INSERT ON public.dependency_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.require_dependency_admission_abstention_eligibility();


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_admission_outcomes_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_dependency_admission_outcomes();


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_must_match_runs; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_admission_outcomes_must_match_runs AFTER INSERT ON public.dependency_admission_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_policy_run_id();


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_admission_outcomes_reject_truncate BEFORE TRUNCATE ON public.dependency_admission_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_dependency_admission_outcomes();


--
-- Name: dependency_dismissals dependency_dismissals_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_dismissals_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_dismissals FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_dismissal_mutation();


--
-- Name: dependency_dismissals dependency_dismissals_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_dismissals_reject_truncate BEFORE TRUNCATE ON public.dependency_dismissals FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dependency_dismissal_mutation();


--
-- Name: dependency_events dependency_event_closure_link_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_closure_link_is_valid BEFORE INSERT OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_closure_link();


--
-- Name: dependency_event_evidence dependency_event_evidence_has_one_owner; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_event_evidence_has_one_owner AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_evidence_link_ownership();


--
-- Name: dependency_event_evidence dependency_event_evidence_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_evidence_is_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_evidence FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_evidence_mutation();


--
-- Name: dependency_event_evidence dependency_event_evidence_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_evidence_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_evidence FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_evidence();


--
-- Name: dependency_event_evidence dependency_event_evidence_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_evidence_reject_truncate BEFORE TRUNCATE ON public.dependency_event_evidence FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: dependency_event_migration_receipts dependency_event_migration_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_migration_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_migration_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_migration_receipt_mutation();


--
-- Name: dependency_event_migration_receipts dependency_event_migration_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_migration_receipts_reject_truncate BEFORE TRUNCATE ON public.dependency_event_migration_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decision_actor_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_decision_actor_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_scope_decisions FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision_actor();


--
-- Name: dependency_event_scopes dependency_event_scope_decision_link_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_decision_link_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_scopes FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision_link();


--
-- Name: dependency_event_scopes dependency_event_scope_decision_links_match_shape; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_event_scope_decision_links_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_scopes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision();


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decision_shape_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_event_scope_decision_shape_is_valid AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_scope_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision();


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decisions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_decisions_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_scope_decisions FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_scope_decision_mutation();


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decisions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_decisions_reject_truncate BEFORE TRUNCATE ON public.dependency_event_scope_decisions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: dependency_event_scopes dependency_event_scope_links_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_links_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_scopes FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_scope_link_mutation();


--
-- Name: dependency_event_scopes dependency_event_scope_links_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_event_scope_links_reject_truncate BEFORE TRUNCATE ON public.dependency_event_scopes FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: dependency_events dependency_event_timing_cardinality_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_event_timing_cardinality_is_valid AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.verify_dependency_event_timing_cardinality();


--
-- Name: dependency_event_timings dependency_event_timing_rows_match_event; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_event_timing_rows_match_event AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_timings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.verify_dependency_event_timing_cardinality();


--
-- Name: dependency_events dependency_events_have_valid_commitment_lineage; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER dependency_events_have_valid_commitment_lineage AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_lineage_event();


--
-- Name: dependency_events dependency_events_receive_initial_scope_decision; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_events_receive_initial_scope_decision AFTER INSERT ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.create_initial_dependency_event_scope_decision();


--
-- Name: dependency_evidence_sufficiencies dependency_evidence_sufficiency_scope_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dependency_evidence_sufficiency_scope_is_valid BEFORE INSERT OR UPDATE ON public.dependency_evidence_sufficiencies FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_evidence_sufficiency_scope_role();


--
-- Name: dispute_history_resolutions dispute_history_resolutions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dispute_history_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.dispute_history_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_dispute_history_resolution_mutation();


--
-- Name: dispute_history_resolutions dispute_history_resolutions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dispute_history_resolutions_reject_truncate BEFORE TRUNCATE ON public.dispute_history_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dispute_history_resolution_mutation();


--
-- Name: dispute_settlements dispute_settlements_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dispute_settlements_are_immutable BEFORE DELETE OR UPDATE ON public.dispute_settlements FOR EACH ROW EXECUTE FUNCTION public.reject_dispute_settlement_mutation();


--
-- Name: dispute_settlements dispute_settlements_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER dispute_settlements_reject_truncate BEFORE TRUNCATE ON public.dispute_settlements FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dispute_settlement_mutation();


--
-- Name: document_notification_attempts document_attempts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.document_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_document_attempt_mutation();


--
-- Name: document_notification_attempts document_attempts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_attempts_reject_truncate BEFORE TRUNCATE ON public.document_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();


--
-- Name: document_notification_dispatches document_dispatch_identity_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.document_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_document_dispatch_identity();


--
-- Name: document_notification_dispatches document_dispatches_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_dispatches_reject_truncate BEFORE TRUNCATE ON public.document_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();


--
-- Name: document_notifications document_notifications_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.document_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_document_notification_immutable();


--
-- Name: document_notifications document_notifications_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_notifications_reject_truncate BEFORE TRUNCATE ON public.document_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();


--
-- Name: document_rendition_derivations document_rendition_derivations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_rendition_derivations_are_immutable BEFORE DELETE OR UPDATE ON public.document_rendition_derivations FOR EACH ROW EXECUTE FUNCTION public.reject_document_rendition_derivation_mutation();


--
-- Name: document_rendition_derivations document_rendition_derivations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER document_rendition_derivations_reject_truncate BEFORE TRUNCATE ON public.document_rendition_derivations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_rendition_derivation_mutation();


--
-- Name: documentation_field_confirmations documentation_field_confirmations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER documentation_field_confirmations_are_immutable BEFORE DELETE OR UPDATE ON public.documentation_field_confirmations FOR EACH ROW EXECUTE FUNCTION public.reject_documentation_confirmation_mutation();


--
-- Name: documentation_field_confirmations documentation_field_confirmations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER documentation_field_confirmations_reject_truncate BEFORE TRUNCATE ON public.documentation_field_confirmations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_documentation_confirmation_mutation();


--
-- Name: due_action_notification_attempts due_action_attempts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.due_action_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_due_action_attempt_mutation();


--
-- Name: due_action_notification_attempts due_action_attempts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_attempts_reject_truncate BEFORE TRUNCATE ON public.due_action_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();


--
-- Name: due_action_notification_dispatches due_action_dispatch_identity_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.due_action_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_due_action_dispatch_identity();


--
-- Name: due_action_notification_dispatches due_action_dispatches_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_dispatches_reject_truncate BEFORE TRUNCATE ON public.due_action_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();


--
-- Name: due_action_notifications due_action_notifications_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.due_action_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_due_action_notification_immutable();


--
-- Name: due_action_notifications due_action_notifications_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_action_notifications_reject_truncate BEFORE TRUNCATE ON public.due_action_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();


--
-- Name: due_work_occurrences due_work_occurrence_identity_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_occurrence_identity_is_immutable BEFORE DELETE OR UPDATE ON public.due_work_occurrences FOR EACH ROW EXECUTE FUNCTION public.enforce_due_work_occurrence_identity();


--
-- Name: due_work_occurrences due_work_occurrences_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_occurrences_reject_truncate BEFORE TRUNCATE ON public.due_work_occurrences FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();


--
-- Name: due_work_receipts due_work_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.due_work_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_due_work_receipt_mutation();


--
-- Name: due_work_receipts due_work_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_receipts_reject_truncate BEFORE TRUNCATE ON public.due_work_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();


--
-- Name: due_work_schedules due_work_schedules_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_schedules_are_immutable BEFORE DELETE OR UPDATE ON public.due_work_schedules FOR EACH ROW EXECUTE FUNCTION public.enforce_due_work_schedule_mutation();


--
-- Name: due_work_schedules due_work_schedules_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER due_work_schedules_reject_truncate BEFORE TRUNCATE ON public.due_work_schedules FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();


--
-- Name: event_admission_acceptance_receipts event_admission_acceptance_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_admission_acceptance_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_acceptance_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_event_admission_acceptance_mutation();


--
-- Name: event_admission_activations event_admission_activations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_admission_activations_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_activations FOR EACH ROW EXECUTE FUNCTION public.refuse_event_admission_acceptance_mutation();


--
-- Name: event_admission_activations event_admission_activations_require_passing_receipt; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_admission_activations_require_passing_receipt BEFORE INSERT ON public.event_admission_activations FOR EACH ROW EXECUTE FUNCTION public.require_passing_event_admission_activation();


--
-- Name: event_admission_outcomes event_admission_outcomes_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_admission_outcomes_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_event_admission_outcomes();


--
-- Name: event_admission_outcomes event_admission_outcomes_must_match_runs; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER event_admission_outcomes_must_match_runs AFTER INSERT ON public.event_admission_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_policy_run_id();


--
-- Name: event_admission_outcomes event_admission_outcomes_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_admission_outcomes_reject_truncate BEFORE TRUNCATE ON public.event_admission_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_event_admission_outcomes();


--
-- Name: event_cohort_receipts event_cohort_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_cohort_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.event_cohort_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_event_cohort_receipt();


--
-- Name: event_cohort_receipts event_cohort_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER event_cohort_receipts_reject_truncate BEFORE TRUNCATE ON public.event_cohort_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_event_cohort_receipt();


--
-- Name: evidence_investigation_candidate_review_starts evidence_investigation_candidate_review_starts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_candidate_review_starts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_candidate_review_starts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_capture_contracts_are_immutable BEFORE DELETE OR UPDATE ON public.evidence_investigation_capture_contracts FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation();


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_capture_contracts_reject_truncate BEFORE TRUNCATE ON public.evidence_investigation_capture_contracts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation();


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_capture_results_are_immutable BEFORE DELETE OR UPDATE ON public.evidence_investigation_capture_results FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_investigation_capture_results_mutation();


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_capture_results_reject_truncate BEFORE TRUNCATE ON public.evidence_investigation_capture_results FOR EACH STATEMENT EXECUTE FUNCTION public.reject_evidence_investigation_capture_results_mutation();


--
-- Name: evidence_investigation_evaluation_receipts evidence_investigation_evaluation_receipts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_evaluation_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_evaluation_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_packet_receipts evidence_investigation_packet_receipts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_packet_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_packet_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_review_observations evidence_investigation_review_observations_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_review_observations_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_review_observations FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_runs evidence_investigation_runs_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_runs_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_runs FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_shadow_cases_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_cases FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_shadow_executions_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_executions FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_shadow_outcomes evidence_investigation_shadow_outcomes_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_shadow_outcomes_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_outcomes FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_investigation_step_receipts evidence_investigation_step_receipts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER evidence_investigation_step_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_step_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();


--
-- Name: evidence_links evidence_links_have_one_owner; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER evidence_links_have_one_owner AFTER INSERT OR DELETE OR UPDATE ON public.evidence_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_evidence_link_ownership();


--
-- Name: dependency_events external_party_statement_events_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_events_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_mutation();


--
-- Name: dependency_events external_party_statement_events_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_events_reject_truncate BEFORE TRUNCATE ON public.dependency_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: evidence_links external_party_statement_evidence_is_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_evidence_is_immutable BEFORE DELETE OR UPDATE ON public.evidence_links FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_evidence_mutation();


--
-- Name: evidence_links external_party_statement_evidence_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_evidence_reject_truncate BEFORE TRUNCATE ON public.evidence_links FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: dependency_event_timings external_party_statement_timings_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_timings_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_timings FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_child_mutation();


--
-- Name: dependency_event_timings external_party_statement_timings_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_party_statement_timings_reject_truncate BEFORE TRUNCATE ON public.dependency_event_timings FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();


--
-- Name: external_report_artifacts external_report_artifacts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_report_artifacts_are_immutable BEFORE DELETE OR UPDATE ON public.external_report_artifacts FOR EACH ROW EXECUTE FUNCTION public.prevent_external_report_artifact_mutation();


--
-- Name: external_report_artifacts external_report_artifacts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_report_artifacts_reject_truncate BEFORE TRUNCATE ON public.external_report_artifacts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_report_artifact_truncate();


--
-- Name: external_report_releases external_report_releases_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_report_releases_reject_truncate BEFORE TRUNCATE ON public.external_report_releases FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_report_release_truncate();


--
-- Name: external_report_releases external_report_releases_require_artifact_content_owner; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER external_report_releases_require_artifact_content_owner BEFORE INSERT ON public.external_report_releases FOR EACH ROW EXECUTE FUNCTION public.enforce_external_report_release_content_owner();


--
-- Name: extraction_failure_diagnosis_configurations extraction_failure_diagnosis_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_failure_diagnosis_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_failure_diagnosis_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation();


--
-- Name: extraction_failure_diagnosis_configurations extraction_failure_diagnosis_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_failure_diagnosis_configurations_reject_truncate BEFORE TRUNCATE ON public.extraction_failure_diagnosis_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation();


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_failure_diagnosis_requests_reject_truncate BEFORE TRUNCATE ON public.extraction_failure_diagnosis_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation();


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_retention_aware_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_failure_diagnosis_requests_retention_aware_immutable BEFORE DELETE OR UPDATE ON public.extraction_failure_diagnosis_requests FOR EACH ROW EXECUTE FUNCTION public.enforce_manifested_class_b_expiry();


--
-- Name: extraction_measurement_case_states extraction_measurement_case_state_lineage; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_measurement_case_state_lineage BEFORE INSERT ON public.extraction_measurement_case_states FOR EACH ROW EXECUTE FUNCTION public.validate_extraction_measurement_case_state();


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_measurement_case_states_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_measurement_case_states FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_measurement_case_mutation();


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_measurement_case_states_reject_truncate BEFORE TRUNCATE ON public.extraction_measurement_case_states FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_measurement_case_mutation();


--
-- Name: extraction_runs extraction_run_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_run_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_runs FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_run_receipt_mutation();


--
-- Name: extraction_runs extraction_run_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER extraction_run_receipts_reject_truncate BEFORE TRUNCATE ON public.extraction_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_run_receipt_mutation();


--
-- Name: key_date_draft_receipts key_date_draft_receipts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER key_date_draft_receipts_append_only BEFORE DELETE OR UPDATE ON public.key_date_draft_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_key_date_draft_receipt_mutation();


--
-- Name: key_date_draft_row_receipts key_date_draft_row_receipts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER key_date_draft_row_receipts_append_only BEFORE DELETE OR UPDATE ON public.key_date_draft_row_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_key_date_draft_receipt_mutation();


--
-- Name: legacy_ledger_archives legacy_ledger_archives_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER legacy_ledger_archives_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.legacy_ledger_archives FOR EACH ROW EXECUTE FUNCTION public.reject_legacy_ledger_archive_mutation();


--
-- Name: legacy_ledger_archives legacy_ledger_archives_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER legacy_ledger_archives_reject_truncate BEFORE TRUNCATE ON public.legacy_ledger_archives FOR EACH STATEMENT EXECUTE FUNCTION public.reject_legacy_ledger_archive_mutation();


--
-- Name: milestone_registrations milestone_registrations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER milestone_registrations_are_immutable BEFORE DELETE OR UPDATE ON public.milestone_registrations FOR EACH ROW EXECUTE FUNCTION public.enforce_milestone_registration();


--
-- Name: milestone_registrations milestone_registrations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER milestone_registrations_reject_truncate BEFORE TRUNCATE ON public.milestone_registrations FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_milestone_registration();


--
-- Name: operative_support operative_event_evidence_scope_is_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER operative_event_evidence_scope_is_valid BEFORE INSERT OR UPDATE ON public.operative_support FOR EACH ROW EXECUTE FUNCTION public.validate_operative_event_evidence_scope_role();


--
-- Name: organization_identity_activations organization_identity_activations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER organization_identity_activations_are_immutable BEFORE DELETE OR UPDATE ON public.organization_identity_activations FOR EACH ROW EXECUTE FUNCTION public.reject_organization_identity_mutation();


--
-- Name: organization_identity_activations organization_identity_activations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER organization_identity_activations_reject_truncate BEFORE TRUNCATE ON public.organization_identity_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_organization_identity_mutation();


--
-- Name: organization_identity_receipts organization_identity_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER organization_identity_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.organization_identity_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_organization_identity_mutation();


--
-- Name: organization_identity_receipts organization_identity_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER organization_identity_receipts_reject_truncate BEFORE TRUNCATE ON public.organization_identity_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_organization_identity_mutation();


--
-- Name: policy_approvals policy_approvals_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER policy_approvals_are_immutable BEFORE DELETE OR UPDATE ON public.policy_approvals FOR EACH ROW EXECUTE FUNCTION public.enforce_policy_approvals();


--
-- Name: policy_approvals policy_approvals_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER policy_approvals_reject_truncate BEFORE TRUNCATE ON public.policy_approvals FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_policy_approvals();


--
-- Name: policy_runs policy_runs_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER policy_runs_are_immutable BEFORE DELETE OR UPDATE ON public.policy_runs FOR EACH ROW EXECUTE FUNCTION public.enforce_policy_runs();


--
-- Name: policy_runs policy_runs_must_match_outcomes; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER policy_runs_must_match_outcomes AFTER INSERT ON public.policy_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_run_counts();


--
-- Name: policy_runs policy_runs_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER policy_runs_reject_truncate BEFORE TRUNCATE ON public.policy_runs FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_policy_runs();


--
-- Name: external_report_releases prevent_external_report_release_mutation; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER prevent_external_report_release_mutation BEFORE DELETE OR UPDATE ON public.external_report_releases FOR EACH ROW EXECUTE FUNCTION public.prevent_external_report_release_mutation();


--
-- Name: production_run_explanation_configurations production_run_explanation_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER production_run_explanation_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.production_run_explanation_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_production_run_explanation_configurations_mutation();


--
-- Name: production_run_explanation_configurations production_run_explanation_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER production_run_explanation_configurations_reject_truncate BEFORE TRUNCATE ON public.production_run_explanation_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_production_run_explanation_configurations_mutation();


--
-- Name: production_run_explanation_requests production_run_explanation_requests_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER production_run_explanation_requests_reject_truncate BEFORE TRUNCATE ON public.production_run_explanation_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_production_run_explanation_requests_mutation();


--
-- Name: production_run_explanation_requests production_run_explanation_requests_retention_aware_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER production_run_explanation_requests_retention_aware_immutable BEFORE DELETE OR UPDATE ON public.production_run_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.enforce_manifested_class_b_expiry();


--
-- Name: project_check_configurations project_check_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER project_check_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.project_check_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_project_check_configuration_mutation();


--
-- Name: project_check_configurations project_check_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER project_check_configurations_reject_truncate BEFORE TRUNCATE ON public.project_check_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_project_check_configuration_mutation();


--
-- Name: reconfirmation_receipts reconfirmation_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER reconfirmation_receipts_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.reconfirmation_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_reconfirmation_receipt_mutation();


--
-- Name: reconfirmation_receipts reconfirmation_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER reconfirmation_receipts_reject_truncate BEFORE TRUNCATE ON public.reconfirmation_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_reconfirmation_receipt_mutation();


--
-- Name: revision_change_explanation_configurations revision_change_explanation_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_change_explanation_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.revision_change_explanation_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_revision_change_explanation_configurations_mutation();


--
-- Name: revision_change_explanation_configurations revision_change_explanation_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_change_explanation_configurations_reject_truncate BEFORE TRUNCATE ON public.revision_change_explanation_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_change_explanation_configurations_mutation();


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_change_explanation_requests_reject_truncate BEFORE TRUNCATE ON public.revision_change_explanation_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_change_explanation_requests_mutation();


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_retention_aware_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_change_explanation_requests_retention_aware_immutable BEFORE DELETE OR UPDATE ON public.revision_change_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.enforce_manifested_class_b_expiry();


--
-- Name: revision_comparison_findings revision_comparison_findings_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_comparison_findings_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.revision_comparison_findings FOR EACH ROW EXECUTE FUNCTION public.enforce_revision_comparison_finding_mutation();


--
-- Name: revision_comparison_findings revision_comparison_findings_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_comparison_findings_reject_truncate BEFORE TRUNCATE ON public.revision_comparison_findings FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_comparison_truncate();


--
-- Name: revision_comparison_runs revision_comparison_runs_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_comparison_runs_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.revision_comparison_runs FOR EACH ROW EXECUTE FUNCTION public.enforce_revision_comparison_run_mutation();


--
-- Name: revision_comparison_runs revision_comparison_runs_must_commit_sealed; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER revision_comparison_runs_must_commit_sealed AFTER INSERT ON public.revision_comparison_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_sealed_revision_comparison();


--
-- Name: revision_comparison_runs revision_comparison_runs_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER revision_comparison_runs_reject_truncate BEFORE TRUNCATE ON public.revision_comparison_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_comparison_truncate();


--
-- Name: schedule_governing_derivations schedule_governing_derivations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_governing_derivations_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_governing_derivations FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: schedule_governing_derivations schedule_governing_derivations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_governing_derivations_reject_truncate BEFORE TRUNCATE ON public.schedule_governing_derivations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: schedule_link_activations schedule_link_activations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_link_activations_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_link_activations FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: schedule_link_activations schedule_link_activations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_link_activations_reject_truncate BEFORE TRUNCATE ON public.schedule_link_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: schedule_link_receipts schedule_link_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_link_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_link_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: schedule_link_receipts schedule_link_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER schedule_link_receipts_reject_truncate BEFORE TRUNCATE ON public.schedule_link_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();


--
-- Name: scheduled_report_publications scheduled_report_publications_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER scheduled_report_publications_are_immutable BEFORE DELETE OR UPDATE ON public.scheduled_report_publications FOR EACH ROW EXECUTE FUNCTION public.reject_scheduled_report_publications_mutation();


--
-- Name: scheduled_report_publications scheduled_report_publications_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER scheduled_report_publications_reject_truncate BEFORE TRUNCATE ON public.scheduled_report_publications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_scheduled_report_publications_mutation();


--
-- Name: source_intake_draft_configurations source_intake_draft_configurations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER source_intake_draft_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.source_intake_draft_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_source_intake_draft_configurations_mutation();


--
-- Name: source_intake_draft_configurations source_intake_draft_configurations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER source_intake_draft_configurations_reject_truncate BEFORE TRUNCATE ON public.source_intake_draft_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_source_intake_draft_configurations_mutation();


--
-- Name: source_intake_draft_requests source_intake_draft_requests_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER source_intake_draft_requests_reject_truncate BEFORE TRUNCATE ON public.source_intake_draft_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_source_intake_draft_requests_mutation();


--
-- Name: source_intake_draft_requests source_intake_draft_requests_retention_aware_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER source_intake_draft_requests_retention_aware_immutable BEFORE DELETE OR UPDATE ON public.source_intake_draft_requests FOR EACH ROW EXECUTE FUNCTION public.enforce_manifested_class_b_expiry();


--
-- Name: statement_coordination_receipts statement_coordination_receipts_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_coordination_receipts statement_coordination_receipts_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_receipts_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_coordination_reversal_effects statement_coordination_reversal_effects_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_reversal_effects_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_reversal_effects FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_coordination_reversal_effects statement_coordination_reversal_effects_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_reversal_effects_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_reversal_effects FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_coordination_reversals statement_coordination_reversals_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_reversals_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_reversals FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_coordination_reversals statement_coordination_reversals_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_coordination_reversals_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_reversals FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();


--
-- Name: statement_suggestion_eligibility_declarations statement_suggestion_eligibility_declarations_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_eligibility_declarations_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_eligibility_declarations FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: statement_suggestion_eligibility_declarations statement_suggestion_eligibility_declarations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_eligibility_declarations_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_eligibility_declarations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: statement_suggestion_protection_ends statement_suggestion_protection_ends_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_protection_ends_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_protection_ends FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: statement_suggestion_protection_ends statement_suggestion_protection_ends_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_protection_ends_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_protection_ends FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: statement_suggestion_protections statement_suggestion_protections_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_protections_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_protections FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: statement_suggestion_protections statement_suggestion_protections_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER statement_suggestion_protections_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_protections FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();


--
-- Name: extracted_proposal_facts trg_extracted_proposal_facts_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extracted_proposal_facts_immutable BEFORE DELETE OR UPDATE ON public.extracted_proposal_facts FOR EACH ROW EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: extracted_proposal_facts trg_extracted_proposal_facts_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extracted_proposal_facts_no_truncate BEFORE TRUNCATE ON public.extracted_proposal_facts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: extracted_proposals trg_extracted_proposals_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extracted_proposals_immutable BEFORE DELETE OR UPDATE ON public.extracted_proposals FOR EACH ROW EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: extracted_proposals trg_extracted_proposals_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extracted_proposals_no_truncate BEFORE TRUNCATE ON public.extracted_proposals FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: extraction_run_candidates trg_extraction_run_candidates_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extraction_run_candidates_immutable BEFORE DELETE OR UPDATE ON public.extraction_run_candidates FOR EACH ROW EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: extraction_run_candidates trg_extraction_run_candidates_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_extraction_run_candidates_no_truncate BEFORE TRUNCATE ON public.extraction_run_candidates FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: fact_applies_to trg_fact_applies_to_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_applies_to_append_only BEFORE DELETE OR UPDATE ON public.fact_applies_to FOR EACH ROW EXECUTE FUNCTION public.enforce_structured_fact_satellite_append_only();


--
-- Name: fact_closure_results trg_fact_closure_results_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_closure_results_append_only BEFORE DELETE OR UPDATE ON public.fact_closure_results FOR EACH ROW EXECUTE FUNCTION public.enforce_structured_fact_satellite_append_only();


--
-- Name: fact_closure_sources trg_fact_closure_sources_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_closure_sources_append_only BEFORE DELETE OR UPDATE ON public.fact_closure_sources FOR EACH ROW EXECUTE FUNCTION public.enforce_structured_fact_satellite_append_only();


--
-- Name: fact_decisions trg_fact_decisions_guard; Type: TRIGGER; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TRIGGER trg_fact_decisions_guard BEFORE INSERT OR DELETE OR UPDATE ON public.fact_decisions FOR EACH ROW EXECUTE FUNCTION public.enforce_fact_decision_write();


--
-- Name: fact_dispositions trg_fact_dispositions_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_dispositions_immutable BEFORE DELETE OR UPDATE ON public.fact_dispositions FOR EACH ROW EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: fact_dispositions trg_fact_dispositions_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_dispositions_no_truncate BEFORE TRUNCATE ON public.fact_dispositions FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_immutable_proposal_spine();


--
-- Name: fact_sources trg_fact_sources_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_sources_append_only BEFORE DELETE OR UPDATE ON public.fact_sources FOR EACH ROW EXECUTE FUNCTION public.enforce_fact_sources_append_only();


--
-- Name: fact_sources trg_fact_sources_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_sources_no_truncate BEFORE TRUNCATE ON public.fact_sources FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_fact_sources_append_only();


--
-- Name: fact_statement_timings trg_fact_statement_timings_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_fact_statement_timings_append_only BEFORE DELETE OR UPDATE ON public.fact_statement_timings FOR EACH ROW EXECUTE FUNCTION public.enforce_structured_fact_satellite_append_only();


--
-- Name: facts trg_facts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_facts_append_only BEFORE DELETE OR UPDATE ON public.facts FOR EACH ROW EXECUTE FUNCTION public.enforce_facts_append_only();


--
-- Name: facts trg_facts_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_facts_no_truncate BEFORE TRUNCATE ON public.facts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_facts_append_only();


--
-- Name: facts trg_facts_require_value_source; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER trg_facts_require_value_source AFTER INSERT ON public.facts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_fact_value_source();


--
-- Name: project_record_revisions trg_project_record_revisions_guard; Type: TRIGGER; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TRIGGER trg_project_record_revisions_guard BEFORE INSERT OR DELETE OR UPDATE ON public.project_record_revisions FOR EACH ROW EXECUTE FUNCTION public.enforce_project_record_revision_write();


--
-- Name: source_fact_append_receipts trg_source_fact_append_receipts_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_source_fact_append_receipts_immutable BEFORE DELETE OR UPDATE ON public.source_fact_append_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_source_fact_append_receipts_immutable();


--
-- Name: source_fact_append_receipts trg_source_fact_append_receipts_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_source_fact_append_receipts_no_truncate BEFORE TRUNCATE ON public.source_fact_append_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_source_fact_append_receipts_immutable();


--
-- Name: source_segments trg_source_segments_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_source_segments_append_only BEFORE DELETE OR UPDATE ON public.source_segments FOR EACH ROW EXECUTE FUNCTION public.enforce_source_segments_append_only();


--
-- Name: source_segments trg_source_segments_no_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_source_segments_no_truncate BEFORE TRUNCATE ON public.source_segments FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_source_segments_append_only();


--
-- Name: source_segments trg_source_segments_prose_non_overlap; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_source_segments_prose_non_overlap BEFORE INSERT ON public.source_segments FOR EACH ROW EXECUTE FUNCTION public.enforce_prose_segment_non_overlap();


--
-- Name: stated_by_people trg_stated_by_people_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_stated_by_people_append_only BEFORE DELETE OR UPDATE ON public.stated_by_people FOR EACH ROW EXECUTE FUNCTION public.enforce_subject_resolution_append_only();


--
-- Name: subject_candidate_suggestions trg_subject_candidate_suggestions_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_subject_candidate_suggestions_append_only BEFORE DELETE OR UPDATE ON public.subject_candidate_suggestions FOR EACH ROW EXECUTE FUNCTION public.enforce_subject_resolution_append_only();


--
-- Name: subject_resolution_attempts trg_subject_resolution_attempts_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_subject_resolution_attempts_append_only BEFORE DELETE OR UPDATE ON public.subject_resolution_attempts FOR EACH ROW EXECUTE FUNCTION public.enforce_subject_resolution_append_only();


--
-- Name: subject_resolution_candidates trg_subject_resolution_candidates_append_only; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER trg_subject_resolution_candidates_append_only BEFORE DELETE OR UPDATE ON public.subject_resolution_candidates FOR EACH ROW EXECUTE FUNCTION public.enforce_subject_resolution_append_only();


--
-- Name: subject_resolution_decisions trg_subject_resolution_decisions_guard; Type: TRIGGER; Schema: public; Owner: corridor_fact_decision_writer
--

CREATE TRIGGER trg_subject_resolution_decisions_guard BEFORE INSERT OR DELETE OR UPDATE ON public.subject_resolution_decisions FOR EACH ROW EXECUTE FUNCTION public.enforce_subject_resolution_decision_write();


--
-- Name: unreadable_cell_admission_activations unreadable_cell_admission_activations_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_admission_activations_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_admission_activations FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_admission_activations_mutation();


--
-- Name: unreadable_cell_admission_activations unreadable_cell_admission_activations_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_admission_activations_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_admission_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_admission_activations_mutation();


--
-- Name: unreadable_cell_reading_profiles unreadable_cell_reading_profiles_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_profiles_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_profiles FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation();


--
-- Name: unreadable_cell_reading_profiles unreadable_cell_reading_profiles_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_profiles_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_profiles FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation();


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_runs_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_runs FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_runs_mutation();


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_runs_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_runs_mutation();


--
-- Name: unreadable_cell_reading_steps unreadable_cell_reading_steps_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_steps_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_steps FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_steps_mutation();


--
-- Name: unreadable_cell_reading_steps unreadable_cell_reading_steps_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_reading_steps_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_steps FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_steps_mutation();


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_resolutions_mutation();


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER unreadable_cell_resolutions_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_resolutions_mutation();


--
-- Name: dependency_events verbal_dependency_events_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER verbal_dependency_events_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.reject_verbal_dependency_event_mutation();


--
-- Name: dependency_events verbal_dependency_events_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER verbal_dependency_events_reject_truncate BEFORE TRUNCATE ON public.dependency_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_verbal_dependency_event_mutation();


--
-- Name: dependency_event_timings verbal_statement_timings_match_shape; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER verbal_statement_timings_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_timings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_verbal_statement_shape();


--
-- Name: dependency_events verbal_statements_match_shape; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER verbal_statements_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_verbal_statement_shape();


--
-- Name: work_decision_milestone_impacts work_decision_milestone_impacts_are_valid; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER work_decision_milestone_impacts_are_valid AFTER INSERT OR DELETE OR UPDATE ON public.work_decision_milestone_impacts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_milestone_impact();


--
-- Name: work_decisions work_decisions_are_immutable; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER work_decisions_are_immutable BEFORE DELETE OR UPDATE ON public.work_decisions FOR EACH ROW EXECUTE FUNCTION public.enforce_work_decision();


--
-- Name: work_decisions work_decisions_have_valid_milestone_impact; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE CONSTRAINT TRIGGER work_decisions_have_valid_milestone_impact AFTER INSERT OR DELETE OR UPDATE ON public.work_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_milestone_impact();


--
-- Name: work_decisions work_decisions_have_valid_subject; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER work_decisions_have_valid_subject BEFORE INSERT OR UPDATE ON public.work_decisions FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_subject();


--
-- Name: work_decisions work_decisions_reject_truncate; Type: TRIGGER; Schema: public; Owner: corridor
--

CREATE TRIGGER work_decisions_reject_truncate BEFORE TRUNCATE ON public.work_decisions FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_work_decision();


--
-- Name: retired_automatic_carry_forward_policy_activations active_automatic_carry_forward_policies_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: active_extraction_runs active_extraction_runs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: active_run_declarations active_run_declarations_document_id_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_document_id_extraction_run_id_fkey FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: active_run_declarations active_run_declarations_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: assertions assertions_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: assertions assertions_evidence_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);


--
-- Name: assignment_notification_attempts assignment_notification_attempts_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.assignment_notification_dispatches(id);


--
-- Name: assignment_notification_attempts assignment_notification_attempts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: assignment_notification_dispatches assignment_notification_dispatches_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.assignment_notifications(id);


--
-- Name: assignment_notification_dispatches assignment_notification_dispatches_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.assignment_notifications(id);


--
-- Name: assignment_notification_feedback assignment_notification_feedback_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: assignment_notifications assignment_notifications_assignment_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_assignment_decision_id_fkey FOREIGN KEY (assignment_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: assignment_notifications assignment_notifications_commitment_lineage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: assignment_notifications assignment_notifications_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: assignment_notifications assignment_notifications_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: assignment_notifications assignment_notifications_recipient_roster_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_recipient_roster_entry_id_fkey FOREIGN KEY (recipient_roster_entry_id) REFERENCES public.project_roster_entries(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_comparison_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_predecessor_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_predecessor_candidate_id_fkey FOREIGN KEY (predecessor_candidate_id) REFERENCES public.candidates(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_receipt_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_receipt_audit_log_id_fkey FOREIGN KEY (receipt_audit_log_id) REFERENCES public.automatic_carry_forward_receipts(audit_log_id);


--
-- Name: automatic_carry_forward_outcomes automatic_carry_forward_outcomes_successor_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_recei_predecessor_support_transfer_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_recei_predecessor_support_transfer_fkey FOREIGN KEY (predecessor_support_transfer_audit_id) REFERENCES public.audit_log(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_comparison_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_origin_admission_audit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_origin_admission_audit_id_fkey FOREIGN KEY (origin_admission_audit_id) REFERENCES public.audit_log(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_predecessor_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_predecessor_candidate_id_fkey FOREIGN KEY (predecessor_candidate_id) REFERENCES public.candidates(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: automatic_carry_forward_receipts automatic_carry_forward_receipts_successor_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);


--
-- Name: candidate_dispositions candidate_dispositions_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidate_dispositions
    ADD CONSTRAINT candidate_dispositions_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: candidates candidates_merged_into_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES public.dependencies(id);


--
-- Name: candidates candidates_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: candidates candidates_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.documents(id);


--
-- Name: cohort_receipts cohort_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: cohort_receipts cohort_receipts_revision_comparison_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_revision_comparison_run_id_fkey FOREIGN KEY (revision_comparison_run_id) REFERENCES public.revision_comparison_runs(id);


--
-- Name: commitment_lineages commitment_lineages_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.commitment_lineages
    ADD CONSTRAINT commitment_lineages_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: coordination_summary_configurations coordination_summary_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_configurations
    ADD CONSTRAINT coordination_summary_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: coordination_summary_requests coordination_summary_requests_configuration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.coordination_summary_configurations(id);


--
-- Name: coordination_summary_requests coordination_summary_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: dependencies dependencies_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: dependency_admission_outcomes dependency_admission_outcomes_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: dependency_dismissals dependency_dismissals_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_dismissals
    ADD CONSTRAINT dependency_dismissals_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: dependency_event_evidence dependency_event_evidence_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);


--
-- Name: dependency_event_evidence dependency_event_evidence_evidence_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);


--
-- Name: dependency_event_migration_receipts dependency_event_migration_receipts_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_migration_receipts
    ADD CONSTRAINT dependency_event_migration_receipts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id) ON DELETE CASCADE;


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decisi_supersedes_scope_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisi_supersedes_scope_decision_id_fkey FOREIGN KEY (supersedes_scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);


--
-- Name: dependency_event_scope_decisions dependency_event_scope_decisions_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisions_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);


--
-- Name: dependency_event_scopes dependency_event_scopes_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: dependency_event_scopes dependency_event_scopes_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);


--
-- Name: dependency_event_timings dependency_event_timings_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT dependency_event_timings_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);


--
-- Name: dependency_evidence_sufficiencies dependency_evidence_sufficiencies_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: dependency_evidence_sufficiencies dependency_evidence_sufficiencies_evidence_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);


--
-- Name: discovered_references discovered_references_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT discovered_references_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: dispute_history_resolutions dispute_history_resolutions_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: dispute_history_resolutions dispute_history_resolutions_newer_assertion_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_newer_assertion_id_fkey FOREIGN KEY (newer_assertion_id) REFERENCES public.assertions(id);


--
-- Name: dispute_history_resolutions dispute_history_resolutions_older_assertion_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_older_assertion_id_fkey FOREIGN KEY (older_assertion_id) REFERENCES public.assertions(id);


--
-- Name: dispute_settlements dispute_settlements_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dispute_settlements
    ADD CONSTRAINT dispute_settlements_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: doc_pages doc_pages_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: document_notification_attempts document_notification_attempts_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.document_notification_dispatches(id);


--
-- Name: document_notification_attempts document_notification_attempts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: document_notification_dispatches document_notification_dispatches_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.document_notifications(id);


--
-- Name: document_notification_dispatches document_notification_dispatches_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: document_notifications document_notifications_commitment_lineage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: document_notifications document_notifications_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: document_notifications document_notifications_predecessor_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_predecessor_document_id_fkey FOREIGN KEY (predecessor_document_id) REFERENCES public.documents(id);


--
-- Name: document_notifications document_notifications_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: document_notifications document_notifications_review_confirmation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_review_confirmation_id_fkey FOREIGN KEY (review_confirmation_id) REFERENCES public.documentation_field_confirmations(id);


--
-- Name: document_notifications document_notifications_statement_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_statement_event_id_fkey FOREIGN KEY (statement_event_id) REFERENCES public.dependency_events(id);


--
-- Name: document_notifications document_notifications_successor_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_successor_document_id_fkey FOREIGN KEY (successor_document_id) REFERENCES public.documents(id);


--
-- Name: document_quarantines document_quarantines_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_quarantines
    ADD CONSTRAINT document_quarantines_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: document_rendition_derivations document_rendition_derivations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: documentation_field_confirmations documentation_field_confirmations_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT documentation_field_confirmations_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: documents documents_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: due_action_notification_attempts due_action_notification_attempts_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.due_action_notification_dispatches(id);


--
-- Name: due_action_notification_attempts due_action_notification_attempts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: due_action_notification_dispatches due_action_notification_dispatches_notification_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.due_action_notifications(id);


--
-- Name: due_action_notification_dispatches due_action_notification_dispatches_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: due_action_notifications due_action_notifications_commitment_lineage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: due_action_notifications due_action_notifications_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: due_action_notifications due_action_notifications_plan_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_plan_decision_id_fkey FOREIGN KEY (plan_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: due_action_notifications due_action_notifications_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: due_action_notifications due_action_notifications_recipient_roster_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_recipient_roster_entry_id_fkey FOREIGN KEY (recipient_roster_entry_id) REFERENCES public.project_roster_entries(id);


--
-- Name: due_work_occurrences due_work_occurrences_scheduled_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_scheduled_job_id_fkey FOREIGN KEY (scheduled_job_id) REFERENCES public.due_work_schedules(id);


--
-- Name: due_work_receipts due_work_receipts_occurrence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_occurrence_id_fkey FOREIGN KEY (occurrence_id) REFERENCES public.due_work_occurrences(id);


--
-- Name: due_work_receipts due_work_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: due_work_schedules due_work_schedules_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: event_admission_acceptance_receipts event_admission_acceptance_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_acceptance_receipts
    ADD CONSTRAINT event_admission_acceptance_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: event_admission_activations event_admission_activations_acceptance_receipt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_acceptance_receipt_id_fkey FOREIGN KEY (acceptance_receipt_id) REFERENCES public.event_admission_acceptance_receipts(id);


--
-- Name: event_admission_activations event_admission_activations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: event_admission_outcomes event_admission_outcomes_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: event_admission_outcomes event_admission_outcomes_dependency_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_dependency_event_id_fkey FOREIGN KEY (dependency_event_id) REFERENCES public.dependency_events(id);


--
-- Name: event_cohort_receipts event_cohort_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT event_cohort_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_candidate_review_starts evidence_investigation_candidate_review_start_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_start_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: evidence_investigation_candidate_review_starts evidence_investigation_candidate_review_starts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_capture_contracts evidence_investigation_capture_contracts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_capture_contract_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_capture_contract_id_fkey FOREIGN KEY (capture_contract_id) REFERENCES public.evidence_investigation_capture_contracts(id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);


--
-- Name: evidence_investigation_capture_results evidence_investigation_capture_results_shadow_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);


--
-- Name: evidence_investigation_packet_receipts evidence_investigation_packet_receipts_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);


--
-- Name: evidence_investigation_review_observations evidence_investigation_review_observations_shadow_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT evidence_investigation_review_observations_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);


--
-- Name: evidence_investigation_runs evidence_investigation_runs_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: evidence_investigation_runs evidence_investigation_runs_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);


--
-- Name: evidence_investigation_runs evidence_investigation_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);


--
-- Name: evidence_investigation_shadow_cases evidence_investigation_shadow_cases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);


--
-- Name: evidence_investigation_shadow_executions evidence_investigation_shadow_executions_shadow_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);


--
-- Name: evidence_investigation_shadow_outcomes evidence_investigation_shadow_outcomes_shadow_case_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);


--
-- Name: evidence_investigation_step_receipts evidence_investigation_step_receipts_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT evidence_investigation_step_receipts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);


--
-- Name: evidence_links evidence_links_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: evidence_links evidence_links_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: external_report_artifacts external_report_artifacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_artifacts
    ADD CONSTRAINT external_report_artifacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: external_report_releases external_report_releases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT external_report_releases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extracted_proposal_facts extracted_proposal_facts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT extracted_proposal_facts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extracted_proposals extracted_proposals_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT extracted_proposals_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: extracted_proposals extracted_proposals_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT extracted_proposals_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: extracted_proposals extracted_proposals_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT extracted_proposals_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extraction_failure_diagnosis_configurations extraction_failure_diagnosis_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations
    ADD CONSTRAINT extraction_failure_diagnosis_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_configuration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.extraction_failure_diagnosis_configurations(id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);


--
-- Name: extraction_failure_diagnosis_requests extraction_failure_diagnosis_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_predecessor_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_predecessor_state_id_fkey FOREIGN KEY (predecessor_state_id) REFERENCES public.extraction_measurement_case_states(id);


--
-- Name: extraction_measurement_case_states extraction_measurement_case_states_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: extraction_run_candidates extraction_run_candidates_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates
    ADD CONSTRAINT extraction_run_candidates_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: extraction_run_candidates extraction_run_candidates_extraction_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_run_candidates
    ADD CONSTRAINT extraction_run_candidates_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);


--
-- Name: extraction_runs extraction_runs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: fact_applies_to fact_applies_to_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT fact_applies_to_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_closure_results fact_closure_results_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results
    ADD CONSTRAINT fact_closure_results_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_closure_sources fact_closure_sources_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT fact_closure_sources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_decisions fact_decisions_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions
    ADD CONSTRAINT fact_decisions_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.facts(id);


--
-- Name: fact_decisions fact_decisions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions
    ADD CONSTRAINT fact_decisions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_decisions fact_decisions_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions
    ADD CONSTRAINT fact_decisions_revision_id_fkey FOREIGN KEY (revision_id) REFERENCES public.project_record_revisions(id);


--
-- Name: fact_decisions fact_decisions_superseded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.fact_decisions
    ADD CONSTRAINT fact_decisions_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES public.fact_decisions(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: fact_dispositions fact_dispositions_predecessor_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_predecessor_fact_id_fkey FOREIGN KEY (predecessor_fact_id) REFERENCES public.facts(id);


--
-- Name: fact_dispositions fact_dispositions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_dispositions fact_dispositions_successor_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_dispositions
    ADD CONSTRAINT fact_dispositions_successor_fact_id_fkey FOREIGN KEY (successor_fact_id) REFERENCES public.facts(id);


--
-- Name: fact_sources fact_sources_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fact_sources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: fact_statement_timings fact_statement_timings_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_statement_timings
    ADD CONSTRAINT fact_statement_timings_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: facts facts_document_value_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_document_value_id_fkey FOREIGN KEY (document_value_id) REFERENCES public.documents(id);


--
-- Name: facts facts_external_org_value_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_external_org_value_id_fkey FOREIGN KEY (external_org_value_id) REFERENCES public.external_orgs(id);


--
-- Name: facts facts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT facts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: retired_automatic_carry_forward_policy_activations fk_active_automatic_carry_forward_policy_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT fk_active_automatic_carry_forward_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);


--
-- Name: active_run_declarations fk_active_run_declarations_predecessor; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT fk_active_run_declarations_predecessor FOREIGN KEY (document_id, predecessor_declaration_id) REFERENCES public.active_run_declarations(document_id, id);


--
-- Name: active_extraction_runs fk_active_run_document_extraction_run; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT fk_active_run_document_extraction_run FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: automatic_carry_forward_outcomes fk_automatic_carry_forward_outcome_policy_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT fk_automatic_carry_forward_outcome_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);


--
-- Name: automatic_carry_forward_outcomes fk_automatic_carry_forward_outcome_run_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT fk_automatic_carry_forward_outcome_run_project FOREIGN KEY (project_id, family, run_id) REFERENCES public.policy_runs(project_id, family, id);


--
-- Name: automatic_carry_forward_receipts fk_automatic_carry_forward_receipt_dependency_evidence; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT fk_automatic_carry_forward_receipt_dependency_evidence FOREIGN KEY (dependency_id, new_evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);


--
-- Name: automatic_carry_forward_receipts fk_automatic_carry_forward_receipt_policy_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT fk_automatic_carry_forward_receipt_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);


--
-- Name: candidates fk_candidates_document_extraction_run; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT fk_candidates_document_extraction_run FOREIGN KEY (source_document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: condition_resolutions fk_condition_resolution_basis_event; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_basis_event FOREIGN KEY (basis_event_id) REFERENCES public.dependency_events(id);


--
-- Name: condition_resolutions fk_condition_resolution_basis_evidence; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_basis_evidence FOREIGN KEY (basis_evidence_link_id) REFERENCES public.evidence_links(id);


--
-- Name: condition_resolutions fk_condition_resolution_owned_evidence; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_owned_evidence FOREIGN KEY (dependency_id, evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);


--
-- Name: dependencies fk_dependencies_external_org_id_external_orgs_id; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_external_org_id_external_orgs_id FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: dependencies fk_dependencies_milestone_id_milestones_id; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_milestone_id_milestones_id FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);


--
-- Name: dependencies fk_dependencies_milestone_registration; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_milestone_registration FOREIGN KEY (milestone_registration_id) REFERENCES public.milestone_registrations(id);


--
-- Name: dependency_admission_outcomes fk_dependency_admission_outcomes_run_family; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT fk_dependency_admission_outcomes_run_family FOREIGN KEY (family, policy_run_id) REFERENCES public.policy_runs(family, id);


--
-- Name: dependency_event_scopes fk_dependency_event_scopes_scope_decision; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT fk_dependency_event_scopes_scope_decision FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);


--
-- Name: dependency_events fk_dependency_events_affected_external_org; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_affected_external_org FOREIGN KEY (affected_external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: dependency_events fk_dependency_events_closes_commitment_lineage; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_closes_commitment_lineage FOREIGN KEY (closes_commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: dependency_events fk_dependency_events_commitment_lineage; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_commitment_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: dependency_events fk_dependency_events_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_project FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: dependency_events fk_dependency_events_stated_external_org; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_stated_external_org FOREIGN KEY (stated_external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: dependency_events fk_dependency_events_supersedes_event; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_supersedes_event FOREIGN KEY (supersedes_event_id) REFERENCES public.dependency_events(id);


--
-- Name: dependency_evidence_sufficiencies fk_dependency_evidence_sufficiencies_scope_link; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT fk_dependency_evidence_sufficiencies_scope_link FOREIGN KEY (scope_link_id) REFERENCES public.dependency_event_scopes(id);


--
-- Name: documentation_field_confirmations fk_documentation_confirmation_owned_evidence; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT fk_documentation_confirmation_owned_evidence FOREIGN KEY (dependency_id, evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);


--
-- Name: documents fk_documents_superseded_by_same_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_superseded_by_same_project FOREIGN KEY (project_id, superseded_by) REFERENCES public.documents(project_id, id);


--
-- Name: documents fk_documents_supersession_source_page; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_supersession_source_page FOREIGN KEY (supersession_source_document_id, supersession_source_page) REFERENCES public.doc_pages(document_id, page_no);


--
-- Name: documents fk_documents_supersession_source_same_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_supersession_source_same_project FOREIGN KEY (project_id, supersession_source_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: event_admission_outcomes fk_event_admission_outcome_audit; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_audit FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: event_admission_outcomes fk_event_admission_outcome_candidate_disposition; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_candidate_disposition FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);


--
-- Name: event_admission_outcomes fk_event_admission_outcome_lineage; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: event_admission_outcomes fk_event_admission_outcome_scope_decision; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_scope_decision FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);


--
-- Name: event_admission_outcomes fk_event_admission_outcomes_run_family; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcomes_run_family FOREIGN KEY (family, policy_run_id) REFERENCES public.policy_runs(family, id);


--
-- Name: external_report_releases fk_external_report_releases_artifact; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT fk_external_report_releases_artifact FOREIGN KEY (artifact_id) REFERENCES public.external_report_artifacts(id);


--
-- Name: extracted_proposal_facts fk_extracted_proposal_fact_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT fk_extracted_proposal_fact_fact_scope FOREIGN KEY (project_id, document_id, extraction_run_id, fact_id) REFERENCES public.facts(project_id, document_id, extraction_run_id, id);


--
-- Name: extracted_proposal_facts fk_extracted_proposal_fact_proposal_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposal_facts
    ADD CONSTRAINT fk_extracted_proposal_fact_proposal_scope FOREIGN KEY (project_id, document_id, extraction_run_id, proposal_id) REFERENCES public.extracted_proposals(project_id, document_id, extraction_run_id, id);


--
-- Name: extracted_proposals fk_extracted_proposal_run_document; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.extracted_proposals
    ADD CONSTRAINT fk_extracted_proposal_run_document FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: fact_applies_to fk_fact_applies_to_dependency_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT fk_fact_applies_to_dependency_scope FOREIGN KEY (project_id, dependency_id) REFERENCES public.dependencies(project_id, id);


--
-- Name: fact_applies_to fk_fact_applies_to_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_applies_to
    ADD CONSTRAINT fk_fact_applies_to_fact_scope FOREIGN KEY (project_id, fact_id) REFERENCES public.facts(project_id, id);


--
-- Name: fact_closure_results fk_fact_closure_result_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results
    ADD CONSTRAINT fk_fact_closure_result_fact_scope FOREIGN KEY (project_id, fact_id) REFERENCES public.facts(project_id, id);


--
-- Name: fact_closure_results fk_fact_closure_result_successor_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_results
    ADD CONSTRAINT fk_fact_closure_result_successor_scope FOREIGN KEY (project_id, successor_dependency_id) REFERENCES public.dependencies(project_id, id);


--
-- Name: fact_closure_sources fk_fact_closure_source_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT fk_fact_closure_source_fact_scope FOREIGN KEY (project_id, document_id, fact_id) REFERENCES public.facts(project_id, document_id, id);


--
-- Name: fact_closure_sources fk_fact_closure_source_segment_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_closure_sources
    ADD CONSTRAINT fk_fact_closure_source_segment_scope FOREIGN KEY (project_id, document_id, source_segment_id) REFERENCES public.source_segments(project_id, document_id, id);


--
-- Name: fact_sources fk_fact_sources_fact_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fk_fact_sources_fact_project FOREIGN KEY (project_id, fact_id) REFERENCES public.facts(project_id, id);


--
-- Name: fact_sources fk_fact_sources_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fk_fact_sources_fact_scope FOREIGN KEY (project_id, document_id, fact_id) REFERENCES public.facts(project_id, document_id, id);


--
-- Name: fact_sources fk_fact_sources_segment_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fk_fact_sources_segment_project FOREIGN KEY (project_id, source_segment_id) REFERENCES public.source_segments(project_id, id);


--
-- Name: fact_sources fk_fact_sources_segment_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_sources
    ADD CONSTRAINT fk_fact_sources_segment_scope FOREIGN KEY (project_id, document_id, source_segment_id) REFERENCES public.source_segments(project_id, document_id, id);


--
-- Name: fact_statement_timings fk_fact_statement_timing_fact_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.fact_statement_timings
    ADD CONSTRAINT fk_fact_statement_timing_fact_scope FOREIGN KEY (project_id, fact_id) REFERENCES public.facts(project_id, id);


--
-- Name: facts fk_facts_document_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT fk_facts_document_scope FOREIGN KEY (project_id, document_id) REFERENCES public.documents(project_id, id);


--
-- Name: facts fk_facts_extraction_run_document; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.facts
    ADD CONSTRAINT fk_facts_extraction_run_document FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: inbound_threads fk_inbound_threads_bound_by_message; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT fk_inbound_threads_bound_by_message FOREIGN KEY (bound_by_message_id) REFERENCES public.inbound_messages(id);


--
-- Name: key_date_draft_receipts fk_key_date_draft_receipts_source_same_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_receipts
    ADD CONSTRAINT fk_key_date_draft_receipts_source_same_project FOREIGN KEY (project_id, source_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: milestones fk_milestones_current_registration; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT fk_milestones_current_registration FOREIGN KEY (current_registration_id) REFERENCES public.milestone_registrations(id);


--
-- Name: operative_support fk_operative_support_evidence_link; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT fk_operative_support_evidence_link FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);


--
-- Name: operative_support fk_operative_support_scope_link; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT fk_operative_support_scope_link FOREIGN KEY (scope_link_id) REFERENCES public.dependency_event_scopes(id);


--
-- Name: policy_runs fk_policy_runs_approval_project_family; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT fk_policy_runs_approval_project_family FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);


--
-- Name: document_rendition_derivations fk_rendition_derivation_derived_same_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT fk_rendition_derivation_derived_same_project FOREIGN KEY (project_id, derived_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: document_rendition_derivations fk_rendition_derivation_source_same_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT fk_rendition_derivation_source_same_project FOREIGN KEY (project_id, source_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: revision_comparison_runs fk_revision_comparison_predecessor_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_predecessor_project FOREIGN KEY (project_id, predecessor_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: revision_comparison_runs fk_revision_comparison_predecessor_run; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_predecessor_run FOREIGN KEY (predecessor_document_id, predecessor_extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: revision_comparison_runs fk_revision_comparison_successor_project; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_successor_project FOREIGN KEY (project_id, successor_document_id) REFERENCES public.documents(project_id, id);


--
-- Name: revision_comparison_runs fk_revision_comparison_successor_run; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_successor_run FOREIGN KEY (successor_document_id, successor_extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: source_fact_append_receipts fk_source_fact_append_run_document; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT fk_source_fact_append_run_document FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);


--
-- Name: source_segments fk_source_segments_document_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT fk_source_segments_document_scope FOREIGN KEY (project_id, document_id) REFERENCES public.documents(project_id, id);


--
-- Name: statement_coordination_receipts fk_statement_coordination_receipt_disposition; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT fk_statement_coordination_receipt_disposition FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);


--
-- Name: subject_candidate_suggestions fk_subject_candidate_suggestion_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT fk_subject_candidate_suggestion_scope FOREIGN KEY (attempt_id, candidate_id) REFERENCES public.subject_resolution_candidates(attempt_id, id);


--
-- Name: subject_resolution_decisions fk_subject_resolution_decision_segment_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT fk_subject_resolution_decision_segment_scope FOREIGN KEY (project_id, source_document_id, source_segment_id) REFERENCES public.source_segments(project_id, document_id, id);


--
-- Name: subject_resolution_attempts fk_subject_resolution_segment_scope; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT fk_subject_resolution_segment_scope FOREIGN KEY (project_id, source_document_id, source_segment_id) REFERENCES public.source_segments(project_id, document_id, id);


--
-- Name: work_decisions fk_work_decisions_commitment_lineage; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_commitment_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: work_decisions fk_work_decisions_commitment_lineage_predecessor; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_commitment_lineage_predecessor FOREIGN KEY (commitment_lineage_id, predecessor_decision_id) REFERENCES public.work_decisions(commitment_lineage_id, id);


--
-- Name: work_decisions fk_work_decisions_predecessor; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_predecessor FOREIGN KEY (dependency_id, predecessor_decision_id) REFERENCES public.work_decisions(dependency_id, id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_internal_owner_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_decision_id_fkey FOREIGN KEY (internal_owner_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_internal_owner_roster_entry_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_roster_entry_id_fkey FOREIGN KEY (internal_owner_roster_entry_id) REFERENCES public.project_roster_entries(id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_next_action_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_next_action_decision_id_fkey FOREIGN KEY (next_action_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_receipts follow_up_plan_receipts_resumed_deferral_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_resumed_deferral_decision_id_fkey FOREIGN KEY (resumed_deferral_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_deferral_reversal_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_deferral_reversal_decision_id_fkey FOREIGN KEY (deferral_reversal_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_internal_owner_reversal_decision__fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_internal_owner_reversal_decision__fkey FOREIGN KEY (internal_owner_reversal_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_next_action_reversal_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_next_action_reversal_decision_id_fkey FOREIGN KEY (next_action_reversal_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: follow_up_plan_reversals follow_up_plan_reversals_receipt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.follow_up_plan_receipts(id);


--
-- Name: inbound_messages inbound_messages_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: inbound_messages inbound_messages_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: inbound_messages inbound_messages_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);


--
-- Name: inbound_route_triage inbound_route_triage_resolved_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_resolved_project_id_fkey FOREIGN KEY (resolved_project_id) REFERENCES public.projects(id);


--
-- Name: inbound_route_triage inbound_route_triage_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);


--
-- Name: inbound_thread_readings inbound_thread_readings_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: inbound_thread_readings inbound_thread_readings_closing_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_closing_message_id_fkey FOREIGN KEY (closing_message_id) REFERENCES public.inbound_messages(id);


--
-- Name: inbound_thread_readings inbound_thread_readings_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);


--
-- Name: inbound_threads inbound_threads_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: inbound_threads inbound_threads_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: intake_project_identifiers intake_project_identifiers_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: key_date_draft_row_receipts key_date_draft_row_receipts_receipt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT key_date_draft_row_receipts_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.key_date_draft_receipts(id);


--
-- Name: legacy_ledger_archives legacy_ledger_archives_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: milestone_registrations milestone_registrations_milestone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);


--
-- Name: milestone_registrations milestone_registrations_predecessor_registration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_predecessor_registration_id_fkey FOREIGN KEY (predecessor_registration_id) REFERENCES public.milestone_registrations(id);


--
-- Name: milestones milestones_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: operative_support operative_support_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT operative_support_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: organization_identity_activations organization_identity_activations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_activations
    ADD CONSTRAINT organization_identity_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: organization_identity_receipts organization_identity_receipts_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: organization_identity_receipts organization_identity_receipts_external_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_external_org_id_fkey FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: organization_identity_receipts organization_identity_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: page_processing_failures page_processing_failures_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_processing_failures
    ADD CONSTRAINT page_processing_failures_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: page_render_derivatives page_render_derivatives_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.page_render_derivatives
    ADD CONSTRAINT page_render_derivatives_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: policy_approvals policy_approvals_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT policy_approvals_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: policy_runs policy_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: processing_artifacts processing_artifacts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.processing_artifacts
    ADD CONSTRAINT processing_artifacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: production_run_explanation_configurations production_run_explanation_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_configurations
    ADD CONSTRAINT production_run_explanation_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: production_run_explanation_requests production_run_explanation_requests_configuration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.production_run_explanation_configurations(id);


--
-- Name: production_run_explanation_requests production_run_explanation_requests_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: production_run_explanation_requests production_run_explanation_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: project_check_configurations project_check_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_check_configurations
    ADD CONSTRAINT project_check_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: project_record_revisions project_record_revisions_predecessor_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.project_record_revisions
    ADD CONSTRAINT project_record_revisions_predecessor_revision_id_fkey FOREIGN KEY (predecessor_revision_id) REFERENCES public.project_record_revisions(id);


--
-- Name: project_record_revisions project_record_revisions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.project_record_revisions
    ADD CONSTRAINT project_record_revisions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: project_roster_entries project_roster_entries_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT project_roster_entries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: reconfirmation_receipts reconfirmation_receipts_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id) ON DELETE CASCADE;


--
-- Name: reconfirmation_receipts reconfirmation_receipts_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: reconfirmation_receipts reconfirmation_receipts_successor_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);


--
-- Name: record_inclusion_requests record_inclusion_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.record_inclusion_requests
    ADD CONSTRAINT record_inclusion_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: report_runs report_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: retention_holds retention_holds_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_holds
    ADD CONSTRAINT retention_holds_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: retention_manifest_items retention_manifest_items_manifest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifest_items
    ADD CONSTRAINT retention_manifest_items_manifest_id_fkey FOREIGN KEY (manifest_id) REFERENCES public.retention_manifests(id);


--
-- Name: retention_manifest_items retention_manifest_items_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_manifest_items
    ADD CONSTRAINT retention_manifest_items_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: retention_references retention_references_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retention_references
    ADD CONSTRAINT retention_references_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: retired_dependency_statuses retired_dependency_statuses_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.retired_dependency_statuses
    ADD CONSTRAINT retired_dependency_statuses_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: revision_change_explanation_configurations revision_change_explanation_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_configurations
    ADD CONSTRAINT revision_change_explanation_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_comparison_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_configuration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.revision_change_explanation_configurations(id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_finding_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);


--
-- Name: revision_change_explanation_requests revision_change_explanation_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: revision_comparison_findings revision_comparison_findings_revision_comparison_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_revision_comparison_run_id_fkey FOREIGN KEY (revision_comparison_run_id) REFERENCES public.revision_comparison_runs(id);


--
-- Name: revision_comparison_runs revision_comparison_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT revision_comparison_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: revision_reconciliation_requests revision_reconciliation_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.revision_reconciliation_requests
    ADD CONSTRAINT revision_reconciliation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: schedule_governing_derivations schedule_governing_derivations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_governing_derivations
    ADD CONSTRAINT schedule_governing_derivations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: schedule_link_activations schedule_link_activations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_activations
    ADD CONSTRAINT schedule_link_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: schedule_link_receipts schedule_link_receipts_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: schedule_link_receipts schedule_link_receipts_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: schedule_link_receipts schedule_link_receipts_milestone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);


--
-- Name: schedule_link_receipts schedule_link_receipts_milestone_registration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_milestone_registration_id_fkey FOREIGN KEY (milestone_registration_id) REFERENCES public.milestone_registrations(id);


--
-- Name: schedule_link_receipts schedule_link_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: scheduled_report_publications scheduled_report_publications_occurrence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_occurrence_id_fkey FOREIGN KEY (occurrence_id) REFERENCES public.due_work_occurrences(id);


--
-- Name: scheduled_report_publications scheduled_report_publications_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: scheduled_report_publications scheduled_report_publications_schedule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.due_work_schedules(id);


--
-- Name: source_fact_append_receipts source_fact_append_receipts_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT source_fact_append_receipts_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: source_fact_append_receipts source_fact_append_receipts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fact_append_receipts
    ADD CONSTRAINT source_fact_append_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: source_fetch_attempts source_fetch_attempts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_fetch_attempts
    ADD CONSTRAINT source_fetch_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: source_intake_draft_configurations source_intake_draft_configurations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_configurations
    ADD CONSTRAINT source_intake_draft_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: source_intake_draft_requests source_intake_draft_requests_configuration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.source_intake_draft_configurations(id);


--
-- Name: source_intake_draft_requests source_intake_draft_requests_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: source_segments source_segments_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT source_segments_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: source_segments source_segments_statement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.source_segments
    ADD CONSTRAINT source_segments_statement_id_fkey FOREIGN KEY (statement_id) REFERENCES public.dependency_events(id);


--
-- Name: stated_by_people stated_by_people_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.stated_by_people
    ADD CONSTRAINT stated_by_people_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: statement_coordination_receipts statement_coordination_receip_internal_owner_roster_entry__fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receip_internal_owner_roster_entry__fkey FOREIGN KEY (internal_owner_roster_entry_id) REFERENCES public.project_roster_entries(id);


--
-- Name: statement_coordination_receipts statement_coordination_receip_milestone_impact_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receip_milestone_impact_decision_id_fkey FOREIGN KEY (milestone_impact_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_commitment_lineage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_dependency_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_dependency_event_id_fkey FOREIGN KEY (dependency_event_id) REFERENCES public.dependency_events(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_internal_owner_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_internal_owner_decision_id_fkey FOREIGN KEY (internal_owner_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_next_action_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_next_action_decision_id_fkey FOREIGN KEY (next_action_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: statement_coordination_receipts statement_coordination_receipts_scope_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_scope_decision_id_fkey FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);


--
-- Name: statement_coordination_reversal_effects statement_coordination_reversal_effects_reversal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT statement_coordination_reversal_effects_reversal_id_fkey FOREIGN KEY (reversal_id) REFERENCES public.statement_coordination_reversals(id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_audit_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_candidate_disposition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_disposition_id_fkey FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: statement_coordination_reversals statement_coordination_reversals_receipt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.statement_coordination_receipts(id);


--
-- Name: statement_suggestion_eligibility_declarations statement_suggestion_eligibility_declarations_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: statement_suggestion_eligibility_declarations statement_suggestion_eligibility_declarations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: statement_suggestion_protection_ends statement_suggestion_protection_ends_protection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT statement_suggestion_protection_ends_protection_id_fkey FOREIGN KEY (protection_id) REFERENCES public.statement_suggestion_protections(id);


--
-- Name: statement_suggestion_protections statement_suggestion_protections_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);


--
-- Name: statement_suggestion_protections statement_suggestion_protections_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: subject_candidate_suggestions subject_candidate_suggestions_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT subject_candidate_suggestions_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.subject_resolution_attempts(id);


--
-- Name: subject_candidate_suggestions subject_candidate_suggestions_candidate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_candidate_suggestions
    ADD CONSTRAINT subject_candidate_suggestions_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.subject_resolution_candidates(id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_resolved_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_resolved_dependency_id_fkey FOREIGN KEY (resolved_dependency_id) REFERENCES public.dependencies(id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_resolved_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_resolved_document_id_fkey FOREIGN KEY (resolved_document_id) REFERENCES public.documents(id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_resolved_external_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_resolved_external_org_id_fkey FOREIGN KEY (resolved_external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: subject_resolution_attempts subject_resolution_attempts_resolved_stated_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_attempts
    ADD CONSTRAINT subject_resolution_attempts_resolved_stated_by_person_id_fkey FOREIGN KEY (resolved_stated_by_person_id) REFERENCES public.stated_by_people(id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.subject_resolution_attempts(id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_external_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_external_org_id_fkey FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: subject_resolution_candidates subject_resolution_candidates_stated_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.subject_resolution_candidates
    ADD CONSTRAINT subject_resolution_candidates_stated_by_person_id_fkey FOREIGN KEY (stated_by_person_id) REFERENCES public.stated_by_people(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.subject_resolution_attempts(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_external_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_external_org_id_fkey FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_revision_id_fkey FOREIGN KEY (revision_id) REFERENCES public.project_record_revisions(id);


--
-- Name: subject_resolution_decisions subject_resolution_decisions_stated_by_person_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor_fact_decision_writer
--

ALTER TABLE ONLY public.subject_resolution_decisions
    ADD CONSTRAINT subject_resolution_decisions_stated_by_person_id_fkey FOREIGN KEY (stated_by_person_id) REFERENCES public.stated_by_people(id);


--
-- Name: token_layers token_layers_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.token_layers
    ADD CONSTRAINT token_layers_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: unreadable_cell_admission_activations unreadable_cell_admission_activations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_admission_activations
    ADD CONSTRAINT unreadable_cell_admission_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: unreadable_cell_reading_profiles unreadable_cell_reading_profiles_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_profiles
    ADD CONSTRAINT unreadable_cell_reading_profiles_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.unreadable_cell_reading_profiles(id);


--
-- Name: unreadable_cell_reading_runs unreadable_cell_reading_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: unreadable_cell_reading_steps unreadable_cell_reading_steps_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT unreadable_cell_reading_steps_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.unreadable_cell_reading_runs(id);


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_corroboration_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_corroboration_document_id_fkey FOREIGN KEY (corroboration_document_id) REFERENCES public.documents(id);


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: unreadable_cell_resolutions unreadable_cell_resolutions_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.unreadable_cell_reading_runs(id);


--
-- Name: work_decision_milestone_impacts work_decision_milestone_impacts_milestone_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);


--
-- Name: work_decision_milestone_impacts work_decision_milestone_impacts_work_decision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_work_decision_id_fkey FOREIGN KEY (work_decision_id) REFERENCES public.work_decisions(id);


--
-- Name: work_decisions work_decisions_dependency_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: corridor
--

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT work_decisions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT USAGE ON SCHEMA public TO corridor_statement_retirement;
GRANT USAGE ON SCHEMA public TO corridor_web;
GRANT USAGE ON SCHEMA public TO corridor_worker;


--
-- Name: FUNCTION include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying); Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

REVOKE ALL ON FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) FROM PUBLIC;
GRANT ALL ON FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) TO corridor_worker;


--
-- Name: FUNCTION purge_external_party_statement_rows(target_project_id bigint, target_purpose text); Type: ACL; Schema: public; Owner: corridor_statement_retirement
--

REVOKE ALL ON FUNCTION public.purge_external_party_statement_rows(target_project_id bigint, target_purpose text) FROM PUBLIC;


--
-- Name: FUNCTION record_human_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_command_type character varying, p_disposition character varying, p_human_principal text, p_idempotency_key character varying, p_expected_predecessor bigint); Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

REVOKE ALL ON FUNCTION public.record_human_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_command_type character varying, p_disposition character varying, p_human_principal text, p_idempotency_key character varying, p_expected_predecessor bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.record_human_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_command_type character varying, p_disposition character varying, p_human_principal text, p_idempotency_key character varying, p_expected_predecessor bigint) TO corridor_web;


--
-- Name: FUNCTION record_subject_alias_decision(p_project_id bigint, p_attempt_id bigint, p_subject_type character varying, p_subject_id bigint, p_human_principal character varying, p_idempotency_key character varying); Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

REVOKE ALL ON FUNCTION public.record_subject_alias_decision(p_project_id bigint, p_attempt_id bigint, p_subject_type character varying, p_subject_id bigint, p_human_principal character varying, p_idempotency_key character varying) FROM PUBLIC;
GRANT ALL ON FUNCTION public.record_subject_alias_decision(p_project_id bigint, p_attempt_id bigint, p_subject_type character varying, p_subject_id bigint, p_human_principal character varying, p_idempotency_key character varying) TO corridor_web;


--
-- Name: TABLE active_extraction_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.active_extraction_runs TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.active_extraction_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.active_extraction_runs TO corridor_worker;


--
-- Name: TABLE active_run_declarations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.active_run_declarations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.active_run_declarations TO corridor_worker;


--
-- Name: SEQUENCE active_run_declarations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.active_run_declarations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.active_run_declarations_id_seq TO corridor_worker;


--
-- Name: TABLE assertions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assertions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assertions TO corridor_worker;


--
-- Name: SEQUENCE assertions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.assertions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.assertions_id_seq TO corridor_worker;


--
-- Name: TABLE assignment_notification_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_attempts TO corridor_worker;


--
-- Name: SEQUENCE assignment_notification_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE assignment_notification_dispatches; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_dispatches TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_dispatches TO corridor_worker;


--
-- Name: SEQUENCE assignment_notification_dispatches_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_dispatches_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_dispatches_id_seq TO corridor_worker;


--
-- Name: TABLE assignment_notification_feedback; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_feedback TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notification_feedback TO corridor_worker;


--
-- Name: SEQUENCE assignment_notification_feedback_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_feedback_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.assignment_notification_feedback_id_seq TO corridor_worker;


--
-- Name: TABLE assignment_notifications; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notifications TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assignment_notifications TO corridor_worker;


--
-- Name: SEQUENCE assignment_notifications_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.assignment_notifications_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.assignment_notifications_id_seq TO corridor_worker;


--
-- Name: TABLE audit_log; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_log TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_log TO corridor_worker;


--
-- Name: SEQUENCE audit_log_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.audit_log_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.audit_log_id_seq TO corridor_worker;


--
-- Name: TABLE automatic_carry_forward_outcomes; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.automatic_carry_forward_outcomes TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.automatic_carry_forward_outcomes TO corridor_worker;


--
-- Name: SEQUENCE automatic_carry_forward_outcomes_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.automatic_carry_forward_outcomes_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.automatic_carry_forward_outcomes_id_seq TO corridor_worker;


--
-- Name: TABLE automatic_carry_forward_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.automatic_carry_forward_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.automatic_carry_forward_receipts TO corridor_worker;


--
-- Name: TABLE candidate_dispositions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_dispositions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidate_dispositions TO corridor_worker;


--
-- Name: SEQUENCE candidate_dispositions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.candidate_dispositions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.candidate_dispositions_id_seq TO corridor_worker;


--
-- Name: TABLE candidates; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.candidates TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidates TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.candidates TO corridor_worker;


--
-- Name: SEQUENCE candidates_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.candidates_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.candidates_id_seq TO corridor_worker;


--
-- Name: TABLE cohort_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cohort_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.cohort_receipts TO corridor_worker;


--
-- Name: SEQUENCE cohort_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.cohort_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.cohort_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE commitment_lineages; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.commitment_lineages TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.commitment_lineages TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.commitment_lineages TO corridor_worker;


--
-- Name: SEQUENCE commitment_lineages_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.commitment_lineages_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.commitment_lineages_id_seq TO corridor_worker;


--
-- Name: TABLE condition_resolutions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.condition_resolutions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.condition_resolutions TO corridor_worker;


--
-- Name: SEQUENCE condition_resolutions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.condition_resolutions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.condition_resolutions_id_seq TO corridor_worker;


--
-- Name: TABLE coordination_summary_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.coordination_summary_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.coordination_summary_configurations TO corridor_worker;


--
-- Name: SEQUENCE coordination_summary_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.coordination_summary_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.coordination_summary_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE coordination_summary_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.coordination_summary_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.coordination_summary_requests TO corridor_worker;


--
-- Name: SEQUENCE coordination_summary_requests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.coordination_summary_requests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.coordination_summary_requests_id_seq TO corridor_worker;


--
-- Name: TABLE extracted_proposals; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.extracted_proposals TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extracted_proposals TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extracted_proposals TO corridor_worker;


--
-- Name: TABLE fact_decisions; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT ON TABLE public.fact_decisions TO PUBLIC;
GRANT SELECT ON TABLE public.fact_decisions TO corridor_web;
GRANT SELECT ON TABLE public.fact_decisions TO corridor_worker;


--
-- Name: TABLE facts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.facts TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.facts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.facts TO corridor_worker;


--
-- Name: TABLE current_project_record; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.current_project_record TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.current_project_record TO corridor_worker;


--
-- Name: TABLE dependencies; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.dependencies TO corridor_fact_decision_writer;
GRANT SELECT ON TABLE public.dependencies TO corridor_web;
GRANT SELECT ON TABLE public.dependencies TO corridor_worker;


--
-- Name: SEQUENCE dependencies_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependencies_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependencies_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_admission_outcomes; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_admission_outcomes TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_admission_outcomes TO corridor_worker;


--
-- Name: SEQUENCE dependency_admission_outcomes_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_admission_outcomes_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_admission_outcomes_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_dismissals; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_dismissals TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_dismissals TO corridor_worker;


--
-- Name: SEQUENCE dependency_dismissals_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_dismissals_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_dismissals_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_event_evidence; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.dependency_event_evidence TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_evidence TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_evidence TO corridor_worker;


--
-- Name: TABLE dependency_event_migration_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_migration_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_migration_receipts TO corridor_worker;


--
-- Name: TABLE dependency_event_scope_decisions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.dependency_event_scope_decisions TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_scope_decisions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_scope_decisions TO corridor_worker;


--
-- Name: SEQUENCE dependency_event_scope_decisions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_scope_decisions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_scope_decisions_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_event_scopes; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.dependency_event_scopes TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_scopes TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_scopes TO corridor_worker;


--
-- Name: SEQUENCE dependency_event_scopes_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_scopes_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_scopes_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_event_timings; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.dependency_event_timings TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_timings TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_event_timings TO corridor_worker;


--
-- Name: SEQUENCE dependency_event_timings_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_timings_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_event_timings_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_events; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.dependency_events TO corridor_statement_retirement;
GRANT SELECT ON TABLE public.dependency_events TO corridor_web;
GRANT SELECT ON TABLE public.dependency_events TO corridor_worker;


--
-- Name: SEQUENCE dependency_events_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_events_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_events_id_seq TO corridor_worker;


--
-- Name: TABLE dependency_evidence_sufficiencies; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_evidence_sufficiencies TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dependency_evidence_sufficiencies TO corridor_worker;


--
-- Name: SEQUENCE dependency_evidence_sufficiencies_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dependency_evidence_sufficiencies_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dependency_evidence_sufficiencies_id_seq TO corridor_worker;


--
-- Name: TABLE discovered_references; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.discovered_references TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.discovered_references TO corridor_worker;


--
-- Name: SEQUENCE discovered_references_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.discovered_references_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.discovered_references_id_seq TO corridor_worker;


--
-- Name: TABLE dispute_history_resolutions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.dispute_history_resolutions TO corridor_web;
GRANT SELECT ON TABLE public.dispute_history_resolutions TO corridor_worker;


--
-- Name: SEQUENCE dispute_history_resolutions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dispute_history_resolutions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dispute_history_resolutions_id_seq TO corridor_worker;


--
-- Name: TABLE dispute_settlements; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.dispute_settlements TO corridor_web;
GRANT SELECT ON TABLE public.dispute_settlements TO corridor_worker;


--
-- Name: SEQUENCE dispute_settlements_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.dispute_settlements_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.dispute_settlements_id_seq TO corridor_worker;


--
-- Name: TABLE doc_pages; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.doc_pages TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.doc_pages TO corridor_worker;


--
-- Name: SEQUENCE doc_pages_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.doc_pages_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.doc_pages_id_seq TO corridor_worker;


--
-- Name: TABLE document_notification_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notification_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notification_attempts TO corridor_worker;


--
-- Name: SEQUENCE document_notification_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.document_notification_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.document_notification_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE document_notification_dispatches; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notification_dispatches TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notification_dispatches TO corridor_worker;


--
-- Name: SEQUENCE document_notification_dispatches_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.document_notification_dispatches_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.document_notification_dispatches_id_seq TO corridor_worker;


--
-- Name: TABLE document_notifications; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notifications TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_notifications TO corridor_worker;


--
-- Name: SEQUENCE document_notifications_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.document_notifications_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.document_notifications_id_seq TO corridor_worker;


--
-- Name: TABLE document_quarantines; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_quarantines TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_quarantines TO corridor_worker;


--
-- Name: TABLE document_rendition_derivations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_rendition_derivations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.document_rendition_derivations TO corridor_worker;


--
-- Name: SEQUENCE document_rendition_derivations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.document_rendition_derivations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.document_rendition_derivations_id_seq TO corridor_worker;


--
-- Name: TABLE documentation_field_confirmations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documentation_field_confirmations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documentation_field_confirmations TO corridor_worker;


--
-- Name: SEQUENCE documentation_field_confirmations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.documentation_field_confirmations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.documentation_field_confirmations_id_seq TO corridor_worker;


--
-- Name: TABLE documents; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.documents TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documents TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documents TO corridor_worker;


--
-- Name: SEQUENCE documents_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.documents_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.documents_id_seq TO corridor_worker;


--
-- Name: TABLE due_action_notification_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notification_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notification_attempts TO corridor_worker;


--
-- Name: SEQUENCE due_action_notification_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_action_notification_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_action_notification_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE due_action_notification_dispatches; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notification_dispatches TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notification_dispatches TO corridor_worker;


--
-- Name: SEQUENCE due_action_notification_dispatches_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_action_notification_dispatches_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_action_notification_dispatches_id_seq TO corridor_worker;


--
-- Name: TABLE due_action_notifications; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notifications TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_action_notifications TO corridor_worker;


--
-- Name: SEQUENCE due_action_notifications_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_action_notifications_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_action_notifications_id_seq TO corridor_worker;


--
-- Name: TABLE due_work_occurrences; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_occurrences TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_occurrences TO corridor_worker;


--
-- Name: SEQUENCE due_work_occurrences_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_work_occurrences_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_work_occurrences_id_seq TO corridor_worker;


--
-- Name: TABLE due_work_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_receipts TO corridor_worker;


--
-- Name: SEQUENCE due_work_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_work_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_work_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE due_work_schedules; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_schedules TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.due_work_schedules TO corridor_worker;


--
-- Name: SEQUENCE due_work_schedules_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.due_work_schedules_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.due_work_schedules_id_seq TO corridor_worker;


--
-- Name: TABLE event_admission_acceptance_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_acceptance_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_acceptance_receipts TO corridor_worker;


--
-- Name: SEQUENCE event_admission_acceptance_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.event_admission_acceptance_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.event_admission_acceptance_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE event_admission_activations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_activations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_activations TO corridor_worker;


--
-- Name: SEQUENCE event_admission_activations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.event_admission_activations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.event_admission_activations_id_seq TO corridor_worker;


--
-- Name: TABLE event_admission_outcomes; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_outcomes TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_admission_outcomes TO corridor_worker;


--
-- Name: SEQUENCE event_admission_outcomes_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.event_admission_outcomes_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.event_admission_outcomes_id_seq TO corridor_worker;


--
-- Name: TABLE event_cohort_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_cohort_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_cohort_receipts TO corridor_worker;


--
-- Name: SEQUENCE event_cohort_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.event_cohort_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.event_cohort_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_candidate_review_starts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_candidate_review_starts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_candidate_review_starts TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_candidate_review_starts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_capture_contracts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_capture_contracts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_capture_contracts TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_capture_contracts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_capture_contracts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_capture_contracts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_capture_results; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_capture_results TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_capture_results TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_capture_results_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_capture_results_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_capture_results_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_evaluation_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_evaluation_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_evaluation_receipts TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_evaluation_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_packet_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_packet_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_packet_receipts TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_packet_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_packet_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_packet_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_review_observations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_review_observations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_review_observations TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_review_observations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_review_observations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_review_observations_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_runs TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_runs_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_shadow_cases; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_cases TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_cases TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_shadow_cases_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_cases_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_cases_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_shadow_executions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_executions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_executions TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_shadow_executions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_executions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_executions_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_shadow_outcomes; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_outcomes TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_shadow_outcomes TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_shadow_outcomes_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_investigation_step_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_step_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_investigation_step_receipts TO corridor_worker;


--
-- Name: SEQUENCE evidence_investigation_step_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_step_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_investigation_step_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE evidence_links; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.evidence_links TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_links TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.evidence_links TO corridor_worker;


--
-- Name: SEQUENCE evidence_links_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.evidence_links_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.evidence_links_id_seq TO corridor_worker;


--
-- Name: TABLE external_orgs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.external_orgs TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_orgs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_orgs TO corridor_worker;


--
-- Name: SEQUENCE external_orgs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.external_orgs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.external_orgs_id_seq TO corridor_worker;


--
-- Name: TABLE external_report_artifacts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_report_artifacts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_report_artifacts TO corridor_worker;


--
-- Name: SEQUENCE external_report_artifacts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.external_report_artifacts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.external_report_artifacts_id_seq TO corridor_worker;


--
-- Name: TABLE external_report_releases; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_report_releases TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.external_report_releases TO corridor_worker;


--
-- Name: SEQUENCE external_report_releases_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.external_report_releases_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.external_report_releases_id_seq TO corridor_worker;


--
-- Name: TABLE extracted_proposal_facts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.extracted_proposal_facts TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extracted_proposal_facts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extracted_proposal_facts TO corridor_worker;


--
-- Name: SEQUENCE extracted_proposal_facts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extracted_proposal_facts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extracted_proposal_facts_id_seq TO corridor_worker;


--
-- Name: SEQUENCE extracted_proposals_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extracted_proposals_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extracted_proposals_id_seq TO corridor_worker;


--
-- Name: TABLE extraction_failure_diagnosis_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_failure_diagnosis_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_failure_diagnosis_configurations TO corridor_worker;


--
-- Name: SEQUENCE extraction_failure_diagnosis_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE extraction_failure_diagnosis_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_failure_diagnosis_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_failure_diagnosis_requests TO corridor_worker;


--
-- Name: SEQUENCE extraction_failure_diagnosis_requests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extraction_failure_diagnosis_requests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extraction_failure_diagnosis_requests_id_seq TO corridor_worker;


--
-- Name: TABLE extraction_measurement_case_states; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_measurement_case_states TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_measurement_case_states TO corridor_worker;


--
-- Name: SEQUENCE extraction_measurement_case_states_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extraction_measurement_case_states_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extraction_measurement_case_states_id_seq TO corridor_worker;


--
-- Name: TABLE extraction_run_candidates; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_run_candidates TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_run_candidates TO corridor_worker;


--
-- Name: SEQUENCE extraction_run_candidates_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extraction_run_candidates_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extraction_run_candidates_id_seq TO corridor_worker;


--
-- Name: TABLE extraction_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.extraction_runs TO corridor_worker;


--
-- Name: SEQUENCE extraction_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.extraction_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.extraction_runs_id_seq TO corridor_worker;


--
-- Name: TABLE fact_applies_to; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.fact_applies_to TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_applies_to TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_applies_to TO corridor_worker;


--
-- Name: SEQUENCE fact_applies_to_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_applies_to_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_applies_to_id_seq TO corridor_worker;


--
-- Name: TABLE fact_closure_results; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.fact_closure_results TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_closure_results TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_closure_results TO corridor_worker;


--
-- Name: SEQUENCE fact_closure_results_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_closure_results_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_closure_results_id_seq TO corridor_worker;


--
-- Name: TABLE fact_closure_sources; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.fact_closure_sources TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_closure_sources TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_closure_sources TO corridor_worker;


--
-- Name: SEQUENCE fact_closure_sources_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_closure_sources_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_closure_sources_id_seq TO corridor_worker;


--
-- Name: SEQUENCE fact_decisions_id_seq; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_decisions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_decisions_id_seq TO corridor_worker;


--
-- Name: TABLE fact_dispositions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_dispositions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_dispositions TO corridor_worker;


--
-- Name: SEQUENCE fact_dispositions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_dispositions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_dispositions_id_seq TO corridor_worker;


--
-- Name: TABLE fact_sources; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.fact_sources TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_sources TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_sources TO corridor_worker;


--
-- Name: SEQUENCE fact_sources_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_sources_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_sources_id_seq TO corridor_worker;


--
-- Name: TABLE fact_statement_timings; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_statement_timings TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.fact_statement_timings TO corridor_worker;


--
-- Name: SEQUENCE fact_statement_timings_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.fact_statement_timings_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.fact_statement_timings_id_seq TO corridor_worker;


--
-- Name: SEQUENCE facts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.facts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.facts_id_seq TO corridor_worker;


--
-- Name: TABLE follow_up_plan_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.follow_up_plan_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.follow_up_plan_receipts TO corridor_worker;


--
-- Name: SEQUENCE follow_up_plan_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.follow_up_plan_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.follow_up_plan_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE follow_up_plan_reversals; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.follow_up_plan_reversals TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.follow_up_plan_reversals TO corridor_worker;


--
-- Name: SEQUENCE follow_up_plan_reversals_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.follow_up_plan_reversals_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.follow_up_plan_reversals_id_seq TO corridor_worker;


--
-- Name: TABLE inbound_messages; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_messages TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_messages TO corridor_worker;


--
-- Name: SEQUENCE inbound_messages_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.inbound_messages_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.inbound_messages_id_seq TO corridor_worker;


--
-- Name: TABLE inbound_route_triage; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_route_triage TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_route_triage TO corridor_worker;


--
-- Name: SEQUENCE inbound_route_triage_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.inbound_route_triage_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.inbound_route_triage_id_seq TO corridor_worker;


--
-- Name: TABLE inbound_thread_readings; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_thread_readings TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_thread_readings TO corridor_worker;


--
-- Name: SEQUENCE inbound_thread_readings_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.inbound_thread_readings_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.inbound_thread_readings_id_seq TO corridor_worker;


--
-- Name: TABLE inbound_threads; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_threads TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.inbound_threads TO corridor_worker;


--
-- Name: SEQUENCE inbound_threads_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.inbound_threads_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.inbound_threads_id_seq TO corridor_worker;


--
-- Name: TABLE intake_project_identifiers; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.intake_project_identifiers TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.intake_project_identifiers TO corridor_worker;


--
-- Name: SEQUENCE intake_project_identifiers_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.intake_project_identifiers_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.intake_project_identifiers_id_seq TO corridor_worker;


--
-- Name: TABLE key_date_draft_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.key_date_draft_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.key_date_draft_receipts TO corridor_worker;


--
-- Name: SEQUENCE key_date_draft_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.key_date_draft_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.key_date_draft_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE key_date_draft_row_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.key_date_draft_row_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.key_date_draft_row_receipts TO corridor_worker;


--
-- Name: SEQUENCE key_date_draft_row_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.key_date_draft_row_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.key_date_draft_row_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE legacy_ledger_archives; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.legacy_ledger_archives TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.legacy_ledger_archives TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.legacy_ledger_archives TO corridor_worker;


--
-- Name: SEQUENCE legacy_ledger_archives_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.legacy_ledger_archives_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.legacy_ledger_archives_id_seq TO corridor_worker;


--
-- Name: TABLE milestone_registrations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.milestone_registrations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.milestone_registrations TO corridor_worker;


--
-- Name: SEQUENCE milestone_registrations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.milestone_registrations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.milestone_registrations_id_seq TO corridor_worker;


--
-- Name: TABLE milestones; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.milestones TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.milestones TO corridor_worker;


--
-- Name: SEQUENCE milestones_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.milestones_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.milestones_id_seq TO corridor_worker;


--
-- Name: TABLE operative_support; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.operative_support TO corridor_web;
GRANT SELECT ON TABLE public.operative_support TO corridor_worker;


--
-- Name: SEQUENCE operative_support_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.operative_support_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.operative_support_id_seq TO corridor_worker;


--
-- Name: TABLE organization_identity_activations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organization_identity_activations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organization_identity_activations TO corridor_worker;


--
-- Name: SEQUENCE organization_identity_activations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.organization_identity_activations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.organization_identity_activations_id_seq TO corridor_worker;


--
-- Name: TABLE organization_identity_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organization_identity_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organization_identity_receipts TO corridor_worker;


--
-- Name: SEQUENCE organization_identity_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.organization_identity_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.organization_identity_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE page_processing_failures; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.page_processing_failures TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.page_processing_failures TO corridor_worker;


--
-- Name: SEQUENCE page_processing_failures_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.page_processing_failures_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.page_processing_failures_id_seq TO corridor_worker;


--
-- Name: TABLE page_render_derivatives; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.page_render_derivatives TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.page_render_derivatives TO corridor_worker;


--
-- Name: SEQUENCE page_render_derivatives_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.page_render_derivatives_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.page_render_derivatives_id_seq TO corridor_worker;


--
-- Name: TABLE person_identities; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.person_identities TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.person_identities TO corridor_worker;


--
-- Name: SEQUENCE person_identities_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.person_identities_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.person_identities_id_seq TO corridor_worker;


--
-- Name: TABLE policy_approvals; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_approvals TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_approvals TO corridor_worker;


--
-- Name: SEQUENCE policy_approvals_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.policy_approvals_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.policy_approvals_id_seq TO corridor_worker;


--
-- Name: TABLE policy_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.policy_runs TO corridor_worker;


--
-- Name: SEQUENCE policy_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.policy_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.policy_runs_id_seq TO corridor_worker;


--
-- Name: TABLE processing_artifacts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.processing_artifacts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.processing_artifacts TO corridor_worker;


--
-- Name: SEQUENCE processing_artifacts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.processing_artifacts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.processing_artifacts_id_seq TO corridor_worker;


--
-- Name: TABLE production_run_explanation_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.production_run_explanation_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.production_run_explanation_configurations TO corridor_worker;


--
-- Name: SEQUENCE production_run_explanation_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.production_run_explanation_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.production_run_explanation_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE production_run_explanation_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.production_run_explanation_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.production_run_explanation_requests TO corridor_worker;


--
-- Name: SEQUENCE production_run_explanation_requests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.production_run_explanation_requests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.production_run_explanation_requests_id_seq TO corridor_worker;


--
-- Name: TABLE project_check_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_check_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_check_configurations TO corridor_worker;


--
-- Name: SEQUENCE project_check_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.project_check_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.project_check_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE project_record_revisions; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT ON TABLE public.project_record_revisions TO PUBLIC;
GRANT SELECT ON TABLE public.project_record_revisions TO corridor_web;
GRANT SELECT ON TABLE public.project_record_revisions TO corridor_worker;


--
-- Name: SEQUENCE project_record_revisions_id_seq; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT,USAGE ON SEQUENCE public.project_record_revisions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.project_record_revisions_id_seq TO corridor_worker;


--
-- Name: TABLE project_roster_entries; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_roster_entries TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_roster_entries TO corridor_worker;


--
-- Name: SEQUENCE project_roster_entries_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.project_roster_entries_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.project_roster_entries_id_seq TO corridor_worker;


--
-- Name: TABLE projects; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,DELETE ON TABLE public.projects TO corridor_statement_retirement;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.projects TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.projects TO corridor_worker;


--
-- Name: SEQUENCE projects_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.projects_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.projects_id_seq TO corridor_worker;


--
-- Name: TABLE reconfirmation_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.reconfirmation_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.reconfirmation_receipts TO corridor_worker;


--
-- Name: TABLE record_inclusion_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.record_inclusion_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.record_inclusion_requests TO corridor_worker;


--
-- Name: TABLE report_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.report_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.report_runs TO corridor_worker;


--
-- Name: SEQUENCE report_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.report_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.report_runs_id_seq TO corridor_worker;


--
-- Name: TABLE retention_holds; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_holds TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_holds TO corridor_worker;


--
-- Name: SEQUENCE retention_holds_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.retention_holds_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.retention_holds_id_seq TO corridor_worker;


--
-- Name: TABLE retention_manifest_items; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_manifest_items TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_manifest_items TO corridor_worker;


--
-- Name: SEQUENCE retention_manifest_items_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.retention_manifest_items_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.retention_manifest_items_id_seq TO corridor_worker;


--
-- Name: TABLE retention_manifests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_manifests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_manifests TO corridor_worker;


--
-- Name: SEQUENCE retention_manifests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.retention_manifests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.retention_manifests_id_seq TO corridor_worker;


--
-- Name: TABLE retention_references; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_references TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retention_references TO corridor_worker;


--
-- Name: SEQUENCE retention_references_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.retention_references_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.retention_references_id_seq TO corridor_worker;


--
-- Name: TABLE retired_automatic_carry_forward_policy_activations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retired_automatic_carry_forward_policy_activations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retired_automatic_carry_forward_policy_activations TO corridor_worker;


--
-- Name: TABLE retired_dependency_statuses; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retired_dependency_statuses TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.retired_dependency_statuses TO corridor_worker;


--
-- Name: TABLE revision_change_explanation_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_change_explanation_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_change_explanation_configurations TO corridor_worker;


--
-- Name: SEQUENCE revision_change_explanation_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.revision_change_explanation_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.revision_change_explanation_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE revision_change_explanation_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_change_explanation_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_change_explanation_requests TO corridor_worker;


--
-- Name: SEQUENCE revision_change_explanation_requests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.revision_change_explanation_requests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.revision_change_explanation_requests_id_seq TO corridor_worker;


--
-- Name: TABLE revision_comparison_findings; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_comparison_findings TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_comparison_findings TO corridor_worker;


--
-- Name: SEQUENCE revision_comparison_findings_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.revision_comparison_findings_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.revision_comparison_findings_id_seq TO corridor_worker;


--
-- Name: TABLE revision_comparison_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_comparison_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_comparison_runs TO corridor_worker;


--
-- Name: SEQUENCE revision_comparison_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.revision_comparison_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.revision_comparison_runs_id_seq TO corridor_worker;


--
-- Name: TABLE revision_reconciliation_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_reconciliation_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revision_reconciliation_requests TO corridor_worker;


--
-- Name: TABLE schedule_governing_derivations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_governing_derivations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_governing_derivations TO corridor_worker;


--
-- Name: SEQUENCE schedule_governing_derivations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.schedule_governing_derivations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.schedule_governing_derivations_id_seq TO corridor_worker;


--
-- Name: TABLE schedule_link_activations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_link_activations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_link_activations TO corridor_worker;


--
-- Name: SEQUENCE schedule_link_activations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.schedule_link_activations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.schedule_link_activations_id_seq TO corridor_worker;


--
-- Name: TABLE schedule_link_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_link_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule_link_receipts TO corridor_worker;


--
-- Name: SEQUENCE schedule_link_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.schedule_link_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.schedule_link_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE scheduled_report_publications; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_report_publications TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scheduled_report_publications TO corridor_worker;


--
-- Name: SEQUENCE scheduled_report_publications_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.scheduled_report_publications_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.scheduled_report_publications_id_seq TO corridor_worker;


--
-- Name: TABLE sign_in_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sign_in_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sign_in_attempts TO corridor_worker;


--
-- Name: SEQUENCE sign_in_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.sign_in_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.sign_in_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE sign_in_tokens; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sign_in_tokens TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sign_in_tokens TO corridor_worker;


--
-- Name: SEQUENCE sign_in_tokens_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.sign_in_tokens_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.sign_in_tokens_id_seq TO corridor_worker;


--
-- Name: TABLE source_fact_append_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_fact_append_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_fact_append_receipts TO corridor_worker;


--
-- Name: SEQUENCE source_fact_append_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.source_fact_append_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.source_fact_append_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE source_fetch_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_fetch_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_fetch_attempts TO corridor_worker;


--
-- Name: SEQUENCE source_fetch_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.source_fetch_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.source_fetch_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE source_intake_draft_configurations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_intake_draft_configurations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_intake_draft_configurations TO corridor_worker;


--
-- Name: SEQUENCE source_intake_draft_configurations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.source_intake_draft_configurations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.source_intake_draft_configurations_id_seq TO corridor_worker;


--
-- Name: TABLE source_intake_draft_requests; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_intake_draft_requests TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_intake_draft_requests TO corridor_worker;


--
-- Name: SEQUENCE source_intake_draft_requests_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.source_intake_draft_requests_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.source_intake_draft_requests_id_seq TO corridor_worker;


--
-- Name: TABLE source_segments; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.source_segments TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_segments TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.source_segments TO corridor_worker;


--
-- Name: SEQUENCE source_segments_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.source_segments_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.source_segments_id_seq TO corridor_worker;


--
-- Name: TABLE stated_by_people; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.stated_by_people TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.stated_by_people TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.stated_by_people TO corridor_worker;


--
-- Name: SEQUENCE stated_by_people_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.stated_by_people_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.stated_by_people_id_seq TO corridor_worker;


--
-- Name: TABLE statement_coordination_receipts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_receipts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_receipts TO corridor_worker;


--
-- Name: SEQUENCE statement_coordination_receipts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_receipts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_receipts_id_seq TO corridor_worker;


--
-- Name: TABLE statement_coordination_reversal_effects; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_reversal_effects TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_reversal_effects TO corridor_worker;


--
-- Name: SEQUENCE statement_coordination_reversal_effects_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_reversal_effects_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_reversal_effects_id_seq TO corridor_worker;


--
-- Name: TABLE statement_coordination_reversals; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_reversals TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_coordination_reversals TO corridor_worker;


--
-- Name: SEQUENCE statement_coordination_reversals_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_reversals_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_coordination_reversals_id_seq TO corridor_worker;


--
-- Name: TABLE statement_suggestion_eligibility_declarations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_eligibility_declarations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_eligibility_declarations TO corridor_worker;


--
-- Name: SEQUENCE statement_suggestion_eligibility_declarations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq TO corridor_worker;


--
-- Name: TABLE statement_suggestion_protection_ends; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_protection_ends TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_protection_ends TO corridor_worker;


--
-- Name: SEQUENCE statement_suggestion_protection_ends_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_protection_ends_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_protection_ends_id_seq TO corridor_worker;


--
-- Name: TABLE statement_suggestion_protections; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_protections TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.statement_suggestion_protections TO corridor_worker;


--
-- Name: SEQUENCE statement_suggestion_protections_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_protections_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.statement_suggestion_protections_id_seq TO corridor_worker;


--
-- Name: TABLE subject_candidate_suggestions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_candidate_suggestions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_candidate_suggestions TO corridor_worker;


--
-- Name: SEQUENCE subject_candidate_suggestions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.subject_candidate_suggestions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.subject_candidate_suggestions_id_seq TO corridor_worker;


--
-- Name: TABLE subject_resolution_attempts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.subject_resolution_attempts TO corridor_fact_decision_writer;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_resolution_attempts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_resolution_attempts TO corridor_worker;


--
-- Name: SEQUENCE subject_resolution_attempts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_attempts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_attempts_id_seq TO corridor_worker;


--
-- Name: TABLE subject_resolution_candidates; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_resolution_candidates TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.subject_resolution_candidates TO corridor_worker;


--
-- Name: SEQUENCE subject_resolution_candidates_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_candidates_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_candidates_id_seq TO corridor_worker;


--
-- Name: TABLE subject_resolution_decisions; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT ON TABLE public.subject_resolution_decisions TO PUBLIC;
GRANT SELECT ON TABLE public.subject_resolution_decisions TO corridor_web;
GRANT SELECT ON TABLE public.subject_resolution_decisions TO corridor_worker;


--
-- Name: SEQUENCE subject_resolution_decisions_id_seq; Type: ACL; Schema: public; Owner: corridor_fact_decision_writer
--

GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_decisions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.subject_resolution_decisions_id_seq TO corridor_worker;


--
-- Name: TABLE token_layers; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_layers TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.token_layers TO corridor_worker;


--
-- Name: SEQUENCE token_layers_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.token_layers_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.token_layers_id_seq TO corridor_worker;


--
-- Name: TABLE unreadable_cell_admission_activations; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_admission_activations TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_admission_activations TO corridor_worker;


--
-- Name: SEQUENCE unreadable_cell_admission_activations_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_admission_activations_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_admission_activations_id_seq TO corridor_worker;


--
-- Name: TABLE unreadable_cell_reading_profiles; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_profiles TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_profiles TO corridor_worker;


--
-- Name: SEQUENCE unreadable_cell_reading_profiles_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_profiles_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_profiles_id_seq TO corridor_worker;


--
-- Name: TABLE unreadable_cell_reading_runs; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_runs TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_runs TO corridor_worker;


--
-- Name: SEQUENCE unreadable_cell_reading_runs_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_runs_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_runs_id_seq TO corridor_worker;


--
-- Name: TABLE unreadable_cell_reading_steps; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_steps TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_reading_steps TO corridor_worker;


--
-- Name: SEQUENCE unreadable_cell_reading_steps_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_steps_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_reading_steps_id_seq TO corridor_worker;


--
-- Name: TABLE unreadable_cell_resolutions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_resolutions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.unreadable_cell_resolutions TO corridor_worker;


--
-- Name: SEQUENCE unreadable_cell_resolutions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_resolutions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.unreadable_cell_resolutions_id_seq TO corridor_worker;


--
-- Name: TABLE web_sessions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_sessions TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.web_sessions TO corridor_worker;


--
-- Name: SEQUENCE web_sessions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.web_sessions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.web_sessions_id_seq TO corridor_worker;


--
-- Name: TABLE work_decision_milestone_impacts; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.work_decision_milestone_impacts TO corridor_web;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.work_decision_milestone_impacts TO corridor_worker;


--
-- Name: SEQUENCE work_decision_milestone_impacts_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.work_decision_milestone_impacts_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.work_decision_milestone_impacts_id_seq TO corridor_worker;


--
-- Name: TABLE work_decisions; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT ON TABLE public.work_decisions TO corridor_statement_retirement;
GRANT SELECT ON TABLE public.work_decisions TO corridor_web;
GRANT SELECT ON TABLE public.work_decisions TO corridor_worker;


--
-- Name: SEQUENCE work_decisions_id_seq; Type: ACL; Schema: public; Owner: corridor
--

GRANT SELECT,USAGE ON SEQUENCE public.work_decisions_id_seq TO corridor_web;
GRANT SELECT,USAGE ON SEQUENCE public.work_decisions_id_seq TO corridor_worker;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: corridor
--

ALTER DEFAULT PRIVILEGES FOR ROLE corridor IN SCHEMA public GRANT SELECT,USAGE ON SEQUENCES TO corridor_web;
ALTER DEFAULT PRIVILEGES FOR ROLE corridor IN SCHEMA public GRANT SELECT,USAGE ON SEQUENCES TO corridor_worker;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: corridor
--

ALTER DEFAULT PRIVILEGES FOR ROLE corridor IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO corridor_web;
ALTER DEFAULT PRIVILEGES FOR ROLE corridor IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO corridor_worker;


--
-- PostgreSQL database dump complete
--


