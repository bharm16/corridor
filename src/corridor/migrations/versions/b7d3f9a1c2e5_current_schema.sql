
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

CREATE FUNCTION public.enforce_assignment_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification occurrences are immutable';
        end
        $$;

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

CREATE FUNCTION public.enforce_candidate_run_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        declare
            demo_project boolean;
            owned_by_run boolean;
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from projects
                     where projects.id = old.project_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if tg_op = 'INSERT' then
                if new.extraction_run_id is not null then
                    raise exception
                        'Candidate must attach to its run after input capture'
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
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if new.extraction_run_id is distinct from old.extraction_run_id then
                if old.extraction_run_id is not null
                   or new.extraction_run_id is null then
                    raise exception 'Candidate run lineage is immutable'
                        using errcode = '23514';
                end if;
                select exists (
                    select 1
                      from extraction_runs
                      join documents
                        on documents.id = extraction_runs.document_id
                     where extraction_runs.id = new.extraction_run_id
                       and extraction_runs.document_id = new.source_document_id
                       and documents.project_id = new.project_id
                       and extraction_runs.prompt_version = new.prompt_version
                       and extraction_runs.model is not distinct from new.model
                       and extraction_runs.candidate_inputs_json @>
                           jsonb_build_array(
                               jsonb_build_object(
                                   'candidate_id', new.id,
                                   'project_id', new.project_id,
                                   'source_document_id', new.source_document_id,
                                   'prompt_version', new.prompt_version,
                                   'model', new.model
                               )
                           )
                ) into owned_by_run;
                if not owned_by_run then
                    raise exception
                        'Candidate is absent from its immutable run inputs'
                        using errcode = '23514';
                end if;
            end if;
            return new;
        end;
        $$;

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

CREATE FUNCTION public.enforce_document_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification occurrences are immutable';
        end
        $$;

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

CREATE FUNCTION public.enforce_due_action_notification_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification occurrences are immutable';
        end
        $$;

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

CREATE FUNCTION public.enforce_milestone_registration() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            if tg_op = 'INSERT' then return new; end if;
            raise exception 'Milestone Registrations are immutable'
                using errcode = '23514';
        end
        $$;

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

CREATE FUNCTION public.prevent_external_report_artifact_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;

CREATE FUNCTION public.prevent_external_report_release_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;

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

CREATE FUNCTION public.refuse_assignment_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification attempts are append-only';
        end
        $$;

CREATE FUNCTION public.refuse_assignment_feedback_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification feedback is append-only';
        end
        $$;

CREATE FUNCTION public.refuse_document_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification attempts are append-only';
        end
        $$;

CREATE FUNCTION public.refuse_due_action_attempt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification attempts are append-only';
        end
        $$;

CREATE FUNCTION public.refuse_due_work_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Due Work receipts are append-only';
        end
        $$;

CREATE FUNCTION public.refuse_event_admission_acceptance_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Event Admission acceptance history is immutable';
        end
        $$;

CREATE FUNCTION public.refuse_evidence_investigation_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
          raise exception 'Evidence Investigation receipts are append-only';
        end $$;

CREATE FUNCTION public.refuse_key_date_draft_receipt_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
          raise exception 'Key date draft receipts are append-only';
        end $$;

CREATE FUNCTION public.reject_assignment_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'assignment notification history cannot be truncated';
        end
        $$;

CREATE FUNCTION public.reject_condition_resolutions_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'condition_resolutions are append-only'; end; $$;

CREATE FUNCTION public.reject_coordination_summary_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'coordination_summary_configurations are append-only'; end; $$;

CREATE FUNCTION public.reject_coordination_summary_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'coordination_summary_requests are append-only'; end; $$;

CREATE FUNCTION public.reject_dependency_dismissal_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dependency_dismissals is append-only'
                using errcode = '23514';
        end;
        $$;

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

CREATE FUNCTION public.reject_dispute_history_resolution_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dispute_history_resolutions is append-only';
        end;
        $$;

CREATE FUNCTION public.reject_dispute_settlement_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'dispute_settlements is append-only'
                using errcode = '23514';
        end;
        $$;

CREATE FUNCTION public.reject_document_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document notification history cannot be truncated';
        end
        $$;

CREATE FUNCTION public.reject_document_rendition_derivation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'document rendition derivations are append-only';
        end;
        $$;

CREATE FUNCTION public.reject_documentation_confirmation_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'documentation field confirmations are append-only';
        end;
        $$;

CREATE FUNCTION public.reject_due_action_notification_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'due action notification history cannot be truncated';
        end
        $$;

CREATE FUNCTION public.reject_due_work_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Due Work operational history cannot be truncated';
        end
        $$;

CREATE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'evidence_investigation_capture_contracts are append-only'; end; $$;

CREATE FUNCTION public.reject_evidence_investigation_capture_results_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'evidence_investigation_capture_results are append-only'; end; $$;

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

CREATE FUNCTION public.reject_external_party_statement_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'External Party statements are append-only'
                using errcode = '23514';
        end;
        $$;

CREATE FUNCTION public.reject_external_report_artifact_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'rendered External Report artifacts are immutable'
                using errcode = '55000';
        end;
        $$;

CREATE FUNCTION public.reject_external_report_release_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'released External Report receipts are immutable'
                using errcode = '55000';
        end;
        $$;

CREATE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'extraction_failure_diagnosis_configurations are append-only'; end; $$;

CREATE FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'extraction_failure_diagnosis_requests are append-only'; end; $$;

CREATE FUNCTION public.reject_extraction_measurement_case_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Extraction Measurement case states are append-only';
        end;
        $$;

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

CREATE FUNCTION public.reject_organization_identity_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'organization identity history is append-only';
        end;
        $$;

CREATE FUNCTION public.reject_production_run_explanation_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'production_run_explanation_configurations are append-only'; end; $$;

CREATE FUNCTION public.reject_production_run_explanation_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'production_run_explanation_requests are append-only'; end; $$;

CREATE FUNCTION public.reject_project_check_configuration_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'project check configurations are append-only';
        end;
        $$;

CREATE FUNCTION public.reject_revision_change_explanation_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'revision_change_explanation_configurations are append-only'; end; $$;

CREATE FUNCTION public.reject_revision_change_explanation_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'revision_change_explanation_requests are append-only'; end; $$;

CREATE FUNCTION public.reject_revision_comparison_truncate() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Revision Comparison receipts are append-only'
                using errcode = '23514';
        end;
        $$;

CREATE FUNCTION public.reject_schedule_linking_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'schedule linking records are append-only';
        end;
        $$;

CREATE FUNCTION public.reject_scheduled_report_publications_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin raise exception 'scheduled_report_publications are append-only'; end; $$;

CREATE FUNCTION public.reject_source_intake_draft_configurations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'source_intake_draft_configurations are append-only'; end; $$;

CREATE FUNCTION public.reject_source_intake_draft_requests_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'source_intake_draft_requests are append-only'; end; $$;

CREATE FUNCTION public.reject_statement_lifecycle_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'Guided statement lifecycle history is append-only'
                using errcode = '23514';
        end;
        $$;

CREATE FUNCTION public.reject_statement_suggestion_protection_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            raise exception 'statement suggestion protection history is append-only';
        end;
        $$;

CREATE FUNCTION public.reject_unreadable_cell_admission_activations_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_admission_activations are append-only'; end; $$;

CREATE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_profiles are append-only'; end; $$;

CREATE FUNCTION public.reject_unreadable_cell_reading_runs_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_runs are append-only'; end; $$;

CREATE FUNCTION public.reject_unreadable_cell_reading_steps_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_reading_steps are append-only'; end; $$;

CREATE FUNCTION public.reject_unreadable_cell_resolutions_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            begin raise exception 'unreadable_cell_resolutions are append-only'; end; $$;

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

CREATE FUNCTION public.require_policy_outcome_counts_by_policy_run_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.policy_run_id);
            return null;
        end;
        $$;

CREATE FUNCTION public.require_policy_outcome_counts_by_run_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.run_id);
            return null;
        end;
        $$;

CREATE FUNCTION public.require_policy_run_counts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        begin
            perform verify_policy_run_counts(new.id);
            return null;
        end;
        $$;

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

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.active_extraction_runs (
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.active_run_declarations (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    extraction_run_id bigint NOT NULL,
    declared_by character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    predecessor_declaration_id bigint
);

CREATE SEQUENCE public.active_run_declarations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.active_run_declarations_id_seq OWNED BY public.active_run_declarations.id;

CREATE TABLE public.assertions (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    field_name character varying(64) NOT NULL,
    asserted_value text,
    evidence_link_id bigint NOT NULL,
    doc_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.assertions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assertions_id_seq OWNED BY public.assertions.id;

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
    CONSTRAINT ck_assignment_attempt_outcome CHECK (((outcome)::text = ANY ((ARRAY['completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying, 'skipped'::character varying])::text[]))),
    CONSTRAINT ck_assignment_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_assignment_attempt_positive CHECK ((attempt_number > 0))
);

CREATE SEQUENCE public.assignment_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assignment_notification_attempts_id_seq OWNED BY public.assignment_notification_attempts.id;

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
    CONSTRAINT ck_assignment_dispatch_state CHECK (((delivery_state)::text = ANY ((ARRAY['queued'::character varying, 'completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying])::text[])))
);

CREATE SEQUENCE public.assignment_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assignment_notification_dispatches_id_seq OWNED BY public.assignment_notification_dispatches.id;

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

CREATE SEQUENCE public.assignment_notification_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assignment_notification_feedback_id_seq OWNED BY public.assignment_notification_feedback.id;

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
    CONSTRAINT ck_assignment_notification_subject_kind CHECK (((subject_kind)::text = ANY ((ARRAY['constraint'::character varying, 'statement'::character varying])::text[]))),
    CONSTRAINT ck_assignment_notification_subject_shape CHECK (((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL))))
);

CREATE SEQUENCE public.assignment_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assignment_notifications_id_seq OWNED BY public.assignment_notifications.id;

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

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

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
    CONSTRAINT ck_automatic_carry_forward_outcome_value CHECK (((outcome)::text = ANY ((ARRAY['carried'::character varying, 'abstained'::character varying])::text[])))
);

CREATE SEQUENCE public.automatic_carry_forward_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.automatic_carry_forward_outcomes_id_seq OWNED BY public.automatic_carry_forward_outcomes.id;

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

CREATE TABLE public.candidate_dispositions (
    id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    disposition character varying(32) NOT NULL,
    reason character varying(64),
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_candidate_dispositions_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_candidate_dispositions_kind CHECK (((disposition)::text = ANY ((ARRAY['accepted'::character varying, 'not_relevant'::character varying])::text[]))),
    CONSTRAINT ck_candidate_dispositions_reason CHECK (((((disposition)::text = 'accepted'::text) AND (reason IS NULL)) OR (((disposition)::text = 'not_relevant'::text) AND (reason IS NOT NULL))))
);

CREATE SEQUENCE public.candidate_dispositions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.candidate_dispositions_id_seq OWNED BY public.candidate_dispositions.id;

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
    CONSTRAINT candidate_kind CHECK (((kind)::text = ANY ((ARRAY['dependency'::character varying, 'event'::character varying, 'evidence'::character varying])::text[]))),
    CONSTRAINT candidate_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'accepted'::character varying, 'merged'::character varying, 'rejected'::character varying])::text[])))
);

CREATE SEQUENCE public.candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.candidates_id_seq OWNED BY public.candidates.id;

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

CREATE SEQUENCE public.cohort_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.cohort_receipts_id_seq OWNED BY public.cohort_receipts.id;

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

CREATE SEQUENCE public.commitment_lineages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.commitment_lineages_id_seq OWNED BY public.commitment_lineages.id;

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
    CONSTRAINT ck_condition_resolution_kind CHECK (((kind)::text = ANY ((ARRAY['cleared'::character varying, 'dismissed'::character varying])::text[]))),
    CONSTRAINT ck_condition_resolution_text CHECK ((length(TRIM(BOTH FROM condition_text)) > 0))
);

CREATE SEQUENCE public.condition_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.condition_resolutions_id_seq OWNED BY public.condition_resolutions.id;

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
    CONSTRAINT ck_summary_config_retention CHECK (((retention_policy)::text = 'retained_indefinitely'::text)),
    CONSTRAINT ck_summary_config_source_scope CHECK (((source_scope)::text = ANY ((ARRAY['all_sources'::character varying, 'documents_only'::character varying])::text[]))),
    CONSTRAINT ck_summary_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);

CREATE SEQUENCE public.coordination_summary_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.coordination_summary_configurations_id_seq OWNED BY public.coordination_summary_configurations.id;

CREATE TABLE public.coordination_summary_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    reading_sha256 character varying(64) NOT NULL,
    project_reading_json jsonb NOT NULL,
    evaluated_on date NOT NULL,
    ruleset_version character varying(32) NOT NULL,
    statement_publication_fingerprint character varying(64) NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    summary_markdown text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_summary_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_summary_request_reading_sha CHECK (((reading_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_summary_request_status CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'empty_input'::character varying, 'budget_exhausted'::character varying, 'timeout'::character varying, 'transport_failure'::character varying, 'validation_refused'::character varying])::text[])))
);

CREATE SEQUENCE public.coordination_summary_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.coordination_summary_requests_id_seq OWNED BY public.coordination_summary_requests.id;

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
    CONSTRAINT dep_type CHECK (((dep_type)::text = ANY ((ARRAY['utility_relocation'::character varying, 'agreement'::character varying, 'permit'::character varying, 'row'::character varying, 'railroad'::character varying, 'access'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT resolution_strategy CHECK (((resolution_strategy)::text = ANY ((ARRAY['relocate'::character varying, 'remove'::character varying, 'abandon_in_place'::character varying, 'adjust_vertical'::character varying, 'protect_in_place'::character varying, 'change_design'::character varying, 'policy_exception'::character varying])::text[])))
);

CREATE SEQUENCE public.dependencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependencies_id_seq OWNED BY public.dependencies.id;

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
    CONSTRAINT ck_dependency_admission_outcome_eligibility_shape CHECK (((((outcome)::text = 'abstained'::text) AND (((eligibility_json IS NULL) AND (eligibility_sha256 IS NULL)) OR ((eligibility_json IS NOT NULL) AND (eligibility_sha256 IS NOT NULL)))) OR (((outcome)::text = ANY ((ARRAY['admitted'::character varying, 'merged'::character varying])::text[])) AND (eligibility_json IS NULL) AND (eligibility_sha256 IS NULL)))),
    CONSTRAINT ck_dependency_admission_outcome_kind CHECK (((((outcome)::text = ANY ((ARRAY['admitted'::character varying, 'merged'::character varying])::text[])) AND (reason IS NULL) AND (dependency_id IS NOT NULL)) OR (((outcome)::text = 'abstained'::text) AND (reason IS NOT NULL) AND (dependency_id IS NULL)))),
    CONSTRAINT ck_dependency_admission_outcome_value CHECK (((outcome)::text = ANY ((ARRAY['admitted'::character varying, 'merged'::character varying, 'abstained'::character varying])::text[]))),
    CONSTRAINT ck_dependency_admission_outcomes_family CHECK (((family)::text = 'dependency-admission'::text))
);

CREATE SEQUENCE public.dependency_admission_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_admission_outcomes_id_seq OWNED BY public.dependency_admission_outcomes.id;

CREATE TABLE public.dependency_dismissals (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    reason character varying(32) NOT NULL,
    dismissed_by text NOT NULL,
    dismissed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_dismissals_attributable CHECK ((length(TRIM(BOTH FROM dismissed_by)) > 0)),
    CONSTRAINT ck_dependency_dismissals_reason CHECK (((reason)::text = ANY ((ARRAY['duplicate'::character varying, 'not-a-conflict'::character varying, 'wrong'::character varying])::text[])))
);

CREATE SEQUENCE public.dependency_dismissals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_dismissals_id_seq OWNED BY public.dependency_dismissals.id;

CREATE TABLE public.dependency_event_evidence (
    evidence_link_id bigint NOT NULL,
    event_id bigint NOT NULL,
    recorded_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_event_evidence_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0))
);

CREATE TABLE public.dependency_event_migration_receipts (
    event_id bigint NOT NULL,
    original_event jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.dependency_event_scope_decisions (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    scope_mode character varying(16) NOT NULL,
    supersedes_scope_decision_id bigint,
    decided_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_dependency_event_scope_decisions_actor CHECK ((length(TRIM(BOTH FROM decided_by)) > 0)),
    CONSTRAINT ck_dependency_event_scope_decisions_mode CHECK (((scope_mode)::text = ANY ((ARRAY['unknown'::character varying, 'selected'::character varying, 'all_active'::character varying, 'carried_forward'::character varying])::text[])))
);

CREATE SEQUENCE public.dependency_event_scope_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_event_scope_decisions_id_seq OWNED BY public.dependency_event_scope_decisions.id;

CREATE TABLE public.dependency_event_scopes (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    scope_decision_id bigint NOT NULL,
    recorded_by text NOT NULL
);

CREATE SEQUENCE public.dependency_event_scopes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_event_scopes_id_seq OWNED BY public.dependency_event_scopes.id;

CREATE TABLE public.dependency_event_timings (
    id bigint NOT NULL,
    event_id bigint NOT NULL,
    kind character varying(16) NOT NULL,
    text text NOT NULL,
    "precision" character varying(32) NOT NULL,
    start_date date,
    end_date date,
    CONSTRAINT ck_dependency_event_timing_bounds CHECK ((((("precision")::text = 'day'::text) AND (start_date IS NOT NULL) AND (end_date = start_date)) OR ((("precision")::text = 'month'::text) AND (start_date IS NOT NULL) AND (end_date IS NOT NULL) AND (start_date = (date_trunc('month'::text, (start_date)::timestamp without time zone))::date) AND (end_date = ((date_trunc('month'::text, (start_date)::timestamp without time zone) + '1 mon -1 days'::interval))::date)) OR ((("precision")::text = ANY ((ARRAY['approximate'::character varying, 'legacy_unknown'::character varying])::text[])) AND (start_date IS NULL) AND (end_date IS NULL)))),
    CONSTRAINT ck_dependency_event_timing_kind CHECK (((kind)::text = ANY ((ARRAY['previous'::character varying, 'new'::character varying])::text[]))),
    CONSTRAINT ck_dependency_event_timing_precision CHECK ((("precision")::text = ANY ((ARRAY['day'::character varying, 'month'::character varying, 'approximate'::character varying, 'legacy_unknown'::character varying])::text[])))
);

CREATE SEQUENCE public.dependency_event_timings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_event_timings_id_seq OWNED BY public.dependency_event_timings.id;

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
    CONSTRAINT ck_dependency_events_scope_mode CHECK (((scope_mode)::text = ANY ((ARRAY['unknown'::character varying, 'selected'::character varying, 'all_active'::character varying, 'carried_forward'::character varying])::text[]))),
    CONSTRAINT ck_dependency_events_source_kind CHECK (((source_kind)::text = ANY ((ARRAY['cited'::character varying, 'verbal'::character varying])::text[]))),
    CONSTRAINT ck_dependency_events_timing_direction CHECK (((timing_direction IS NULL) OR ((timing_direction)::text = ANY ((ARRAY['earlier'::character varying, 'later'::character varying, 'unknown'::character varying])::text[])))),
    CONSTRAINT event_type CHECK (((event_type)::text = ANY ((ARRAY['commitment'::character varying, 'committed_date_change'::character varying, 'response'::character varying, 'escalation'::character varying, 'status_change'::character varying, 'closure'::character varying])::text[])))
);

CREATE SEQUENCE public.dependency_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_events_id_seq OWNED BY public.dependency_events.id;

CREATE TABLE public.dependency_evidence_sufficiencies (
    id bigint NOT NULL,
    dependency_id bigint NOT NULL,
    evidence_link_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scope_link_id bigint
);

CREATE SEQUENCE public.dependency_evidence_sufficiencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dependency_evidence_sufficiencies_id_seq OWNED BY public.dependency_evidence_sufficiencies.id;

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
    CONSTRAINT ck_discovered_reference_authorized_doc_type CHECK (((authorized_doc_type IS NULL) OR ((authorized_doc_type)::text = ANY ((ARRAY['matrix'::character varying, 'minutes'::character varying, 'agreement'::character varying, 'email'::character varying, 'plan'::character varying, 'schedule'::character varying, 'spec'::character varying, 'status_report'::character varying, 'other'::character varying])::text[])))),
    CONSTRAINT ck_discovered_reference_observed_count CHECK ((observed_count >= 1)),
    CONSTRAINT ck_discovered_reference_state CHECK (((((state)::text = 'proposed'::text) AND (authorized_at IS NULL) AND (registered_document_id IS NULL)) OR (((state)::text = 'authorized'::text) AND (authorized_at IS NOT NULL) AND (authorized_doc_type IS NOT NULL)) OR (((state)::text = 'registered'::text) AND (authorized_at IS NOT NULL) AND (registered_document_id IS NOT NULL)))),
    CONSTRAINT discovered_reference_state CHECK (((state)::text = ANY ((ARRAY['proposed'::character varying, 'authorized'::character varying, 'registered'::character varying])::text[])))
);

CREATE SEQUENCE public.discovered_references_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.discovered_references_id_seq OWNED BY public.discovered_references.id;

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
    CONSTRAINT ck_dispute_history_resolutions_outcome CHECK (((outcome)::text = ANY ((ARRAY['physical_superseded'::character varying, 'contractual_amendment'::character varying])::text[]))),
    CONSTRAINT ck_dispute_history_resolutions_rule_version CHECK ((length(TRIM(BOTH FROM rule_version)) > 0))
);

CREATE SEQUENCE public.dispute_history_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dispute_history_resolutions_id_seq OWNED BY public.dispute_history_resolutions.id;

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

CREATE SEQUENCE public.dispute_settlements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.dispute_settlements_id_seq OWNED BY public.dispute_settlements.id;

CREATE TABLE public.doc_pages (
    id bigint NOT NULL,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    text text NOT NULL,
    image_path text,
    text_source character varying(10) DEFAULT 'text_layer'::character varying NOT NULL,
    CONSTRAINT text_source CHECK (((text_source)::text = ANY ((ARRAY['text_layer'::character varying, 'ocr'::character varying, 'cells'::character varying])::text[])))
);

CREATE SEQUENCE public.doc_pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.doc_pages_id_seq OWNED BY public.doc_pages.id;

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
    CONSTRAINT ck_document_attempt_outcome CHECK (((outcome)::text = ANY ((ARRAY['completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying, 'skipped'::character varying])::text[]))),
    CONSTRAINT ck_document_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_document_attempt_positive CHECK ((attempt_number > 0))
);

CREATE SEQUENCE public.document_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.document_notification_attempts_id_seq OWNED BY public.document_notification_attempts.id;

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
    CONSTRAINT ck_document_dispatch_state CHECK (((delivery_state)::text = ANY ((ARRAY['queued'::character varying, 'completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying])::text[])))
);

CREATE SEQUENCE public.document_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.document_notification_dispatches_id_seq OWNED BY public.document_notification_dispatches.id;

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
    CONSTRAINT ck_document_notification_category CHECK (((category)::text = ANY ((ARRAY['documentation_loss'::character varying, 'document_change'::character varying])::text[]))),
    CONSTRAINT ck_document_notification_key_hex CHECK (((occurrence_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_document_notification_recipient CHECK ((length(TRIM(BOTH FROM recipient_principal_subject)) > 0)),
    CONSTRAINT ck_document_notification_recipient_role CHECK (((recipient_role)::text = ANY ((ARRAY['current_assignee'::character varying, 'original_reviewer'::character varying, 'current_assignee_and_original_reviewer'::character varying])::text[]))),
    CONSTRAINT ck_document_notification_subject_kind CHECK (((subject_kind)::text = ANY ((ARRAY['constraint'::character varying, 'statement'::character varying])::text[]))),
    CONSTRAINT ck_document_notification_subject_shape CHECK (((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL))))
);

CREATE SEQUENCE public.document_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.document_notifications_id_seq OWNED BY public.document_notifications.id;

CREATE TABLE public.document_quarantines (
    document_id bigint NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

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

CREATE SEQUENCE public.document_rendition_derivations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.document_rendition_derivations_id_seq OWNED BY public.document_rendition_derivations.id;

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
    CONSTRAINT ck_documentation_confirmation_known_classification CHECK (((classification)::text = ANY ((ARRAY['approved'::character varying, 'conditional'::character varying])::text[]))),
    CONSTRAINT ck_documentation_confirmation_known_conclusion CHECK (((conclusion)::text = 'approved'::text)),
    CONSTRAINT ck_documentation_confirmation_known_field CHECK (((field_name)::text = 'approval_interpretation'::text)),
    CONSTRAINT ck_documentation_confirmation_override_records_hedge CHECK (((condition_immaterial = false) OR (((classification)::text = 'conditional'::text) AND (overridden_condition_text IS NOT NULL) AND (length(TRIM(BOTH FROM overridden_condition_text)) > 0))))
);

CREATE SEQUENCE public.documentation_field_confirmations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.documentation_field_confirmations_id_seq OWNED BY public.documentation_field_confirmations.id;

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
    CONSTRAINT ck_documents_numbering_scheme CHECK (((numbering_scheme)::text = ANY ((ARRAY['project-unique'::character varying, 'per-party'::character varying])::text[]))),
    CONSTRAINT doc_type CHECK (((doc_type)::text = ANY ((ARRAY['matrix'::character varying, 'minutes'::character varying, 'agreement'::character varying, 'email'::character varying, 'plan'::character varying, 'schedule'::character varying, 'spec'::character varying, 'status_report'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT parse_status CHECK (((parse_status)::text = ANY ((ARRAY['pending'::character varying, 'parsed'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;

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
    CONSTRAINT ck_due_action_attempt_outcome CHECK (((outcome)::text = ANY ((ARRAY['completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying, 'skipped'::character varying])::text[]))),
    CONSTRAINT ck_due_action_attempt_owner CHECK ((length(TRIM(BOTH FROM runtime_owner)) > 0)),
    CONSTRAINT ck_due_action_attempt_positive CHECK ((attempt_number > 0))
);

CREATE SEQUENCE public.due_action_notification_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_action_notification_attempts_id_seq OWNED BY public.due_action_notification_attempts.id;

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
    CONSTRAINT ck_due_action_dispatch_state CHECK (((delivery_state)::text = ANY ((ARRAY['queued'::character varying, 'completed'::character varying, 'retry_due'::character varying, 'failed'::character varying, 'uncertain'::character varying])::text[])))
);

CREATE SEQUENCE public.due_action_notification_dispatches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_action_notification_dispatches_id_seq OWNED BY public.due_action_notification_dispatches.id;

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
    CONSTRAINT ck_due_action_notification_category CHECK (((category)::text = ANY ((ARRAY['next_action_due'::character varying, 'next_action_escalation'::character varying, 'daily_summary'::character varying])::text[]))),
    CONSTRAINT ck_due_action_notification_key_hex CHECK (((occurrence_key)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_due_action_notification_recipient CHECK ((length(TRIM(BOTH FROM recipient_principal_subject)) > 0)),
    CONSTRAINT ck_due_action_notification_role CHECK (((recipient_role)::text = ANY ((ARRAY['assignee'::character varying, 'escalation'::character varying, 'summary'::character varying])::text[]))),
    CONSTRAINT ck_due_action_notification_shape CHECK (((((category)::text = 'daily_summary'::text) AND (subject_kind IS NULL) AND (dependency_id IS NULL) AND (commitment_lineage_id IS NULL) AND (plan_decision_id IS NULL) AND (urgency IS NULL) AND (action_due_date IS NULL) AND ((recipient_role)::text = 'summary'::text) AND (observation_start IS NOT NULL) AND (observation_end IS NOT NULL)) OR (((category)::text = ANY ((ARRAY['next_action_due'::character varying, 'next_action_escalation'::character varying])::text[])) AND ((subject_kind)::text = ANY ((ARRAY['constraint'::character varying, 'statement'::character varying])::text[])) AND (plan_decision_id IS NOT NULL) AND (urgency IS NOT NULL) AND ((recipient_role)::text = ANY ((ARRAY['assignee'::character varying, 'escalation'::character varying])::text[])) AND ((((subject_kind)::text = 'constraint'::text) AND (dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR (((subject_kind)::text = 'statement'::text) AND (commitment_lineage_id IS NOT NULL) AND (dependency_id IS NULL)))))),
    CONSTRAINT ck_due_action_notification_urgency CHECK (((urgency IS NULL) OR ((urgency)::text = ANY ((ARRAY['soon'::character varying, 'overdue'::character varying, 'urgent_overdue'::character varying])::text[]))))
);

CREATE SEQUENCE public.due_action_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_action_notifications_id_seq OWNED BY public.due_action_notifications.id;

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
    CONSTRAINT ck_due_work_occurrence_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'claimed'::character varying, 'retry_due'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.due_work_occurrences_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_work_occurrences_id_seq OWNED BY public.due_work_occurrences.id;

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
    CONSTRAINT ck_due_work_receipt_outcome CHECK (((execution_outcome)::text = ANY ((ARRAY['completed'::character varying, 'retry_due'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_due_work_receipt_result_shape CHECK (((((execution_outcome)::text = 'completed'::text) AND (handler_result_json IS NOT NULL) AND (error_code IS NULL)) OR (((execution_outcome)::text = ANY ((ARRAY['retry_due'::character varying, 'failed'::character varying])::text[])) AND (handler_result_json IS NULL) AND (error_code IS NOT NULL))))
);

CREATE SEQUENCE public.due_work_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_work_receipts_id_seq OWNED BY public.due_work_receipts.id;

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

CREATE SEQUENCE public.due_work_schedules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.due_work_schedules_id_seq OWNED BY public.due_work_schedules.id;

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
    CONSTRAINT ck_event_admission_acceptance_status CHECK (((status)::text = ANY ((ARRAY['passed'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.event_admission_acceptance_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.event_admission_acceptance_receipts_id_seq OWNED BY public.event_admission_acceptance_receipts.id;

CREATE TABLE public.event_admission_activations (
    id bigint NOT NULL,
    project_id integer NOT NULL,
    acceptance_receipt_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    policy_version character varying(64) NOT NULL,
    reason character varying(128) NOT NULL,
    recorded_by character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_admission_activation_action CHECK (((action)::text = ANY ((ARRAY['activate'::character varying, 'suspend'::character varying])::text[]))),
    CONSTRAINT ck_event_admission_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_event_admission_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0))
);

CREATE SEQUENCE public.event_admission_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.event_admission_activations_id_seq OWNED BY public.event_admission_activations.id;

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
    CONSTRAINT ck_event_admission_outcome_value CHECK (((outcome)::text = ANY ((ARRAY['admitted'::character varying, 'abstained'::character varying])::text[]))),
    CONSTRAINT ck_event_admission_outcomes_family CHECK (((family)::text = 'event-admission'::text))
);

CREATE SEQUENCE public.event_admission_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.event_admission_outcomes_id_seq OWNED BY public.event_admission_outcomes.id;

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

CREATE SEQUENCE public.event_cohort_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.event_cohort_receipts_id_seq OWNED BY public.event_cohort_receipts.id;

CREATE TABLE public.evidence_investigation_candidate_review_starts (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    principal character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL
);

CREATE SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_candidate_review_starts_id_seq OWNED BY public.evidence_investigation_candidate_review_starts.id;

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

CREATE SEQUENCE public.evidence_investigation_capture_contracts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_capture_contracts_id_seq OWNED BY public.evidence_investigation_capture_contracts.id;

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
    CONSTRAINT ck_capture_result_completeness CHECK (((completeness)::text = ANY ((ARRAY['complete'::character varying, 'incomplete'::character varying])::text[]))),
    CONSTRAINT ck_capture_result_incomplete_reason CHECK (((((completeness)::text = 'complete'::text) AND (incomplete_reason IS NULL)) OR (((completeness)::text = 'incomplete'::text) AND (incomplete_reason IS NOT NULL))))
);

CREATE SEQUENCE public.evidence_investigation_capture_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_capture_results_id_seq OWNED BY public.evidence_investigation_capture_results.id;

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

CREATE SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_evaluation_receipts_id_seq OWNED BY public.evidence_investigation_evaluation_receipts.id;

CREATE TABLE public.evidence_investigation_packet_receipts (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    packet_json jsonb NOT NULL,
    validator_outcome character varying(32) NOT NULL,
    packet_sha256 character varying(64) NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL
);

CREATE SEQUENCE public.evidence_investigation_packet_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_packet_receipts_id_seq OWNED BY public.evidence_investigation_packet_receipts.id;

CREATE TABLE public.evidence_investigation_review_observations (
    id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    boundary character varying(16) NOT NULL,
    principal character varying(128) NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_shadow_review_boundary CHECK (((boundary)::text = ANY ((ARRAY['start'::character varying, 'end'::character varying])::text[])))
);

CREATE SEQUENCE public.evidence_investigation_review_observations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_review_observations_id_seq OWNED BY public.evidence_investigation_review_observations.id;

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
    CONSTRAINT ck_evidence_investigation_runs_terminal_status CHECK (((terminal_status)::text = ANY ((ARRAY['options_available'::character varying, 'human_judgment_needed'::character varying, 'abstained'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.evidence_investigation_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_runs_id_seq OWNED BY public.evidence_investigation_runs.id;

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

CREATE SEQUENCE public.evidence_investigation_shadow_cases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_shadow_cases_id_seq OWNED BY public.evidence_investigation_shadow_cases.id;

CREATE TABLE public.evidence_investigation_shadow_executions (
    id bigint NOT NULL,
    shadow_case_id bigint NOT NULL,
    run_id bigint NOT NULL,
    execution_status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.evidence_investigation_shadow_executions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_shadow_executions_id_seq OWNED BY public.evidence_investigation_shadow_executions.id;

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

CREATE SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_shadow_outcomes_id_seq OWNED BY public.evidence_investigation_shadow_outcomes.id;

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

CREATE SEQUENCE public.evidence_investigation_step_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_investigation_step_receipts_id_seq OWNED BY public.evidence_investigation_step_receipts.id;

CREATE TABLE public.evidence_links (
    id bigint NOT NULL,
    dependency_id bigint,
    document_id bigint NOT NULL,
    page_no integer NOT NULL,
    quote text NOT NULL,
    verified boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.evidence_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.evidence_links_id_seq OWNED BY public.evidence_links.id;

CREATE TABLE public.external_orgs (
    id bigint NOT NULL,
    name text NOT NULL,
    org_type character varying(10) DEFAULT 'utility'::character varying NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT org_type CHECK (((org_type)::text = ANY ((ARRAY['utility'::character varying, 'railroad'::character varying, 'agency'::character varying, 'consultant'::character varying, 'other'::character varying])::text[])))
);

CREATE SEQUENCE public.external_orgs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.external_orgs_id_seq OWNED BY public.external_orgs.id;

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
    CONSTRAINT ck_external_report_artifacts_provenance_mode CHECK (((provenance_mode)::text = ANY ((ARRAY['all-supported-sources'::character varying, 'document-only'::character varying])::text[])))
);

CREATE SEQUENCE public.external_report_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.external_report_artifacts_id_seq OWNED BY public.external_report_artifacts.id;

CREATE TABLE public.external_report_releases (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    artifact_name text NOT NULL,
    format character varying(16) DEFAULT 'pdf'::character varying NOT NULL,
    pdf_bytes bytea NOT NULL,
    pdf_sha256 character varying(64) NOT NULL,
    evaluated_on date NOT NULL,
    ruleset_version character varying(64) NOT NULL,
    provenance_mode character varying(32) NOT NULL,
    record_context_json jsonb NOT NULL,
    released_by character varying(128) NOT NULL,
    released_at timestamp with time zone DEFAULT now() NOT NULL,
    evaluation_context_json jsonb,
    artifact_id bigint,
    released_by_display text,
    CONSTRAINT ck_external_report_releases_artifact_name CHECK ((length(TRIM(BOTH FROM artifact_name)) > 0)),
    CONSTRAINT ck_external_report_releases_context_object CHECK ((jsonb_typeof(record_context_json) = 'object'::text)),
    CONSTRAINT ck_external_report_releases_evaluation_object CHECK (((evaluation_context_json IS NULL) OR (jsonb_typeof(evaluation_context_json) = 'object'::text))),
    CONSTRAINT ck_external_report_releases_nonempty_pdf CHECK ((octet_length(pdf_bytes) > 5)),
    CONSTRAINT ck_external_report_releases_pdf_only CHECK (((format)::text = 'pdf'::text)),
    CONSTRAINT ck_external_report_releases_pdf_sha256 CHECK (((pdf_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_external_report_releases_provenance_mode CHECK (((provenance_mode)::text = ANY ((ARRAY['all-supported-sources'::character varying, 'document-only'::character varying])::text[]))),
    CONSTRAINT ck_external_report_releases_released_by CHECK ((length(TRIM(BOTH FROM released_by)) > 0))
);

CREATE SEQUENCE public.external_report_releases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.external_report_releases_id_seq OWNED BY public.external_report_releases.id;

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
    CONSTRAINT ck_failure_diagnosis_config_retention CHECK (((retention_policy)::text = 'retained_indefinitely'::text)),
    CONSTRAINT ck_failure_diagnosis_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);

CREATE SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.extraction_failure_diagnosis_configurations_id_seq OWNED BY public.extraction_failure_diagnosis_configurations.id;

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
    source_context_json jsonb NOT NULL,
    diagnosis_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_failure_diagnosis_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_failure_diagnosis_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_failure_diagnosis_request_input_sha CHECK (((input_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_failure_diagnosis_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_failure_diagnosis_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_failure_diagnosis_request_status CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'budget_exhausted'::character varying, 'timeout'::character varying, 'transport_failure'::character varying, 'validation_refused'::character varying, 'stale_input'::character varying])::text[])))
);

CREATE SEQUENCE public.extraction_failure_diagnosis_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.extraction_failure_diagnosis_requests_id_seq OWNED BY public.extraction_failure_diagnosis_requests.id;

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
    CONSTRAINT ck_extraction_measurement_case_states_kind CHECK (((kind)::text = ANY ((ARRAY['candidate_correction'::character varying, 'source_discrepancy_settlement'::character varying, 'do_not_add'::character varying, 'statement_fact_correction'::character varying, 'statement_scope_correction'::character varying])::text[]))),
    CONSTRAINT ck_extraction_measurement_case_states_source CHECK (((jsonb_typeof(source_identity_json) = 'object'::text) AND (source_identity_json ?& ARRAY['candidate_id'::text, 'extraction_run_id'::text, 'documents'::text]) AND (jsonb_typeof((source_identity_json -> 'documents'::text)) = 'array'::text) AND (jsonb_array_length((source_identity_json -> 'documents'::text)) > 0))),
    CONSTRAINT ck_extraction_measurement_case_states_state CHECK (((state)::text = ANY ((ARRAY['active'::character varying, 'reversed'::character varying])::text[])))
);

CREATE SEQUENCE public.extraction_measurement_case_states_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.extraction_measurement_case_states_id_seq OWNED BY public.extraction_measurement_case_states.id;

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
    CONSTRAINT ck_extraction_runs_completed_row_accounting CHECK (((NOT (((outcome)::text = 'completed'::text) AND ((prompt_version)::text = ANY ((ARRAY['sheet_native_v2'::character varying, 'matrix_tiered_v4'::character varying])::text[])))) OR ((row_accounting_json IS NOT NULL) AND (jsonb_array_length((row_accounting_json -> 'unaccounted_rows'::text)) = 0) AND (((row_accounting_json ->> 'accounted_row_count'::text))::integer = ((row_accounting_json ->> 'detected_row_count'::text))::integer) AND (((row_accounting_json ->> 'extracted_row_count'::text))::integer = candidate_count)))),
    CONSTRAINT ck_extraction_runs_config_receipt_shape CHECK ((((prompt_sha256 IS NULL) AND (schema_sha256 IS NULL) AND (postprocessor_sha256 IS NULL) AND (extractor_config_json IS NULL) AND (extractor_config_sha256 IS NULL) AND (token_usage_json IS NULL)) OR (((prompt_sha256 IS NOT NULL) AND (schema_sha256 IS NOT NULL) AND (postprocessor_sha256 IS NOT NULL) AND (extractor_config_json IS NOT NULL) AND (extractor_config_sha256 IS NOT NULL) AND (token_usage_json IS NOT NULL) AND ((prompt_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((schema_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((postprocessor_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND ((extractor_config_sha256)::text ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(extractor_config_json) = 'object'::text) AND (extractor_config_json ?& ARRAY['receipt_version'::text, 'extractor'::text, 'prompt_version'::text, 'model'::text, 'schema_version'::text, 'prompt_sha256'::text, 'schema_sha256'::text, 'postprocessor_sha256'::text, 'request_controls'::text, 'runtime'::text]) AND (jsonb_typeof((extractor_config_json -> 'receipt_version'::text)) = 'number'::text) AND ((extractor_config_json ->> 'receipt_version'::text) = '1'::text) AND (jsonb_typeof((extractor_config_json -> 'extractor'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM (extractor_config_json ->> 'extractor'::text))) > 0) AND (jsonb_typeof((extractor_config_json -> 'prompt_version'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'schema_version'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'prompt_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'schema_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'postprocessor_sha256'::text)) = 'string'::text) AND (jsonb_typeof((extractor_config_json -> 'request_controls'::text)) = 'object'::text) AND (jsonb_typeof((extractor_config_json -> 'runtime'::text)) = 'object'::text) AND ((extractor_config_json -> 'runtime'::text) ?& ARRAY['python_implementation'::text, 'python_version'::text, 'dependency_lock_sha256'::text, 'packages'::text]) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'python_implementation'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM ((extractor_config_json -> 'runtime'::text) ->> 'python_implementation'::text))) > 0) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'python_version'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM ((extractor_config_json -> 'runtime'::text) ->> 'python_version'::text))) > 0) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'dependency_lock_sha256'::text)) = 'string'::text) AND (((extractor_config_json -> 'runtime'::text) ->> 'dependency_lock_sha256'::text) ~ '^[0-9a-f]{64}$'::text) AND (jsonb_typeof(((extractor_config_json -> 'runtime'::text) -> 'packages'::text)) = 'object'::text) AND ((extractor_config_json ->> 'prompt_version'::text) = (prompt_version)::text) AND ((extractor_config_json ->> 'schema_version'::text) = (schema_version)::text) AND ((extractor_config_json ->> 'prompt_sha256'::text) = (prompt_sha256)::text) AND ((extractor_config_json ->> 'schema_sha256'::text) = (schema_sha256)::text) AND ((extractor_config_json ->> 'postprocessor_sha256'::text) = (postprocessor_sha256)::text) AND (((model IS NULL) AND (jsonb_typeof((extractor_config_json -> 'model'::text)) = 'null'::text)) OR ((model IS NOT NULL) AND (jsonb_typeof((extractor_config_json -> 'model'::text)) = 'string'::text) AND ((extractor_config_json ->> 'model'::text) = (model)::text))) AND (jsonb_typeof(token_usage_json) = 'object'::text) AND (token_usage_json ?& ARRAY['scope'::text, 'document_ids'::text, 'measurement'::text]) AND (jsonb_typeof((token_usage_json -> 'scope'::text)) = 'string'::text) AND (jsonb_typeof((token_usage_json -> 'measurement'::text)) = 'string'::text) AND ((token_usage_json ->> 'scope'::text) = ANY (ARRAY['run'::text, 'batch'::text])) AND (jsonb_typeof((token_usage_json -> 'document_ids'::text)) = 'array'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) > 0) AND ((((token_usage_json ->> 'scope'::text) = 'run'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) = 1)) OR (((token_usage_json ->> 'scope'::text) = 'batch'::text) AND (jsonb_array_length((token_usage_json -> 'document_ids'::text)) > 1))) AND ((((token_usage_json ->> 'measurement'::text) = 'unavailable'::text) AND (token_usage_json ? 'reason'::text) AND (jsonb_typeof((token_usage_json -> 'reason'::text)) = 'string'::text) AND (length(TRIM(BOTH FROM (token_usage_json ->> 'reason'::text))) > 0)) OR (((token_usage_json ->> 'measurement'::text) = 'exact'::text) AND (token_usage_json ?& ARRAY['prompt_tokens'::text, 'completion_tokens'::text, 'reasoning_tokens'::text, 'cached_tokens'::text]) AND (jsonb_typeof((token_usage_json -> 'prompt_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'completion_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'reasoning_tokens'::text)) = 'number'::text) AND (jsonb_typeof((token_usage_json -> 'cached_tokens'::text)) = 'number'::text) AND ((token_usage_json ->> 'prompt_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'completion_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'reasoning_tokens'::text) ~ '^[0-9]+$'::text) AND ((token_usage_json ->> 'cached_tokens'::text) ~ '^[0-9]+$'::text) AND (((token_usage_json ->> 'prompt_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'completion_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'reasoning_tokens'::text))::numeric >= (0)::numeric) AND (((token_usage_json ->> 'cached_tokens'::text))::numeric >= (0)::numeric))) AND public.extraction_token_usage_membership_is_valid(document_id, token_usage_json)) IS TRUE))),
    CONSTRAINT ck_extraction_runs_row_accounting_shape CHECK (((row_accounting_json IS NULL) OR ((jsonb_typeof(row_accounting_json) = 'object'::text) AND (row_accounting_json ?& ARRAY['schema_version'::text, 'reader_version'::text, 'reader_path'::text, 'detected_row_count'::text, 'accounted_row_count'::text, 'extracted_row_count'::text, 'blank_row_count'::text, 'skipped_row_count'::text, 'unaccounted_rows'::text, 'rows'::text]) AND ((row_accounting_json ->> 'schema_version'::text) = 'matrix-row-accounting-v1'::text) AND ((row_accounting_json ->> 'reader_version'::text) = (prompt_version)::text) AND (jsonb_typeof((row_accounting_json -> 'rows'::text)) = 'array'::text) AND (jsonb_typeof((row_accounting_json -> 'unaccounted_rows'::text)) = 'array'::text) AND ((row_accounting_json ->> 'detected_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'accounted_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'extracted_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'blank_row_count'::text) ~ '^[0-9]+$'::text) AND ((row_accounting_json ->> 'skipped_row_count'::text) ~ '^[0-9]+$'::text) AND (jsonb_array_length((row_accounting_json -> 'rows'::text)) = ((row_accounting_json ->> 'detected_row_count'::text))::integer) AND (((row_accounting_json ->> 'accounted_row_count'::text))::integer = ((((row_accounting_json ->> 'extracted_row_count'::text))::integer + ((row_accounting_json ->> 'blank_row_count'::text))::integer) + ((row_accounting_json ->> 'skipped_row_count'::text))::integer)) AND (jsonb_array_length((row_accounting_json -> 'unaccounted_rows'::text)) = (((row_accounting_json ->> 'detected_row_count'::text))::integer - ((row_accounting_json ->> 'accounted_row_count'::text))::integer))))),
    CONSTRAINT extraction_outcome CHECK (((outcome)::text = ANY ((ARRAY['completed'::character varying, 'failed'::character varying, 'unreadable'::character varying, 'no_matrix'::character varying, 'quarantined'::character varying])::text[])))
);

CREATE SEQUENCE public.extraction_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.extraction_runs_id_seq OWNED BY public.extraction_runs.id;

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

CREATE SEQUENCE public.follow_up_plan_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.follow_up_plan_receipts_id_seq OWNED BY public.follow_up_plan_receipts.id;

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

CREATE SEQUENCE public.follow_up_plan_reversals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.follow_up_plan_reversals_id_seq OWNED BY public.follow_up_plan_reversals.id;

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
    CONSTRAINT ck_inbound_message_route CHECK (((route_status)::text = ANY ((ARRAY['routed'::character varying, 'triage'::character varying])::text[]))),
    CONSTRAINT ck_inbound_message_sha256 CHECK (((raw_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.inbound_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inbound_messages_id_seq OWNED BY public.inbound_messages.id;

CREATE TABLE public.inbound_route_triage (
    id bigint NOT NULL,
    thread_id bigint NOT NULL,
    candidate_project_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    state character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    resolved_project_id bigint,
    resolved_by text,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_inbound_route_triage_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'resolved'::character varying])::text[])))
);

CREATE SEQUENCE public.inbound_route_triage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inbound_route_triage_id_seq OWNED BY public.inbound_route_triage.id;

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
    CONSTRAINT ck_inbound_thread_reading_resolution CHECK (((resolution)::text = ANY ((ARRAY['concluded'::character varying, 'unresolved'::character varying])::text[])))
);

CREATE SEQUENCE public.inbound_thread_readings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inbound_thread_readings_id_seq OWNED BY public.inbound_thread_readings.id;

CREATE TABLE public.inbound_threads (
    id bigint NOT NULL,
    project_id bigint,
    dependency_id bigint,
    bound_by_message_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.inbound_threads_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inbound_threads_id_seq OWNED BY public.inbound_threads.id;

CREATE TABLE public.intake_project_identifiers (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    kind character varying(48) NOT NULL,
    value_normalized character varying(256) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_intake_identifier_kind CHECK ((length(TRIM(BOTH FROM kind)) > 0)),
    CONSTRAINT ck_intake_identifier_value CHECK ((length(TRIM(BOTH FROM value_normalized)) > 0))
);

CREATE SEQUENCE public.intake_project_identifiers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.intake_project_identifiers_id_seq OWNED BY public.intake_project_identifiers.id;

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
    CONSTRAINT ck_key_date_draft_receipts_status CHECK (((status)::text = ANY ((ARRAY['drafted'::character varying, 'abstained'::character varying, 'failed'::character varying])::text[])))
);

CREATE SEQUENCE public.key_date_draft_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.key_date_draft_receipts_id_seq OWNED BY public.key_date_draft_receipts.id;

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

CREATE SEQUENCE public.key_date_draft_row_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.key_date_draft_row_receipts_id_seq OWNED BY public.key_date_draft_row_receipts.id;

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

CREATE SEQUENCE public.legacy_ledger_archives_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.legacy_ledger_archives_id_seq OWNED BY public.legacy_ledger_archives.id;

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

CREATE SEQUENCE public.milestone_registrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.milestone_registrations_id_seq OWNED BY public.milestone_registrations.id;

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

CREATE SEQUENCE public.milestones_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.milestones_id_seq OWNED BY public.milestones.id;

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

CREATE SEQUENCE public.operative_support_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.operative_support_id_seq OWNED BY public.operative_support.id;

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
    CONSTRAINT ck_organization_identity_activation_action CHECK (((action)::text = ANY ((ARRAY['activate'::character varying, 'suspend'::character varying])::text[]))),
    CONSTRAINT ck_organization_identity_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_organization_identity_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_organization_identity_activation_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.organization_identity_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.organization_identity_activations_id_seq OWNED BY public.organization_identity_activations.id;

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
    CONSTRAINT ck_organization_identity_receipt_method CHECK (((method)::text = ANY ((ARRAY['human_confirmation'::character varying, 'automatic_name_alias'::character varying, 'automatic_facility_class'::character varying, 'automatic_contact'::character varying, 'automatic_revision_lineage'::character varying, 'automatic_stated_alias'::character varying, 'human_cited_alias_confirmation'::character varying, 'alias_correction'::character varying])::text[]))),
    CONSTRAINT ck_organization_identity_receipt_scope CHECK (((scope)::text = 'registry'::text)),
    CONSTRAINT ck_organization_identity_receipt_wording CHECK ((length(TRIM(BOTH FROM stated_wording)) > 0))
);

CREATE SEQUENCE public.organization_identity_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.organization_identity_receipts_id_seq OWNED BY public.organization_identity_receipts.id;

CREATE TABLE public.person_identities (
    id bigint NOT NULL,
    email_normalized text NOT NULL,
    principal_subject character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_person_identity_email CHECK ((length(TRIM(BOTH FROM email_normalized)) > 0)),
    CONSTRAINT ck_person_identity_principal CHECK ((length(TRIM(BOTH FROM principal_subject)) > 0))
);

CREATE SEQUENCE public.person_identities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.person_identities_id_seq OWNED BY public.person_identities.id;

CREATE TABLE public.policy_approvals (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    family character varying(32) NOT NULL,
    policy_version character varying(64) NOT NULL,
    approved_by text NOT NULL,
    policy_json jsonb NOT NULL,
    policy_sha256 character varying(64) NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_policy_approvals_family CHECK (((family)::text = ANY ((ARRAY['automatic-carry-forward'::character varying, 'event-admission'::character varying, 'dependency-admission'::character varying])::text[]))),
    CONSTRAINT ck_policy_approvals_object CHECK ((jsonb_typeof(policy_json) = 'object'::text)),
    CONSTRAINT ck_policy_approvals_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.policy_approvals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.policy_approvals_id_seq OWNED BY public.policy_approvals.id;

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
    CONSTRAINT ck_policy_runs_family CHECK (((family)::text = ANY ((ARRAY['automatic-carry-forward'::character varying, 'event-admission'::character varying, 'dependency-admission'::character varying])::text[]))),
    CONSTRAINT ck_policy_runs_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.policy_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.policy_runs_id_seq OWNED BY public.policy_runs.id;

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
    CONSTRAINT ck_run_explanation_config_retention CHECK (((retention_policy)::text = 'retained_indefinitely'::text)),
    CONSTRAINT ck_run_explanation_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);

CREATE SEQUENCE public.production_run_explanation_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.production_run_explanation_configurations_id_seq OWNED BY public.production_run_explanation_configurations.id;

CREATE TABLE public.production_run_explanation_requests (
    id bigint NOT NULL,
    public_id character varying(36) NOT NULL,
    project_id bigint NOT NULL,
    document_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    requested_by character varying(128) NOT NULL,
    comparison_sha256 character varying(64) NOT NULL,
    state_token character varying(64) NOT NULL,
    competing_run_ids_json jsonb NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    comparison_json jsonb NOT NULL,
    explanation_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_run_explanation_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_run_explanation_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_run_explanation_request_comparison_sha CHECK (((comparison_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_run_explanation_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_run_explanation_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_run_explanation_request_status CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'budget_exhausted'::character varying, 'timeout'::character varying, 'transport_failure'::character varying, 'validation_refused'::character varying, 'stale_input'::character varying])::text[])))
);

CREATE SEQUENCE public.production_run_explanation_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.production_run_explanation_requests_id_seq OWNED BY public.production_run_explanation_requests.id;

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

CREATE SEQUENCE public.project_check_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.project_check_configurations_id_seq OWNED BY public.project_check_configurations.id;

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

CREATE SEQUENCE public.project_roster_entries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.project_roster_entries_id_seq OWNED BY public.project_roster_entries.id;

CREATE TABLE public.projects (
    id bigint NOT NULL,
    slug character varying(64) NOT NULL,
    name text NOT NULL,
    agency text,
    is_synthetic boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    project_side_parties jsonb DEFAULT '[]'::jsonb NOT NULL
);

CREATE SEQUENCE public.projects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;

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

CREATE TABLE public.report_runs (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    ruleset_version character varying(32) NOT NULL,
    snapshot_json jsonb NOT NULL,
    output_path text,
    document_only boolean DEFAULT false NOT NULL
);

CREATE SEQUENCE public.report_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.report_runs_id_seq OWNED BY public.report_runs.id;

CREATE TABLE public.retired_automatic_carry_forward_policy_activations (
    project_id bigint NOT NULL,
    policy_approval_id bigint NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    family character varying(32) DEFAULT 'automatic-carry-forward'::character varying NOT NULL,
    CONSTRAINT ck_active_automatic_carry_forward_policy_family CHECK (((family)::text = 'automatic-carry-forward'::text))
);

CREATE TABLE public.retired_dependency_statuses (
    dependency_id bigint NOT NULL,
    status character varying(32) NOT NULL,
    retired_at timestamp with time zone DEFAULT now() NOT NULL
);

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
    CONSTRAINT ck_rev_change_expl_cfg_retention CHECK (((retention_policy)::text = 'retained_indefinitely'::text)),
    CONSTRAINT ck_rev_change_expl_cfg_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);

CREATE SEQUENCE public.revision_change_explanation_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.revision_change_explanation_configurations_id_seq OWNED BY public.revision_change_explanation_configurations.id;

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
    comparison_json jsonb NOT NULL,
    explanation_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_rev_change_expl_req_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_rev_change_expl_req_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_rev_change_expl_req_comparison_sha CHECK (((comparison_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_rev_change_expl_req_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_rev_change_expl_req_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_rev_change_expl_req_status CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'budget_exhausted'::character varying, 'timeout'::character varying, 'transport_failure'::character varying, 'validation_refused'::character varying, 'stale_input'::character varying])::text[])))
);

CREATE SEQUENCE public.revision_change_explanation_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.revision_change_explanation_requests_id_seq OWNED BY public.revision_change_explanation_requests.id;

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
    CONSTRAINT ck_revision_comparison_finding_shape CHECK (((((state)::text = 'added'::text) AND (cardinality(predecessor_candidate_ids) = 0) AND (cardinality(successor_candidate_ids) = 1)) OR (((state)::text = 'dropped'::text) AND (cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 0)) OR (((state)::text = 'unmatched'::text) AND (((cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 0)) OR ((cardinality(predecessor_candidate_ids) = 0) AND (cardinality(successor_candidate_ids) = 1)))) OR (((state)::text = ANY ((ARRAY['unchanged'::character varying, 'changed'::character varying])::text[])) AND (cardinality(predecessor_candidate_ids) = 1) AND (cardinality(successor_candidate_ids) = 1)) OR (((state)::text = 'ambiguous'::text) AND (cardinality(predecessor_candidate_ids) > 0) AND (cardinality(successor_candidate_ids) > 0)))),
    CONSTRAINT ck_revision_comparison_match_score CHECK (((match_score IS NULL) OR ((match_score >= (0)::double precision) AND (match_score <= (1)::double precision)))),
    CONSTRAINT revision_comparison_state CHECK (((state)::text = ANY ((ARRAY['added'::character varying, 'dropped'::character varying, 'unchanged'::character varying, 'changed'::character varying, 'ambiguous'::character varying, 'unmatched'::character varying])::text[])))
);

CREATE SEQUENCE public.revision_comparison_findings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.revision_comparison_findings_id_seq OWNED BY public.revision_comparison_findings.id;

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

CREATE SEQUENCE public.revision_comparison_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.revision_comparison_runs_id_seq OWNED BY public.revision_comparison_runs.id;

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
    CONSTRAINT ck_schedule_governing_method CHECK (((method)::text = ANY ((ARRAY['coded'::character varying, 'awaiting_pick'::character varying, 'human_pick'::character varying])::text[]))),
    CONSTRAINT ck_schedule_governing_recorded_by CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_schedule_governing_sha256 CHECK (((source_sha256 IS NULL) OR ((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);

CREATE SEQUENCE public.schedule_governing_derivations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.schedule_governing_derivations_id_seq OWNED BY public.schedule_governing_derivations.id;

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
    CONSTRAINT ck_schedule_link_activation_action CHECK (((action)::text = ANY ((ARRAY['activate'::character varying, 'suspend'::character varying])::text[]))),
    CONSTRAINT ck_schedule_link_activation_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_schedule_link_activation_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_schedule_link_activation_sha256 CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.schedule_link_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.schedule_link_activations_id_seq OWNED BY public.schedule_link_activations.id;

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
    CONSTRAINT ck_schedule_link_receipts_basis CHECK (((basis)::text = ANY ((ARRAY['exact_station_containment'::character varying, 'human_choice'::character varying, 'flow_through'::character varying])::text[]))),
    CONSTRAINT ck_schedule_link_receipts_decided_by CHECK ((length(TRIM(BOTH FROM decided_by)) > 0)),
    CONSTRAINT ck_schedule_link_receipts_sha256 CHECK (((policy_sha256 IS NULL) OR ((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_schedule_link_receipts_values CHECK ((jsonb_typeof(deciding_values_json) = 'object'::text))
);

CREATE SEQUENCE public.schedule_link_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.schedule_link_receipts_id_seq OWNED BY public.schedule_link_receipts.id;

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
    CONSTRAINT ck_scheduled_report_publication_provenance_mode CHECK (((provenance_mode)::text = ANY ((ARRAY['all-supported-sources'::character varying, 'document-only'::character varying])::text[]))),
    CONSTRAINT ck_scheduled_report_publication_snapshot_object CHECK ((jsonb_typeof(snapshot_json) = 'object'::text)),
    CONSTRAINT ck_scheduled_report_publication_thresholds_object CHECK ((jsonb_typeof(thresholds_json) = 'object'::text)),
    CONSTRAINT ck_scheduled_report_publication_window_days CHECK (((window_start IS NULL) = (comparison_window_days IS NULL))),
    CONSTRAINT ck_scheduled_report_publication_window_nonneg CHECK (((comparison_window_days IS NULL) OR (comparison_window_days >= 0)))
);

CREATE SEQUENCE public.scheduled_report_publications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.scheduled_report_publications_id_seq OWNED BY public.scheduled_report_publications.id;

CREATE TABLE public.sign_in_attempts (
    id bigint NOT NULL,
    scope_kind character varying(32) NOT NULL,
    scope_value text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.sign_in_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sign_in_attempts_id_seq OWNED BY public.sign_in_attempts.id;

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

CREATE SEQUENCE public.sign_in_tokens_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sign_in_tokens_id_seq OWNED BY public.sign_in_tokens.id;

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
    CONSTRAINT source_fetch_outcome CHECK (((outcome)::text = ANY ((ARRAY['registered'::character varying, 'unchanged'::character varying, 'drift'::character varying, 'failed'::character varying, 'budget_exhausted'::character varying])::text[])))
);

CREATE SEQUENCE public.source_fetch_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.source_fetch_attempts_id_seq OWNED BY public.source_fetch_attempts.id;

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
    CONSTRAINT ck_intake_draft_config_retention CHECK (((retention_policy)::text = 'retained_indefinitely'::text)),
    CONSTRAINT ck_intake_draft_config_timeout CHECK (((timeout_seconds >= 1) AND (timeout_seconds <= 600)))
);

CREATE SEQUENCE public.source_intake_draft_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.source_intake_draft_configurations_id_seq OWNED BY public.source_intake_draft_configurations.id;

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
    permitted_pages_json jsonb NOT NULL,
    model character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    adapter character varying(64) NOT NULL,
    adapter_contract_version character varying(128),
    tool_contract_version character varying(128) NOT NULL,
    validator_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    reason text,
    source_json jsonb NOT NULL,
    proposals_json jsonb,
    execution_lineage_json jsonb,
    read_fingerprint character varying(64),
    budget_json jsonb NOT NULL,
    usage_json jsonb NOT NULL,
    non_authoritative boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_intake_draft_request_actor CHECK ((length(TRIM(BOTH FROM requested_by)) > 0)),
    CONSTRAINT ck_intake_draft_request_adapter CHECK ((length(TRIM(BOTH FROM adapter)) > 0)),
    CONSTRAINT ck_intake_draft_request_non_auth CHECK (non_authoritative),
    CONSTRAINT ck_intake_draft_request_source_sha CHECK (((source_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_staged_sha CHECK (((staged_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_state_token CHECK (((state_token)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_intake_draft_request_status CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'budget_exhausted'::character varying, 'timeout'::character varying, 'transport_failure'::character varying, 'validation_refused'::character varying, 'stale_input'::character varying])::text[])))
);

CREATE SEQUENCE public.source_intake_draft_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.source_intake_draft_requests_id_seq OWNED BY public.source_intake_draft_requests.id;

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

CREATE SEQUENCE public.statement_coordination_receipts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_coordination_receipts_id_seq OWNED BY public.statement_coordination_receipts.id;

CREATE TABLE public.statement_coordination_reversal_effects (
    id bigint NOT NULL,
    reversal_id bigint NOT NULL,
    effect_kind character varying(32) NOT NULL,
    target_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_coordination_reversal_effects_kind CHECK (((effect_kind)::text = ANY ((ARRAY['statement'::character varying, 'scope_decision'::character varying, 'work_decision'::character varying, 'milestone_link'::character varying, 'candidate_disposition'::character varying, 'candidate_projection'::character varying, 'lineage_projection'::character varying, 'audit_pointer'::character varying, 'grouping_receipt'::character varying])::text[])))
);

CREATE SEQUENCE public.statement_coordination_reversal_effects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_coordination_reversal_effects_id_seq OWNED BY public.statement_coordination_reversal_effects.id;

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

CREATE SEQUENCE public.statement_coordination_reversals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_coordination_reversals_id_seq OWNED BY public.statement_coordination_reversals.id;

CREATE TABLE public.statement_suggestion_eligibility_declarations (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    contract_version character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_suggestion_eligibility_contract CHECK ((length(TRIM(BOTH FROM contract_version)) > 0))
);

CREATE SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_suggestion_eligibility_declarations_id_seq OWNED BY public.statement_suggestion_eligibility_declarations.id;

CREATE TABLE public.statement_suggestion_protection_ends (
    id bigint NOT NULL,
    protection_id bigint NOT NULL,
    ended_at timestamp with time zone NOT NULL
);

CREATE SEQUENCE public.statement_suggestion_protection_ends_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_suggestion_protection_ends_id_seq OWNED BY public.statement_suggestion_protection_ends.id;

CREATE TABLE public.statement_suggestion_protections (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    candidate_id bigint NOT NULL,
    kind character varying(32) NOT NULL,
    observation_contract character varying(128) NOT NULL,
    declared_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_statement_suggestion_protection_contract CHECK ((length(TRIM(BOTH FROM observation_contract)) > 0)),
    CONSTRAINT ck_statement_suggestion_protection_kind CHECK (((kind)::text = ANY ((ARRAY['shadow_cohort'::character varying, 'no_agent_baseline'::character varying])::text[])))
);

CREATE SEQUENCE public.statement_suggestion_protections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.statement_suggestion_protections_id_seq OWNED BY public.statement_suggestion_protections.id;

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
    CONSTRAINT ck_unreadable_cell_admission_action CHECK (((action)::text = ANY ((ARRAY['activate'::character varying, 'suspend'::character varying])::text[]))),
    CONSTRAINT ck_unreadable_cell_admission_actor CHECK ((length(TRIM(BOTH FROM recorded_by)) > 0)),
    CONSTRAINT ck_unreadable_cell_admission_reason CHECK ((length(TRIM(BOTH FROM reason)) > 0)),
    CONSTRAINT ck_unreadable_cell_admission_sha CHECK (((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE SEQUENCE public.unreadable_cell_admission_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.unreadable_cell_admission_activations_id_seq OWNED BY public.unreadable_cell_admission_activations.id;

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

CREATE SEQUENCE public.unreadable_cell_reading_profiles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.unreadable_cell_reading_profiles_id_seq OWNED BY public.unreadable_cell_reading_profiles.id;

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
    CONSTRAINT ck_unreadable_cell_run_state CHECK (((terminal_state)::text = ANY ((ARRAY['rescued'::character varying, 'corroborated'::character varying, 'reading_only'::character varying, 'failure'::character varying, 'stale_input'::character varying, 'budget_exhausted'::character varying, 'refused'::character varying, 'validation_refused'::character varying, 'runtime_failure'::character varying])::text[])))
);

CREATE SEQUENCE public.unreadable_cell_reading_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.unreadable_cell_reading_runs_id_seq OWNED BY public.unreadable_cell_reading_runs.id;

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
    CONSTRAINT ck_unreadable_cell_step_type CHECK (((step_type)::text = ANY ((ARRAY['image_op'::character varying, 'read'::character varying, 'corpus_read'::character varying])::text[])))
);

CREATE SEQUENCE public.unreadable_cell_reading_steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.unreadable_cell_reading_steps_id_seq OWNED BY public.unreadable_cell_reading_steps.id;

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
    CONSTRAINT ck_unreadable_cell_resolution_origin CHECK (((origin)::text = ANY ((ARRAY['harness'::character varying, 'corroboration_upgrade'::character varying, 'admission'::character varying, 'human_decision'::character varying])::text[]))),
    CONSTRAINT ck_unreadable_cell_resolution_sha CHECK (((policy_sha256 IS NULL) OR ((policy_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_unreadable_cell_resolution_state CHECK (((state)::text = ANY ((ARRAY['unconfirmed'::character varying, 'corroborated'::character varying, 'absent'::character varying, 'admitted'::character varying])::text[])))
);

CREATE SEQUENCE public.unreadable_cell_resolutions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.unreadable_cell_resolutions_id_seq OWNED BY public.unreadable_cell_resolutions.id;

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

CREATE SEQUENCE public.web_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.web_sessions_id_seq OWNED BY public.web_sessions.id;

CREATE TABLE public.work_decision_milestone_impacts (
    id bigint NOT NULL,
    work_decision_id bigint NOT NULL,
    milestone_id bigint NOT NULL
);

CREATE SEQUENCE public.work_decision_milestone_impacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.work_decision_milestone_impacts_id_seq OWNED BY public.work_decision_milestone_impacts.id;

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
    CONSTRAINT ck_work_decisions_deferral_shape CHECK (((((field)::text <> 'deferral'::text) AND (deferral_reason IS NULL) AND (deferral_return_date IS NULL)) OR (((field)::text = 'deferral'::text) AND (((after_value IS NULL) AND (deferral_reason IS NULL) AND (deferral_return_date IS NULL)) OR ((after_value IS NOT NULL) AND ((deferral_reason)::text = ANY ((ARRAY['waiting_for_information'::character varying, 'waiting_for_external_party'::character varying, 'assigned_to_someone_else'::character varying])::text[])) AND (deferral_return_date IS NOT NULL)))))),
    CONSTRAINT ck_work_decisions_exactly_one_subject CHECK ((((dependency_id IS NOT NULL) AND (commitment_lineage_id IS NULL)) OR ((dependency_id IS NULL) AND (commitment_lineage_id IS NOT NULL)))),
    CONSTRAINT ck_work_decisions_field CHECK (((field)::text = ANY ((ARRAY['internal_owner'::character varying, 'next_action'::character varying, 'milestone_impact'::character varying, 'deferral'::character varying])::text[])))
);

CREATE SEQUENCE public.work_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.work_decisions_id_seq OWNED BY public.work_decisions.id;

ALTER TABLE ONLY public.active_run_declarations ALTER COLUMN id SET DEFAULT nextval('public.active_run_declarations_id_seq'::regclass);

ALTER TABLE ONLY public.assertions ALTER COLUMN id SET DEFAULT nextval('public.assertions_id_seq'::regclass);

ALTER TABLE ONLY public.assignment_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.assignment_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_dispatches_id_seq'::regclass);

ALTER TABLE ONLY public.assignment_notification_feedback ALTER COLUMN id SET DEFAULT nextval('public.assignment_notification_feedback_id_seq'::regclass);

ALTER TABLE ONLY public.assignment_notifications ALTER COLUMN id SET DEFAULT nextval('public.assignment_notifications_id_seq'::regclass);

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes ALTER COLUMN id SET DEFAULT nextval('public.automatic_carry_forward_outcomes_id_seq'::regclass);

ALTER TABLE ONLY public.candidate_dispositions ALTER COLUMN id SET DEFAULT nextval('public.candidate_dispositions_id_seq'::regclass);

ALTER TABLE ONLY public.candidates ALTER COLUMN id SET DEFAULT nextval('public.candidates_id_seq'::regclass);

ALTER TABLE ONLY public.cohort_receipts ALTER COLUMN id SET DEFAULT nextval('public.cohort_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.commitment_lineages ALTER COLUMN id SET DEFAULT nextval('public.commitment_lineages_id_seq'::regclass);

ALTER TABLE ONLY public.condition_resolutions ALTER COLUMN id SET DEFAULT nextval('public.condition_resolutions_id_seq'::regclass);

ALTER TABLE ONLY public.coordination_summary_configurations ALTER COLUMN id SET DEFAULT nextval('public.coordination_summary_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.coordination_summary_requests ALTER COLUMN id SET DEFAULT nextval('public.coordination_summary_requests_id_seq'::regclass);

ALTER TABLE ONLY public.dependencies ALTER COLUMN id SET DEFAULT nextval('public.dependencies_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_admission_outcomes ALTER COLUMN id SET DEFAULT nextval('public.dependency_admission_outcomes_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_dismissals ALTER COLUMN id SET DEFAULT nextval('public.dependency_dismissals_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_event_scope_decisions ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_scope_decisions_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_event_scopes ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_scopes_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_event_timings ALTER COLUMN id SET DEFAULT nextval('public.dependency_event_timings_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_events ALTER COLUMN id SET DEFAULT nextval('public.dependency_events_id_seq'::regclass);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies ALTER COLUMN id SET DEFAULT nextval('public.dependency_evidence_sufficiencies_id_seq'::regclass);

ALTER TABLE ONLY public.discovered_references ALTER COLUMN id SET DEFAULT nextval('public.discovered_references_id_seq'::regclass);

ALTER TABLE ONLY public.dispute_history_resolutions ALTER COLUMN id SET DEFAULT nextval('public.dispute_history_resolutions_id_seq'::regclass);

ALTER TABLE ONLY public.dispute_settlements ALTER COLUMN id SET DEFAULT nextval('public.dispute_settlements_id_seq'::regclass);

ALTER TABLE ONLY public.doc_pages ALTER COLUMN id SET DEFAULT nextval('public.doc_pages_id_seq'::regclass);

ALTER TABLE ONLY public.document_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.document_notification_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.document_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.document_notification_dispatches_id_seq'::regclass);

ALTER TABLE ONLY public.document_notifications ALTER COLUMN id SET DEFAULT nextval('public.document_notifications_id_seq'::regclass);

ALTER TABLE ONLY public.document_rendition_derivations ALTER COLUMN id SET DEFAULT nextval('public.document_rendition_derivations_id_seq'::regclass);

ALTER TABLE ONLY public.documentation_field_confirmations ALTER COLUMN id SET DEFAULT nextval('public.documentation_field_confirmations_id_seq'::regclass);

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);

ALTER TABLE ONLY public.due_action_notification_attempts ALTER COLUMN id SET DEFAULT nextval('public.due_action_notification_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.due_action_notification_dispatches ALTER COLUMN id SET DEFAULT nextval('public.due_action_notification_dispatches_id_seq'::regclass);

ALTER TABLE ONLY public.due_action_notifications ALTER COLUMN id SET DEFAULT nextval('public.due_action_notifications_id_seq'::regclass);

ALTER TABLE ONLY public.due_work_occurrences ALTER COLUMN id SET DEFAULT nextval('public.due_work_occurrences_id_seq'::regclass);

ALTER TABLE ONLY public.due_work_receipts ALTER COLUMN id SET DEFAULT nextval('public.due_work_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.due_work_schedules ALTER COLUMN id SET DEFAULT nextval('public.due_work_schedules_id_seq'::regclass);

ALTER TABLE ONLY public.event_admission_acceptance_receipts ALTER COLUMN id SET DEFAULT nextval('public.event_admission_acceptance_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.event_admission_activations ALTER COLUMN id SET DEFAULT nextval('public.event_admission_activations_id_seq'::regclass);

ALTER TABLE ONLY public.event_admission_outcomes ALTER COLUMN id SET DEFAULT nextval('public.event_admission_outcomes_id_seq'::regclass);

ALTER TABLE ONLY public.event_cohort_receipts ALTER COLUMN id SET DEFAULT nextval('public.event_cohort_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_candidate_review_starts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_capture_contracts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_capture_contracts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_capture_results ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_capture_results_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_evaluation_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_packet_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_packet_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_review_observations ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_review_observations_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_runs ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_runs_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_cases_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_executions_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_shadow_outcomes_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_investigation_step_receipts ALTER COLUMN id SET DEFAULT nextval('public.evidence_investigation_step_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.evidence_links ALTER COLUMN id SET DEFAULT nextval('public.evidence_links_id_seq'::regclass);

ALTER TABLE ONLY public.external_orgs ALTER COLUMN id SET DEFAULT nextval('public.external_orgs_id_seq'::regclass);

ALTER TABLE ONLY public.external_report_artifacts ALTER COLUMN id SET DEFAULT nextval('public.external_report_artifacts_id_seq'::regclass);

ALTER TABLE ONLY public.external_report_releases ALTER COLUMN id SET DEFAULT nextval('public.external_report_releases_id_seq'::regclass);

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations ALTER COLUMN id SET DEFAULT nextval('public.extraction_failure_diagnosis_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests ALTER COLUMN id SET DEFAULT nextval('public.extraction_failure_diagnosis_requests_id_seq'::regclass);

ALTER TABLE ONLY public.extraction_measurement_case_states ALTER COLUMN id SET DEFAULT nextval('public.extraction_measurement_case_states_id_seq'::regclass);

ALTER TABLE ONLY public.extraction_runs ALTER COLUMN id SET DEFAULT nextval('public.extraction_runs_id_seq'::regclass);

ALTER TABLE ONLY public.follow_up_plan_receipts ALTER COLUMN id SET DEFAULT nextval('public.follow_up_plan_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.follow_up_plan_reversals ALTER COLUMN id SET DEFAULT nextval('public.follow_up_plan_reversals_id_seq'::regclass);

ALTER TABLE ONLY public.inbound_messages ALTER COLUMN id SET DEFAULT nextval('public.inbound_messages_id_seq'::regclass);

ALTER TABLE ONLY public.inbound_route_triage ALTER COLUMN id SET DEFAULT nextval('public.inbound_route_triage_id_seq'::regclass);

ALTER TABLE ONLY public.inbound_thread_readings ALTER COLUMN id SET DEFAULT nextval('public.inbound_thread_readings_id_seq'::regclass);

ALTER TABLE ONLY public.inbound_threads ALTER COLUMN id SET DEFAULT nextval('public.inbound_threads_id_seq'::regclass);

ALTER TABLE ONLY public.intake_project_identifiers ALTER COLUMN id SET DEFAULT nextval('public.intake_project_identifiers_id_seq'::regclass);

ALTER TABLE ONLY public.key_date_draft_receipts ALTER COLUMN id SET DEFAULT nextval('public.key_date_draft_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.key_date_draft_row_receipts ALTER COLUMN id SET DEFAULT nextval('public.key_date_draft_row_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.legacy_ledger_archives ALTER COLUMN id SET DEFAULT nextval('public.legacy_ledger_archives_id_seq'::regclass);

ALTER TABLE ONLY public.milestone_registrations ALTER COLUMN id SET DEFAULT nextval('public.milestone_registrations_id_seq'::regclass);

ALTER TABLE ONLY public.milestones ALTER COLUMN id SET DEFAULT nextval('public.milestones_id_seq'::regclass);

ALTER TABLE ONLY public.operative_support ALTER COLUMN id SET DEFAULT nextval('public.operative_support_id_seq'::regclass);

ALTER TABLE ONLY public.organization_identity_activations ALTER COLUMN id SET DEFAULT nextval('public.organization_identity_activations_id_seq'::regclass);

ALTER TABLE ONLY public.organization_identity_receipts ALTER COLUMN id SET DEFAULT nextval('public.organization_identity_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.person_identities ALTER COLUMN id SET DEFAULT nextval('public.person_identities_id_seq'::regclass);

ALTER TABLE ONLY public.policy_approvals ALTER COLUMN id SET DEFAULT nextval('public.policy_approvals_id_seq'::regclass);

ALTER TABLE ONLY public.policy_runs ALTER COLUMN id SET DEFAULT nextval('public.policy_runs_id_seq'::regclass);

ALTER TABLE ONLY public.production_run_explanation_configurations ALTER COLUMN id SET DEFAULT nextval('public.production_run_explanation_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.production_run_explanation_requests ALTER COLUMN id SET DEFAULT nextval('public.production_run_explanation_requests_id_seq'::regclass);

ALTER TABLE ONLY public.project_check_configurations ALTER COLUMN id SET DEFAULT nextval('public.project_check_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.project_roster_entries ALTER COLUMN id SET DEFAULT nextval('public.project_roster_entries_id_seq'::regclass);

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);

ALTER TABLE ONLY public.report_runs ALTER COLUMN id SET DEFAULT nextval('public.report_runs_id_seq'::regclass);

ALTER TABLE ONLY public.revision_change_explanation_configurations ALTER COLUMN id SET DEFAULT nextval('public.revision_change_explanation_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.revision_change_explanation_requests ALTER COLUMN id SET DEFAULT nextval('public.revision_change_explanation_requests_id_seq'::regclass);

ALTER TABLE ONLY public.revision_comparison_findings ALTER COLUMN id SET DEFAULT nextval('public.revision_comparison_findings_id_seq'::regclass);

ALTER TABLE ONLY public.revision_comparison_runs ALTER COLUMN id SET DEFAULT nextval('public.revision_comparison_runs_id_seq'::regclass);

ALTER TABLE ONLY public.schedule_governing_derivations ALTER COLUMN id SET DEFAULT nextval('public.schedule_governing_derivations_id_seq'::regclass);

ALTER TABLE ONLY public.schedule_link_activations ALTER COLUMN id SET DEFAULT nextval('public.schedule_link_activations_id_seq'::regclass);

ALTER TABLE ONLY public.schedule_link_receipts ALTER COLUMN id SET DEFAULT nextval('public.schedule_link_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.scheduled_report_publications ALTER COLUMN id SET DEFAULT nextval('public.scheduled_report_publications_id_seq'::regclass);

ALTER TABLE ONLY public.sign_in_attempts ALTER COLUMN id SET DEFAULT nextval('public.sign_in_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.sign_in_tokens ALTER COLUMN id SET DEFAULT nextval('public.sign_in_tokens_id_seq'::regclass);

ALTER TABLE ONLY public.source_fetch_attempts ALTER COLUMN id SET DEFAULT nextval('public.source_fetch_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.source_intake_draft_configurations ALTER COLUMN id SET DEFAULT nextval('public.source_intake_draft_configurations_id_seq'::regclass);

ALTER TABLE ONLY public.source_intake_draft_requests ALTER COLUMN id SET DEFAULT nextval('public.source_intake_draft_requests_id_seq'::regclass);

ALTER TABLE ONLY public.statement_coordination_receipts ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_receipts_id_seq'::regclass);

ALTER TABLE ONLY public.statement_coordination_reversal_effects ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_reversal_effects_id_seq'::regclass);

ALTER TABLE ONLY public.statement_coordination_reversals ALTER COLUMN id SET DEFAULT nextval('public.statement_coordination_reversals_id_seq'::regclass);

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_eligibility_declarations_id_seq'::regclass);

ALTER TABLE ONLY public.statement_suggestion_protection_ends ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_protection_ends_id_seq'::regclass);

ALTER TABLE ONLY public.statement_suggestion_protections ALTER COLUMN id SET DEFAULT nextval('public.statement_suggestion_protections_id_seq'::regclass);

ALTER TABLE ONLY public.unreadable_cell_admission_activations ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_admission_activations_id_seq'::regclass);

ALTER TABLE ONLY public.unreadable_cell_reading_profiles ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_profiles_id_seq'::regclass);

ALTER TABLE ONLY public.unreadable_cell_reading_runs ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_runs_id_seq'::regclass);

ALTER TABLE ONLY public.unreadable_cell_reading_steps ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_reading_steps_id_seq'::regclass);

ALTER TABLE ONLY public.unreadable_cell_resolutions ALTER COLUMN id SET DEFAULT nextval('public.unreadable_cell_resolutions_id_seq'::regclass);

ALTER TABLE ONLY public.web_sessions ALTER COLUMN id SET DEFAULT nextval('public.web_sessions_id_seq'::regclass);

ALTER TABLE ONLY public.work_decision_milestone_impacts ALTER COLUMN id SET DEFAULT nextval('public.work_decision_milestone_impacts_id_seq'::regclass);

ALTER TABLE ONLY public.work_decisions ALTER COLUMN id SET DEFAULT nextval('public.work_decisions_id_seq'::regclass);

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_pkey PRIMARY KEY (project_id);

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_policy_approval_id_key UNIQUE (policy_approval_id);

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_extraction_run_id_key UNIQUE (extraction_run_id);

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_pkey PRIMARY KEY (document_id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_notification_id_key UNIQUE (notification_id);

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_audit_log_id_key UNIQUE (audit_log_id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_occurrence_key_key UNIQUE (occurrence_key);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_pkey PRIMARY KEY (audit_log_id);

ALTER TABLE ONLY public.candidate_dispositions
    ADD CONSTRAINT candidate_dispositions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_pkey PRIMARY KEY (id);

ALTER TABLE public.external_report_releases
    ADD CONSTRAINT ck_external_report_releases_released_by_display CHECK (((released_by_display IS NOT NULL) AND (length(TRIM(BOTH FROM released_by_display)) > 0))) NOT VALID;

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.commitment_lineages
    ADD CONSTRAINT commitment_lineages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT condition_resolutions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.coordination_summary_configurations
    ADD CONSTRAINT coordination_summary_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_project_id_ref_code_key UNIQUE (project_id, ref_code);

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_dismissals
    ADD CONSTRAINT dependency_dismissals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_pkey PRIMARY KEY (evidence_link_id);

ALTER TABLE ONLY public.dependency_event_migration_receipts
    ADD CONSTRAINT dependency_event_migration_receipts_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT dependency_event_timings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT dependency_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT discovered_references_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dispute_settlements
    ADD CONSTRAINT dispute_settlements_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_document_id_page_no_key UNIQUE (document_id, page_no);

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_notification_id_key UNIQUE (notification_id);

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_occurrence_key_key UNIQUE (occurrence_key);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.document_quarantines
    ADD CONSTRAINT document_quarantines_pkey PRIMARY KEY (document_id);

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_derived_document_id_key UNIQUE (derived_document_id);

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT documentation_field_confirmations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_project_id_sha256_key UNIQUE (project_id, sha256);

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_notification_id_key UNIQUE (notification_id);

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_occurrence_key_key UNIQUE (occurrence_key);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_occurrence_key_key UNIQUE (occurrence_key);

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_attempt_id_key UNIQUE (attempt_id);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.event_admission_acceptance_receipts
    ADD CONSTRAINT event_admission_acceptance_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT event_cohort_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_candidate_id_key UNIQUE (candidate_id);

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_contract_sha256_key UNIQUE (contract_sha256);

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts
    ADD CONSTRAINT evidence_investigation_evaluation_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_evaluation_receipts
    ADD CONSTRAINT evidence_investigation_evaluation_receipts_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_run_id_key UNIQUE (run_id);

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT evidence_investigation_review_observations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_run_id_key UNIQUE (run_id);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_shadow_case_id_key UNIQUE (shadow_case_id);

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_shadow_case_id_key UNIQUE (shadow_case_id);

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT evidence_investigation_step_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.external_orgs
    ADD CONSTRAINT external_orgs_name_key UNIQUE (name);

ALTER TABLE ONLY public.external_orgs
    ADD CONSTRAINT external_orgs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.external_report_artifacts
    ADD CONSTRAINT external_report_artifacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT external_report_releases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations
    ADD CONSTRAINT extraction_failure_diagnosis_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_predecessor_state_id_key UNIQUE (predecessor_state_id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_audit_log_id_key UNIQUE (audit_log_id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_decision_id_key UNIQUE (internal_owner_decision_id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_next_action_decision_id_key UNIQUE (next_action_decision_id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_resumed_deferral_decision_id_key UNIQUE (resumed_deferral_decision_id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_audit_log_id_key UNIQUE (audit_log_id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_deferral_reversal_decision_id_key UNIQUE (deferral_reversal_decision_id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_internal_owner_reversal_decision_i_key UNIQUE (internal_owner_reversal_decision_id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_next_action_reversal_decision_id_key UNIQUE (next_action_reversal_decision_id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_receipt_id_key UNIQUE (receipt_id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_raw_sha256_key UNIQUE (raw_sha256);

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_thread_id_key UNIQUE (thread_id);

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_thread_id_closing_message_id_key UNIQUE (thread_id, closing_message_id);

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_kind_value_normalized_project_id_key UNIQUE (kind, value_normalized, project_id);

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.key_date_draft_receipts
    ADD CONSTRAINT key_date_draft_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT key_date_draft_row_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_project_id_key UNIQUE (project_id);

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_project_id_code_key UNIQUE (project_id, code);

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT operative_support_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.organization_identity_activations
    ADD CONSTRAINT organization_identity_activations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT person_identities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT policy_approvals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.production_run_explanation_configurations
    ADD CONSTRAINT production_run_explanation_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.project_check_configurations
    ADD CONSTRAINT project_check_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT project_roster_entries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_slug_key UNIQUE (slug);

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_pkey PRIMARY KEY (audit_log_id);

ALTER TABLE ONLY public.record_inclusion_requests
    ADD CONSTRAINT record_inclusion_requests_pkey PRIMARY KEY (project_id);

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.retired_dependency_statuses
    ADD CONSTRAINT retired_dependency_statuses_pkey PRIMARY KEY (dependency_id);

ALTER TABLE ONLY public.revision_change_explanation_configurations
    ADD CONSTRAINT revision_change_explanation_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_revision_comparison_run_id_ord_key UNIQUE (revision_comparison_run_id, ordinal);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT revision_comparison_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.revision_reconciliation_requests
    ADD CONSTRAINT revision_reconciliation_requests_pkey PRIMARY KEY (project_id);

ALTER TABLE ONLY public.schedule_governing_derivations
    ADD CONSTRAINT schedule_governing_derivations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.schedule_link_activations
    ADD CONSTRAINT schedule_link_activations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.sign_in_attempts
    ADD CONSTRAINT sign_in_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sign_in_tokens
    ADD CONSTRAINT sign_in_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_fetch_attempts
    ADD CONSTRAINT source_fetch_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_intake_draft_configurations
    ADD CONSTRAINT source_intake_draft_configurations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipt_milestone_impact_decision_id_key UNIQUE (milestone_impact_decision_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_audit_log_id_key UNIQUE (audit_log_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_dependency_event_id_key UNIQUE (dependency_event_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_internal_owner_decision_id_key UNIQUE (internal_owner_decision_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_next_action_decision_id_key UNIQUE (next_action_decision_id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_scope_decision_id_key UNIQUE (scope_decision_id);

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT statement_coordination_reversal_effects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_audit_log_id_key UNIQUE (audit_log_id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_disposition_id_key UNIQUE (candidate_disposition_id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_receipt_id_key UNIQUE (receipt_id);

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT statement_suggestion_protection_ends_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.unreadable_cell_admission_activations
    ADD CONSTRAINT unreadable_cell_admission_activations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.unreadable_cell_reading_profiles
    ADD CONSTRAINT unreadable_cell_reading_profiles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_public_id_key UNIQUE (public_id);

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT unreadable_cell_reading_steps_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT uq_active_run_declarations_document_id_id UNIQUE (document_id, id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT uq_active_run_declarations_predecessor UNIQUE (predecessor_declaration_id);

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT uq_assignment_attempt_number UNIQUE (dispatch_id, attempt_number);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT uq_assignment_feedback_person UNIQUE (notification_id, flagged_by);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT uq_automatic_carry_forward_dependency_successor UNIQUE (dependency_id, successor_candidate_id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT uq_automatic_carry_forward_new_evidence UNIQUE (new_evidence_link_id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT uq_automatic_carry_forward_outcome_project_id UNIQUE (project_id, id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT uq_automatic_carry_forward_outcome_receipt_audit UNIQUE (receipt_audit_log_id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT uq_capture_result_case UNIQUE (capture_contract_id, shadow_case_id);

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT uq_cohort_receipts_one_per_rule UNIQUE (revision_comparison_run_id, rule_version, external_org);

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT uq_dependency_event_scope_decision_supersedes UNIQUE (supersedes_scope_decision_id);

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT uq_dependency_event_scopes_decision_dependency UNIQUE (scope_decision_id, dependency_id);

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT uq_dependency_event_timing_kind UNIQUE (event_id, kind);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT uq_dependency_events_supersedes_event UNIQUE (supersedes_event_id);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT uq_dependency_evidence_sufficiency_scope_evidence UNIQUE (scope_link_id, evidence_link_id);

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT uq_discovered_reference_identity UNIQUE (project_id, reference_key);

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT uq_dispute_history_resolutions_coverage UNIQUE (dependency_id, field_name, covers_assertion_id);

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT uq_document_attempt_number UNIQUE (dispatch_id, attempt_number);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_project_id_id UNIQUE (project_id, id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_project_registry_id UNIQUE (project_id, registry_id);

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT uq_due_action_attempt_number UNIQUE (dispatch_id, attempt_number);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT uq_due_work_receipt_attempt UNIQUE (occurrence_id, attempt_number);

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT uq_due_work_schedule_identity UNIQUE (project_id, handler_key, configuration_version, input_identity_sha256);

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT uq_event_cohort_receipts_one_per_rule UNIQUE (project_id, rule_version);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT uq_evidence_investigation_shadow_case_identity UNIQUE (candidate_id, read_fingerprint, model, prompt_version, prompt_sha256, adapter_contract_version, tool_contract_version);

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT uq_evidence_investigation_shadow_human_outcome UNIQUE (human_outcome_identity);

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT uq_evidence_links_dependency_id_id UNIQUE (dependency_id, id);

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT uq_external_report_releases_artifact_id UNIQUE (artifact_id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT uq_extraction_measurement_case_states_ruling UNIQUE (ruling_type, ruling_id);

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT uq_extraction_runs_document_id_id UNIQUE (document_id, id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT uq_failure_diagnosis_request_input UNIQUE (configuration_id, input_sha256);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT uq_inbound_message_message_id UNIQUE (message_id);

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT uq_intake_draft_request_source UNIQUE (configuration_id, source_sha256);

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT uq_investigation_step_ordinal UNIQUE (run_id, ordinal);

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT uq_key_date_draft_row_ordinal UNIQUE (receipt_id, ordinal);

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT uq_milestone_registrations_predecessor UNIQUE (predecessor_registration_id);

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT uq_organization_identity_receipt_candidate_method UNIQUE (candidate_id, method);

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT uq_person_identity_email UNIQUE (email_normalized);

ALTER TABLE ONLY public.person_identities
    ADD CONSTRAINT uq_person_identity_principal UNIQUE (principal_subject);

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_family_id UNIQUE (family, id);

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_project_family_id UNIQUE (project_id, family, id);

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT uq_policy_approvals_project_id UNIQUE (project_id, id);

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT uq_policy_runs_family_id UNIQUE (family, id);

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT uq_policy_runs_project_family_id UNIQUE (project_id, family, id);

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT uq_project_roster_principal UNIQUE (project_id, principal_subject);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT uq_rev_change_expl_req_comparison UNIQUE (configuration_id, comparison_sha256);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT uq_run_explanation_request_comparison UNIQUE (configuration_id, comparison_sha256);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT uq_schedule_link_receipts_audit UNIQUE (audit_log_id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT uq_scheduled_report_publication_occurrence UNIQUE (occurrence_id);

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT uq_shadow_review_boundary UNIQUE (shadow_case_id, boundary);

ALTER TABLE ONLY public.sign_in_tokens
    ADD CONSTRAINT uq_sign_in_token_hash UNIQUE (token_sha256);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT uq_statement_coordination_receipt_disposition UNIQUE (candidate_disposition_id);

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT uq_statement_coordination_reversal_effect UNIQUE (reversal_id, effect_kind, target_id);

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT uq_statement_suggestion_eligibility_candidate UNIQUE (candidate_id);

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT uq_statement_suggestion_protection_end UNIQUE (protection_id);

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT uq_statement_suggestion_protection_window UNIQUE (candidate_id, kind, observation_contract);

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT uq_summary_request_reading UNIQUE (configuration_id, reading_sha256);

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT uq_unreadable_cell_step_ordinal UNIQUE (run_id, ordinal);

ALTER TABLE ONLY public.web_sessions
    ADD CONSTRAINT uq_web_session_hash UNIQUE (session_sha256);

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT uq_work_decision_milestone_impact UNIQUE (work_decision_id, milestone_id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_commitment_lineage_id_id UNIQUE (commitment_lineage_id, id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_dependency_id_id UNIQUE (dependency_id, id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT uq_work_decisions_predecessor UNIQUE (predecessor_decision_id);

ALTER TABLE ONLY public.web_sessions
    ADD CONSTRAINT web_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT work_decisions_pkey PRIMARY KEY (id);

CREATE INDEX ix_assignment_attempts_dispatch ON public.assignment_notification_attempts USING btree (dispatch_id);

CREATE INDEX ix_assignment_attempts_project ON public.assignment_notification_attempts USING btree (project_id);

CREATE INDEX ix_assignment_dispatches_project ON public.assignment_notification_dispatches USING btree (project_id);

CREATE INDEX ix_assignment_feedback_notification ON public.assignment_notification_feedback USING btree (notification_id);

CREATE INDEX ix_assignment_feedback_project ON public.assignment_notification_feedback USING btree (project_id);

CREATE INDEX ix_assignment_notifications_decision ON public.assignment_notifications USING btree (assignment_decision_id);

CREATE INDEX ix_assignment_notifications_project ON public.assignment_notifications USING btree (project_id);

CREATE INDEX ix_assignment_notifications_recipient ON public.assignment_notifications USING btree (project_id, recipient_principal_subject);

CREATE INDEX ix_automatic_carry_forward_outcomes_project_id ON public.automatic_carry_forward_outcomes USING btree (project_id);

CREATE INDEX ix_automatic_carry_forward_receipts_dependency_id ON public.automatic_carry_forward_receipts USING btree (dependency_id);

CREATE INDEX ix_capture_contracts_project_id ON public.evidence_investigation_capture_contracts USING btree (project_id);

CREATE INDEX ix_capture_results_contract_id ON public.evidence_investigation_capture_results USING btree (capture_contract_id);

CREATE INDEX ix_capture_results_project_id ON public.evidence_investigation_capture_results USING btree (project_id);

CREATE INDEX ix_condition_resolutions_dependency ON public.condition_resolutions USING btree (dependency_id, evidence_link_id);

CREATE INDEX ix_coordination_summary_configurations_project_id ON public.coordination_summary_configurations USING btree (project_id);

CREATE INDEX ix_coordination_summary_requests_project_id ON public.coordination_summary_requests USING btree (project_id);

CREATE INDEX ix_dependency_admission_outcomes_dependency_admission_run_id ON public.dependency_admission_outcomes USING btree (policy_run_id);

CREATE INDEX ix_dependency_dismissals_dependency ON public.dependency_dismissals USING btree (dependency_id);

CREATE INDEX ix_dependency_events_closes_commitment_lineage ON public.dependency_events USING btree (closes_commitment_lineage_id);

CREATE INDEX ix_discovered_references_project_id ON public.discovered_references USING btree (project_id);

CREATE INDEX ix_discovered_references_project_state ON public.discovered_references USING btree (project_id, state);

CREATE INDEX ix_dispute_history_resolutions_dependency_field ON public.dispute_history_resolutions USING btree (dependency_id, field_name);

CREATE INDEX ix_dispute_settlements_dependency_field ON public.dispute_settlements USING btree (dependency_id, field_name);

CREATE INDEX ix_document_attempts_dispatch ON public.document_notification_attempts USING btree (dispatch_id);

CREATE INDEX ix_document_attempts_project ON public.document_notification_attempts USING btree (project_id);

CREATE INDEX ix_document_dispatches_project ON public.document_notification_dispatches USING btree (project_id);

CREATE INDEX ix_document_notifications_project ON public.document_notifications USING btree (project_id);

CREATE INDEX ix_document_notifications_recipient ON public.document_notifications USING btree (project_id, recipient_principal_subject);

CREATE INDEX ix_document_rendition_derivations_project_id ON public.document_rendition_derivations USING btree (project_id);

CREATE INDEX ix_documentation_confirmation_dependency_field ON public.documentation_field_confirmations USING btree (dependency_id, field_name, id);

CREATE INDEX ix_due_action_attempts_dispatch ON public.due_action_notification_attempts USING btree (dispatch_id);

CREATE INDEX ix_due_action_attempts_project ON public.due_action_notification_attempts USING btree (project_id);

CREATE INDEX ix_due_action_dispatches_project ON public.due_action_notification_dispatches USING btree (project_id);

CREATE INDEX ix_due_action_notifications_project ON public.due_action_notifications USING btree (project_id);

CREATE INDEX ix_due_action_notifications_recipient ON public.due_action_notifications USING btree (project_id, recipient_principal_subject);

CREATE INDEX ix_due_work_occurrences_due ON public.due_work_occurrences USING btree (due_at);

CREATE INDEX ix_due_work_occurrences_job ON public.due_work_occurrences USING btree (scheduled_job_id);

CREATE INDEX ix_due_work_receipts_occurrence ON public.due_work_receipts USING btree (occurrence_id);

CREATE INDEX ix_due_work_receipts_project ON public.due_work_receipts USING btree (project_id);

CREATE INDEX ix_due_work_schedules_project_id ON public.due_work_schedules USING btree (project_id);

CREATE INDEX ix_event_admission_acceptance_project ON public.event_admission_acceptance_receipts USING btree (project_id, id);

CREATE INDEX ix_event_admission_activation_project ON public.event_admission_activations USING btree (project_id, id);

CREATE INDEX ix_event_admission_outcomes_event_admission_run_id ON public.event_admission_outcomes USING btree (policy_run_id);

CREATE UNIQUE INDEX ix_evidence_investigation_candidate_review_starts_candidate_id ON public.evidence_investigation_candidate_review_starts USING btree (candidate_id);

CREATE INDEX ix_evidence_investigation_candidate_review_starts_project_id ON public.evidence_investigation_candidate_review_starts USING btree (project_id);

CREATE INDEX ix_evidence_investigation_review_observations_shadow_case_id ON public.evidence_investigation_review_observations USING btree (shadow_case_id);

CREATE INDEX ix_evidence_investigation_runs_candidate_id ON public.evidence_investigation_runs USING btree (candidate_id);

CREATE INDEX ix_evidence_investigation_runs_project_id ON public.evidence_investigation_runs USING btree (project_id);

CREATE INDEX ix_evidence_investigation_shadow_cases_candidate_id ON public.evidence_investigation_shadow_cases USING btree (candidate_id);

CREATE INDEX ix_evidence_investigation_shadow_cases_project_id ON public.evidence_investigation_shadow_cases USING btree (project_id);

CREATE INDEX ix_evidence_investigation_step_receipts_run_id ON public.evidence_investigation_step_receipts USING btree (run_id);

CREATE INDEX ix_external_report_artifacts_project_id ON public.external_report_artifacts USING btree (project_id);

CREATE INDEX ix_external_report_releases_project_id ON public.external_report_releases USING btree (project_id);

CREATE INDEX ix_extraction_measurement_case_states_case_key ON public.extraction_measurement_case_states USING btree (case_key);

CREATE INDEX ix_extraction_measurement_case_states_project_id ON public.extraction_measurement_case_states USING btree (project_id);

CREATE INDEX ix_extraction_runs_completed_prompt_document ON public.extraction_runs USING btree (prompt_version, document_id) WHERE (((outcome)::text = 'completed'::text) AND (page_errors = 0));

CREATE INDEX ix_failure_diagnosis_configurations_project_id ON public.extraction_failure_diagnosis_configurations USING btree (project_id);

CREATE INDEX ix_failure_diagnosis_requests_document_id ON public.extraction_failure_diagnosis_requests USING btree (document_id);

CREATE INDEX ix_failure_diagnosis_requests_project_id ON public.extraction_failure_diagnosis_requests USING btree (project_id);

CREATE INDEX ix_follow_up_plan_receipts_dependency_id ON public.follow_up_plan_receipts USING btree (dependency_id);

CREATE INDEX ix_inbound_messages_project_id ON public.inbound_messages USING btree (project_id);

CREATE INDEX ix_inbound_messages_thread_id ON public.inbound_messages USING btree (thread_id);

CREATE INDEX ix_inbound_thread_readings_thread_id ON public.inbound_thread_readings USING btree (thread_id);

CREATE INDEX ix_intake_draft_configurations_project_id ON public.source_intake_draft_configurations USING btree (project_id);

CREATE INDEX ix_intake_draft_requests_project_id ON public.source_intake_draft_requests USING btree (project_id);

CREATE INDEX ix_intake_draft_requests_staged_sha256 ON public.source_intake_draft_requests USING btree (staged_sha256);

CREATE INDEX ix_intake_project_identifiers_project_id ON public.intake_project_identifiers USING btree (project_id);

CREATE INDEX ix_key_date_draft_receipts_project_id ON public.key_date_draft_receipts USING btree (project_id);

CREATE INDEX ix_key_date_draft_receipts_source_document_id ON public.key_date_draft_receipts USING btree (source_document_id);

CREATE INDEX ix_key_date_draft_row_receipts_receipt_id ON public.key_date_draft_row_receipts USING btree (receipt_id);

CREATE INDEX ix_milestone_registrations_milestone_id ON public.milestone_registrations USING btree (milestone_id);

CREATE INDEX ix_organization_identity_activations_project_id ON public.organization_identity_activations USING btree (project_id);

CREATE INDEX ix_organization_identity_receipts_candidate_id ON public.organization_identity_receipts USING btree (candidate_id);

CREATE INDEX ix_organization_identity_receipts_external_org_id ON public.organization_identity_receipts USING btree (external_org_id);

CREATE INDEX ix_organization_identity_receipts_project_id ON public.organization_identity_receipts USING btree (project_id);

CREATE INDEX ix_policy_runs_project_id ON public.policy_runs USING btree (project_id);

CREATE INDEX ix_project_check_configurations_project_id ON public.project_check_configurations USING btree (project_id);

CREATE INDEX ix_reconfirmation_receipts_dependency_id ON public.reconfirmation_receipts USING btree (dependency_id);

CREATE INDEX ix_rev_change_expl_cfg_project_id ON public.revision_change_explanation_configurations USING btree (project_id);

CREATE INDEX ix_rev_change_expl_req_dependency_id ON public.revision_change_explanation_requests USING btree (dependency_id);

CREATE INDEX ix_rev_change_expl_req_project_id ON public.revision_change_explanation_requests USING btree (project_id);

CREATE INDEX ix_run_explanation_configurations_project_id ON public.production_run_explanation_configurations USING btree (project_id);

CREATE INDEX ix_run_explanation_requests_document_id ON public.production_run_explanation_requests USING btree (document_id);

CREATE INDEX ix_run_explanation_requests_project_id ON public.production_run_explanation_requests USING btree (project_id);

CREATE INDEX ix_schedule_governing_derivations_project_id ON public.schedule_governing_derivations USING btree (project_id);

CREATE INDEX ix_schedule_link_activations_project_id ON public.schedule_link_activations USING btree (project_id);

CREATE INDEX ix_schedule_link_receipts_dependency_id ON public.schedule_link_receipts USING btree (dependency_id);

CREATE INDEX ix_schedule_link_receipts_project_id ON public.schedule_link_receipts USING btree (project_id);

CREATE INDEX ix_scheduled_report_publications_project_id ON public.scheduled_report_publications USING btree (project_id);

CREATE INDEX ix_sign_in_attempt_scope ON public.sign_in_attempts USING btree (scope_kind, scope_value, occurred_at);

CREATE INDEX ix_source_fetch_attempts_project_id ON public.source_fetch_attempts USING btree (project_id);

CREATE INDEX ix_source_fetch_attempts_project_outcome ON public.source_fetch_attempts USING btree (project_id, outcome);

CREATE INDEX ix_source_fetch_attempts_reference ON public.source_fetch_attempts USING btree (project_id, reference_key);

CREATE INDEX ix_statement_suggestion_eligibility_declarations_candidate_id ON public.statement_suggestion_eligibility_declarations USING btree (candidate_id);

CREATE INDEX ix_statement_suggestion_eligibility_declarations_project_id ON public.statement_suggestion_eligibility_declarations USING btree (project_id);

CREATE INDEX ix_statement_suggestion_protection_ends_protection_id ON public.statement_suggestion_protection_ends USING btree (protection_id);

CREATE INDEX ix_statement_suggestion_protections_candidate_id ON public.statement_suggestion_protections USING btree (candidate_id);

CREATE INDEX ix_statement_suggestion_protections_project_id ON public.statement_suggestion_protections USING btree (project_id);

CREATE INDEX ix_unreadable_cell_admission_activations_project_id ON public.unreadable_cell_admission_activations USING btree (project_id);

CREATE INDEX ix_unreadable_cell_reading_profiles_project_id ON public.unreadable_cell_reading_profiles USING btree (project_id);

CREATE INDEX ix_unreadable_cell_reading_runs_document_id ON public.unreadable_cell_reading_runs USING btree (document_id);

CREATE INDEX ix_unreadable_cell_reading_runs_project_id ON public.unreadable_cell_reading_runs USING btree (project_id);

CREATE INDEX ix_unreadable_cell_reading_steps_run_id ON public.unreadable_cell_reading_steps USING btree (run_id);

CREATE INDEX ix_unreadable_cell_resolutions_cell ON public.unreadable_cell_resolutions USING btree (project_id, document_id, page_no, cell_key);

CREATE INDEX ix_unreadable_cell_resolutions_document_id ON public.unreadable_cell_resolutions USING btree (document_id);

CREATE INDEX ix_unreadable_cell_resolutions_project_id ON public.unreadable_cell_resolutions USING btree (project_id);

CREATE UNIQUE INDEX uq_active_run_declarations_one_root ON public.active_run_declarations USING btree (document_id) WHERE (predecessor_declaration_id IS NULL);

CREATE UNIQUE INDEX uq_automatic_carry_forward_outcome_abstained_identity ON public.automatic_carry_forward_outcomes USING btree (project_id, COALESCE(policy_approval_id, (0)::bigint), dependency_id, COALESCE(comparison_id, ('-1'::integer)::bigint), COALESCE(finding_id, ('-1'::integer)::bigint), COALESCE(predecessor_candidate_id, ('-1'::integer)::bigint), COALESCE(successor_candidate_id, ('-1'::integer)::bigint), reason, reason_version) WHERE ((outcome)::text = 'abstained'::text);

CREATE UNIQUE INDEX uq_dependency_event_scope_decision_root ON public.dependency_event_scope_decisions USING btree (event_id) WHERE (supersedes_scope_decision_id IS NULL);

CREATE UNIQUE INDEX uq_dependency_evidence_sufficiency_direct_evidence ON public.dependency_evidence_sufficiencies USING btree (evidence_link_id) WHERE (scope_link_id IS NULL);

CREATE UNIQUE INDEX uq_extraction_measurement_case_states_root ON public.extraction_measurement_case_states USING btree (case_key) WHERE (predecessor_state_id IS NULL);

CREATE UNIQUE INDEX uq_milestone_registrations_one_root ON public.milestone_registrations USING btree (milestone_id) WHERE (predecessor_registration_id IS NULL);

CREATE UNIQUE INDEX uq_operative_support_field_role ON public.operative_support USING btree (dependency_id, role, field_name) WHERE (field_name IS NOT NULL);

CREATE UNIQUE INDEX uq_operative_support_record_role ON public.operative_support USING btree (dependency_id, role) WHERE (field_name IS NULL);

CREATE UNIQUE INDEX uq_work_decisions_commitment_lineage_one_root ON public.work_decisions USING btree (commitment_lineage_id, field) WHERE (predecessor_decision_id IS NULL);

CREATE UNIQUE INDEX uq_work_decisions_one_root ON public.work_decisions USING btree (dependency_id, field) WHERE (predecessor_decision_id IS NULL);

CREATE TRIGGER active_run_declarations_are_immutable BEFORE DELETE OR UPDATE ON public.active_run_declarations FOR EACH ROW EXECUTE FUNCTION public.enforce_active_run_declaration();

CREATE TRIGGER active_run_declarations_reject_truncate BEFORE TRUNCATE ON public.active_run_declarations FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_active_run_declaration();

CREATE TRIGGER assignment_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_assignment_attempt_mutation();

CREATE TRIGGER assignment_attempts_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();

CREATE TRIGGER assignment_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_assignment_dispatch_identity();

CREATE TRIGGER assignment_dispatches_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();

CREATE TRIGGER assignment_feedback_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notification_feedback FOR EACH ROW EXECUTE FUNCTION public.refuse_assignment_feedback_mutation();

CREATE TRIGGER assignment_feedback_reject_truncate BEFORE TRUNCATE ON public.assignment_notification_feedback FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();

CREATE TRIGGER assignment_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.assignment_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_assignment_notification_immutable();

CREATE TRIGGER assignment_notifications_reject_truncate BEFORE TRUNCATE ON public.assignment_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_assignment_notification_truncate();

CREATE TRIGGER automatic_carry_forward_outcomes_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.automatic_carry_forward_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_automatic_carry_forward_outcome();

CREATE CONSTRAINT TRIGGER automatic_carry_forward_outcomes_must_match_runs AFTER INSERT ON public.automatic_carry_forward_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_run_id();

CREATE TRIGGER automatic_carry_forward_outcomes_reject_truncate BEFORE TRUNCATE ON public.automatic_carry_forward_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_automatic_carry_forward_outcome();

CREATE TRIGGER automatic_carry_forward_receipts_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.automatic_carry_forward_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_automatic_carry_forward_receipt();

CREATE TRIGGER automatic_carry_forward_receipts_reject_truncate BEFORE TRUNCATE ON public.automatic_carry_forward_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_automatic_carry_forward_receipt();

CREATE TRIGGER candidate_dispositions_are_immutable BEFORE DELETE OR UPDATE ON public.candidate_dispositions FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER candidate_dispositions_reject_truncate BEFORE TRUNCATE ON public.candidate_dispositions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER candidate_run_lineage_is_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.candidates FOR EACH ROW EXECUTE FUNCTION public.enforce_candidate_run_lineage();

CREATE TRIGGER candidates_reject_truncate BEFORE TRUNCATE ON public.candidates FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_candidate_run_lineage();

CREATE CONSTRAINT TRIGGER ck_documents_registered_supersession_participants AFTER INSERT OR UPDATE OF registry_id, superseded_by, superseded_on, supersession_source_document_id, supersession_source_page ON public.documents DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION public.enforce_document_supersession_registry();

CREATE TRIGGER cohort_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.cohort_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_cohort_receipt();

CREATE TRIGGER cohort_receipts_reject_truncate BEFORE TRUNCATE ON public.cohort_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_cohort_receipt();

CREATE CONSTRAINT TRIGGER commitment_lineages_have_current_statement AFTER INSERT OR DELETE OR UPDATE ON public.commitment_lineages DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_lineage();

CREATE TRIGGER condition_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.condition_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_condition_resolutions_mutation();

CREATE TRIGGER condition_resolutions_reject_truncate BEFORE TRUNCATE ON public.condition_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_condition_resolutions_mutation();

CREATE TRIGGER coordination_summary_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.coordination_summary_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_coordination_summary_configurations_mutation();

CREATE TRIGGER coordination_summary_configurations_reject_truncate BEFORE TRUNCATE ON public.coordination_summary_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_coordination_summary_configurations_mutation();

CREATE TRIGGER coordination_summary_requests_are_immutable BEFORE DELETE OR UPDATE ON public.coordination_summary_requests FOR EACH ROW EXECUTE FUNCTION public.reject_coordination_summary_requests_mutation();

CREATE TRIGGER coordination_summary_requests_reject_truncate BEFORE TRUNCATE ON public.coordination_summary_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_coordination_summary_requests_mutation();

CREATE TRIGGER dependency_admission_abstentions_require_eligibility BEFORE INSERT ON public.dependency_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.require_dependency_admission_abstention_eligibility();

CREATE TRIGGER dependency_admission_outcomes_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_dependency_admission_outcomes();

CREATE CONSTRAINT TRIGGER dependency_admission_outcomes_must_match_runs AFTER INSERT ON public.dependency_admission_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_policy_run_id();

CREATE TRIGGER dependency_admission_outcomes_reject_truncate BEFORE TRUNCATE ON public.dependency_admission_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_dependency_admission_outcomes();

CREATE TRIGGER dependency_dismissals_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_dismissals FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_dismissal_mutation();

CREATE TRIGGER dependency_dismissals_reject_truncate BEFORE TRUNCATE ON public.dependency_dismissals FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dependency_dismissal_mutation();

CREATE TRIGGER dependency_event_closure_link_is_valid BEFORE INSERT OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_closure_link();

CREATE CONSTRAINT TRIGGER dependency_event_evidence_has_one_owner AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_evidence_link_ownership();

CREATE TRIGGER dependency_event_evidence_is_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_evidence FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_evidence_mutation();

CREATE TRIGGER dependency_event_evidence_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_evidence FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_evidence();

CREATE TRIGGER dependency_event_evidence_reject_truncate BEFORE TRUNCATE ON public.dependency_event_evidence FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER dependency_event_migration_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_migration_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_migration_receipt_mutation();

CREATE TRIGGER dependency_event_migration_receipts_reject_truncate BEFORE TRUNCATE ON public.dependency_event_migration_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER dependency_event_scope_decision_actor_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_scope_decisions FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision_actor();

CREATE TRIGGER dependency_event_scope_decision_link_is_valid BEFORE INSERT OR UPDATE ON public.dependency_event_scopes FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision_link();

CREATE CONSTRAINT TRIGGER dependency_event_scope_decision_links_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_scopes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision();

CREATE CONSTRAINT TRIGGER dependency_event_scope_decision_shape_is_valid AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_scope_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_event_scope_decision();

CREATE TRIGGER dependency_event_scope_decisions_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_scope_decisions FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_scope_decision_mutation();

CREATE TRIGGER dependency_event_scope_decisions_reject_truncate BEFORE TRUNCATE ON public.dependency_event_scope_decisions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER dependency_event_scope_links_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_scopes FOR EACH ROW EXECUTE FUNCTION public.reject_dependency_event_scope_link_mutation();

CREATE TRIGGER dependency_event_scope_links_reject_truncate BEFORE TRUNCATE ON public.dependency_event_scopes FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE CONSTRAINT TRIGGER dependency_event_timing_cardinality_is_valid AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.verify_dependency_event_timing_cardinality();

CREATE CONSTRAINT TRIGGER dependency_event_timing_rows_match_event AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_timings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.verify_dependency_event_timing_cardinality();

CREATE CONSTRAINT TRIGGER dependency_events_have_valid_commitment_lineage AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_commitment_lineage_event();

CREATE TRIGGER dependency_events_receive_initial_scope_decision AFTER INSERT ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.create_initial_dependency_event_scope_decision();

CREATE TRIGGER dependency_evidence_sufficiency_scope_is_valid BEFORE INSERT OR UPDATE ON public.dependency_evidence_sufficiencies FOR EACH ROW EXECUTE FUNCTION public.validate_dependency_evidence_sufficiency_scope_role();

CREATE TRIGGER dispute_history_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.dispute_history_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_dispute_history_resolution_mutation();

CREATE TRIGGER dispute_history_resolutions_reject_truncate BEFORE TRUNCATE ON public.dispute_history_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dispute_history_resolution_mutation();

CREATE TRIGGER dispute_settlements_are_immutable BEFORE DELETE OR UPDATE ON public.dispute_settlements FOR EACH ROW EXECUTE FUNCTION public.reject_dispute_settlement_mutation();

CREATE TRIGGER dispute_settlements_reject_truncate BEFORE TRUNCATE ON public.dispute_settlements FOR EACH STATEMENT EXECUTE FUNCTION public.reject_dispute_settlement_mutation();

CREATE TRIGGER document_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.document_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_document_attempt_mutation();

CREATE TRIGGER document_attempts_reject_truncate BEFORE TRUNCATE ON public.document_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();

CREATE TRIGGER document_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.document_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_document_dispatch_identity();

CREATE TRIGGER document_dispatches_reject_truncate BEFORE TRUNCATE ON public.document_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();

CREATE TRIGGER document_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.document_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_document_notification_immutable();

CREATE TRIGGER document_notifications_reject_truncate BEFORE TRUNCATE ON public.document_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_notification_truncate();

CREATE TRIGGER document_rendition_derivations_are_immutable BEFORE DELETE OR UPDATE ON public.document_rendition_derivations FOR EACH ROW EXECUTE FUNCTION public.reject_document_rendition_derivation_mutation();

CREATE TRIGGER document_rendition_derivations_reject_truncate BEFORE TRUNCATE ON public.document_rendition_derivations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_document_rendition_derivation_mutation();

CREATE TRIGGER documentation_field_confirmations_are_immutable BEFORE DELETE OR UPDATE ON public.documentation_field_confirmations FOR EACH ROW EXECUTE FUNCTION public.reject_documentation_confirmation_mutation();

CREATE TRIGGER documentation_field_confirmations_reject_truncate BEFORE TRUNCATE ON public.documentation_field_confirmations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_documentation_confirmation_mutation();

CREATE TRIGGER due_action_attempts_are_immutable BEFORE DELETE OR UPDATE ON public.due_action_notification_attempts FOR EACH ROW EXECUTE FUNCTION public.refuse_due_action_attempt_mutation();

CREATE TRIGGER due_action_attempts_reject_truncate BEFORE TRUNCATE ON public.due_action_notification_attempts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();

CREATE TRIGGER due_action_dispatch_identity_is_immutable BEFORE DELETE OR UPDATE ON public.due_action_notification_dispatches FOR EACH ROW EXECUTE FUNCTION public.enforce_due_action_dispatch_identity();

CREATE TRIGGER due_action_dispatches_reject_truncate BEFORE TRUNCATE ON public.due_action_notification_dispatches FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();

CREATE TRIGGER due_action_notifications_are_immutable BEFORE DELETE OR UPDATE ON public.due_action_notifications FOR EACH ROW EXECUTE FUNCTION public.enforce_due_action_notification_immutable();

CREATE TRIGGER due_action_notifications_reject_truncate BEFORE TRUNCATE ON public.due_action_notifications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_action_notification_truncate();

CREATE TRIGGER due_work_occurrence_identity_is_immutable BEFORE DELETE OR UPDATE ON public.due_work_occurrences FOR EACH ROW EXECUTE FUNCTION public.enforce_due_work_occurrence_identity();

CREATE TRIGGER due_work_occurrences_reject_truncate BEFORE TRUNCATE ON public.due_work_occurrences FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();

CREATE TRIGGER due_work_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.due_work_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_due_work_receipt_mutation();

CREATE TRIGGER due_work_receipts_reject_truncate BEFORE TRUNCATE ON public.due_work_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();

CREATE TRIGGER due_work_schedules_are_immutable BEFORE DELETE OR UPDATE ON public.due_work_schedules FOR EACH ROW EXECUTE FUNCTION public.enforce_due_work_schedule_mutation();

CREATE TRIGGER due_work_schedules_reject_truncate BEFORE TRUNCATE ON public.due_work_schedules FOR EACH STATEMENT EXECUTE FUNCTION public.reject_due_work_truncate();

CREATE TRIGGER event_admission_acceptance_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_acceptance_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_event_admission_acceptance_mutation();

CREATE TRIGGER event_admission_activations_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_activations FOR EACH ROW EXECUTE FUNCTION public.refuse_event_admission_acceptance_mutation();

CREATE TRIGGER event_admission_activations_require_passing_receipt BEFORE INSERT ON public.event_admission_activations FOR EACH ROW EXECUTE FUNCTION public.require_passing_event_admission_activation();

CREATE TRIGGER event_admission_outcomes_are_immutable BEFORE DELETE OR UPDATE ON public.event_admission_outcomes FOR EACH ROW EXECUTE FUNCTION public.enforce_event_admission_outcomes();

CREATE CONSTRAINT TRIGGER event_admission_outcomes_must_match_runs AFTER INSERT ON public.event_admission_outcomes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_outcome_counts_by_policy_run_id();

CREATE TRIGGER event_admission_outcomes_reject_truncate BEFORE TRUNCATE ON public.event_admission_outcomes FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_event_admission_outcomes();

CREATE TRIGGER event_cohort_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.event_cohort_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_event_cohort_receipt();

CREATE TRIGGER event_cohort_receipts_reject_truncate BEFORE TRUNCATE ON public.event_cohort_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_event_cohort_receipt();

CREATE TRIGGER evidence_investigation_candidate_review_starts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_candidate_review_starts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_capture_contracts_are_immutable BEFORE DELETE OR UPDATE ON public.evidence_investigation_capture_contracts FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation();

CREATE TRIGGER evidence_investigation_capture_contracts_reject_truncate BEFORE TRUNCATE ON public.evidence_investigation_capture_contracts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_evidence_investigation_capture_contracts_mutation();

CREATE TRIGGER evidence_investigation_capture_results_are_immutable BEFORE DELETE OR UPDATE ON public.evidence_investigation_capture_results FOR EACH ROW EXECUTE FUNCTION public.reject_evidence_investigation_capture_results_mutation();

CREATE TRIGGER evidence_investigation_capture_results_reject_truncate BEFORE TRUNCATE ON public.evidence_investigation_capture_results FOR EACH STATEMENT EXECUTE FUNCTION public.reject_evidence_investigation_capture_results_mutation();

CREATE TRIGGER evidence_investigation_evaluation_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_evaluation_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_packet_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_packet_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_review_observations_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_review_observations FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_runs_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_runs FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_shadow_cases_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_cases FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_shadow_executions_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_executions FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_shadow_outcomes_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_shadow_outcomes FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE TRIGGER evidence_investigation_step_receipts_append_only BEFORE DELETE OR UPDATE ON public.evidence_investigation_step_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_evidence_investigation_receipt_mutation();

CREATE CONSTRAINT TRIGGER evidence_links_have_one_owner AFTER INSERT OR DELETE OR UPDATE ON public.evidence_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_evidence_link_ownership();

CREATE TRIGGER external_party_statement_events_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_mutation();

CREATE TRIGGER external_party_statement_events_reject_truncate BEFORE TRUNCATE ON public.dependency_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER external_party_statement_evidence_is_immutable BEFORE DELETE OR UPDATE ON public.evidence_links FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_evidence_mutation();

CREATE TRIGGER external_party_statement_evidence_reject_truncate BEFORE TRUNCATE ON public.evidence_links FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER external_party_statement_timings_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_event_timings FOR EACH ROW EXECUTE FUNCTION public.reject_external_party_statement_child_mutation();

CREATE TRIGGER external_party_statement_timings_reject_truncate BEFORE TRUNCATE ON public.dependency_event_timings FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_party_statement_truncate();

CREATE TRIGGER external_report_artifacts_are_immutable BEFORE DELETE OR UPDATE ON public.external_report_artifacts FOR EACH ROW EXECUTE FUNCTION public.prevent_external_report_artifact_mutation();

CREATE TRIGGER external_report_artifacts_reject_truncate BEFORE TRUNCATE ON public.external_report_artifacts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_report_artifact_truncate();

CREATE TRIGGER external_report_releases_reject_truncate BEFORE TRUNCATE ON public.external_report_releases FOR EACH STATEMENT EXECUTE FUNCTION public.reject_external_report_release_truncate();

CREATE TRIGGER extraction_failure_diagnosis_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_failure_diagnosis_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation();

CREATE TRIGGER extraction_failure_diagnosis_configurations_reject_truncate BEFORE TRUNCATE ON public.extraction_failure_diagnosis_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_configurations_mutation();

CREATE TRIGGER extraction_failure_diagnosis_requests_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_failure_diagnosis_requests FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation();

CREATE TRIGGER extraction_failure_diagnosis_requests_reject_truncate BEFORE TRUNCATE ON public.extraction_failure_diagnosis_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_failure_diagnosis_requests_mutation();

CREATE TRIGGER extraction_measurement_case_state_lineage BEFORE INSERT ON public.extraction_measurement_case_states FOR EACH ROW EXECUTE FUNCTION public.validate_extraction_measurement_case_state();

CREATE TRIGGER extraction_measurement_case_states_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_measurement_case_states FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_measurement_case_mutation();

CREATE TRIGGER extraction_measurement_case_states_reject_truncate BEFORE TRUNCATE ON public.extraction_measurement_case_states FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_measurement_case_mutation();

CREATE TRIGGER extraction_run_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.extraction_runs FOR EACH ROW EXECUTE FUNCTION public.reject_extraction_run_receipt_mutation();

CREATE TRIGGER extraction_run_receipts_reject_truncate BEFORE TRUNCATE ON public.extraction_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_extraction_run_receipt_mutation();

CREATE TRIGGER key_date_draft_receipts_append_only BEFORE DELETE OR UPDATE ON public.key_date_draft_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_key_date_draft_receipt_mutation();

CREATE TRIGGER key_date_draft_row_receipts_append_only BEFORE DELETE OR UPDATE ON public.key_date_draft_row_receipts FOR EACH ROW EXECUTE FUNCTION public.refuse_key_date_draft_receipt_mutation();

CREATE TRIGGER legacy_ledger_archives_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.legacy_ledger_archives FOR EACH ROW EXECUTE FUNCTION public.reject_legacy_ledger_archive_mutation();

CREATE TRIGGER legacy_ledger_archives_reject_truncate BEFORE TRUNCATE ON public.legacy_ledger_archives FOR EACH STATEMENT EXECUTE FUNCTION public.reject_legacy_ledger_archive_mutation();

CREATE TRIGGER milestone_registrations_are_immutable BEFORE DELETE OR UPDATE ON public.milestone_registrations FOR EACH ROW EXECUTE FUNCTION public.enforce_milestone_registration();

CREATE TRIGGER milestone_registrations_reject_truncate BEFORE TRUNCATE ON public.milestone_registrations FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_milestone_registration();

CREATE TRIGGER operative_event_evidence_scope_is_valid BEFORE INSERT OR UPDATE ON public.operative_support FOR EACH ROW EXECUTE FUNCTION public.validate_operative_event_evidence_scope_role();

CREATE TRIGGER organization_identity_activations_are_immutable BEFORE DELETE OR UPDATE ON public.organization_identity_activations FOR EACH ROW EXECUTE FUNCTION public.reject_organization_identity_mutation();

CREATE TRIGGER organization_identity_activations_reject_truncate BEFORE TRUNCATE ON public.organization_identity_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_organization_identity_mutation();

CREATE TRIGGER organization_identity_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.organization_identity_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_organization_identity_mutation();

CREATE TRIGGER organization_identity_receipts_reject_truncate BEFORE TRUNCATE ON public.organization_identity_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_organization_identity_mutation();

CREATE TRIGGER policy_approvals_are_immutable BEFORE DELETE OR UPDATE ON public.policy_approvals FOR EACH ROW EXECUTE FUNCTION public.enforce_policy_approvals();

CREATE TRIGGER policy_approvals_reject_truncate BEFORE TRUNCATE ON public.policy_approvals FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_policy_approvals();

CREATE TRIGGER policy_runs_are_immutable BEFORE DELETE OR UPDATE ON public.policy_runs FOR EACH ROW EXECUTE FUNCTION public.enforce_policy_runs();

CREATE CONSTRAINT TRIGGER policy_runs_must_match_outcomes AFTER INSERT ON public.policy_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_policy_run_counts();

CREATE TRIGGER policy_runs_reject_truncate BEFORE TRUNCATE ON public.policy_runs FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_policy_runs();

CREATE TRIGGER prevent_external_report_release_mutation BEFORE DELETE OR UPDATE ON public.external_report_releases FOR EACH ROW EXECUTE FUNCTION public.prevent_external_report_release_mutation();

CREATE TRIGGER production_run_explanation_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.production_run_explanation_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_production_run_explanation_configurations_mutation();

CREATE TRIGGER production_run_explanation_configurations_reject_truncate BEFORE TRUNCATE ON public.production_run_explanation_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_production_run_explanation_configurations_mutation();

CREATE TRIGGER production_run_explanation_requests_are_immutable BEFORE DELETE OR UPDATE ON public.production_run_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.reject_production_run_explanation_requests_mutation();

CREATE TRIGGER production_run_explanation_requests_reject_truncate BEFORE TRUNCATE ON public.production_run_explanation_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_production_run_explanation_requests_mutation();

CREATE TRIGGER project_check_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.project_check_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_project_check_configuration_mutation();

CREATE TRIGGER project_check_configurations_reject_truncate BEFORE TRUNCATE ON public.project_check_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_project_check_configuration_mutation();

CREATE TRIGGER reconfirmation_receipts_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.reconfirmation_receipts FOR EACH ROW EXECUTE FUNCTION public.enforce_reconfirmation_receipt_mutation();

CREATE TRIGGER reconfirmation_receipts_reject_truncate BEFORE TRUNCATE ON public.reconfirmation_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_reconfirmation_receipt_mutation();

CREATE TRIGGER revision_change_explanation_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.revision_change_explanation_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_revision_change_explanation_configurations_mutation();

CREATE TRIGGER revision_change_explanation_configurations_reject_truncate BEFORE TRUNCATE ON public.revision_change_explanation_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_change_explanation_configurations_mutation();

CREATE TRIGGER revision_change_explanation_requests_are_immutable BEFORE DELETE OR UPDATE ON public.revision_change_explanation_requests FOR EACH ROW EXECUTE FUNCTION public.reject_revision_change_explanation_requests_mutation();

CREATE TRIGGER revision_change_explanation_requests_reject_truncate BEFORE TRUNCATE ON public.revision_change_explanation_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_change_explanation_requests_mutation();

CREATE TRIGGER revision_comparison_findings_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.revision_comparison_findings FOR EACH ROW EXECUTE FUNCTION public.enforce_revision_comparison_finding_mutation();

CREATE TRIGGER revision_comparison_findings_reject_truncate BEFORE TRUNCATE ON public.revision_comparison_findings FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_comparison_truncate();

CREATE TRIGGER revision_comparison_runs_are_immutable BEFORE INSERT OR DELETE OR UPDATE ON public.revision_comparison_runs FOR EACH ROW EXECUTE FUNCTION public.enforce_revision_comparison_run_mutation();

CREATE CONSTRAINT TRIGGER revision_comparison_runs_must_commit_sealed AFTER INSERT ON public.revision_comparison_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.require_sealed_revision_comparison();

CREATE TRIGGER revision_comparison_runs_reject_truncate BEFORE TRUNCATE ON public.revision_comparison_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_revision_comparison_truncate();

CREATE TRIGGER schedule_governing_derivations_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_governing_derivations FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER schedule_governing_derivations_reject_truncate BEFORE TRUNCATE ON public.schedule_governing_derivations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER schedule_link_activations_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_link_activations FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER schedule_link_activations_reject_truncate BEFORE TRUNCATE ON public.schedule_link_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER schedule_link_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.schedule_link_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER schedule_link_receipts_reject_truncate BEFORE TRUNCATE ON public.schedule_link_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_schedule_linking_mutation();

CREATE TRIGGER scheduled_report_publications_are_immutable BEFORE DELETE OR UPDATE ON public.scheduled_report_publications FOR EACH ROW EXECUTE FUNCTION public.reject_scheduled_report_publications_mutation();

CREATE TRIGGER scheduled_report_publications_reject_truncate BEFORE TRUNCATE ON public.scheduled_report_publications FOR EACH STATEMENT EXECUTE FUNCTION public.reject_scheduled_report_publications_mutation();

CREATE TRIGGER source_intake_draft_configurations_are_immutable BEFORE DELETE OR UPDATE ON public.source_intake_draft_configurations FOR EACH ROW EXECUTE FUNCTION public.reject_source_intake_draft_configurations_mutation();

CREATE TRIGGER source_intake_draft_configurations_reject_truncate BEFORE TRUNCATE ON public.source_intake_draft_configurations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_source_intake_draft_configurations_mutation();

CREATE TRIGGER source_intake_draft_requests_are_immutable BEFORE DELETE OR UPDATE ON public.source_intake_draft_requests FOR EACH ROW EXECUTE FUNCTION public.reject_source_intake_draft_requests_mutation();

CREATE TRIGGER source_intake_draft_requests_reject_truncate BEFORE TRUNCATE ON public.source_intake_draft_requests FOR EACH STATEMENT EXECUTE FUNCTION public.reject_source_intake_draft_requests_mutation();

CREATE TRIGGER statement_coordination_receipts_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_receipts FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_coordination_receipts_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_coordination_reversal_effects_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_reversal_effects FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_coordination_reversal_effects_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_reversal_effects FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_coordination_reversals_are_immutable BEFORE DELETE OR UPDATE ON public.statement_coordination_reversals FOR EACH ROW EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_coordination_reversals_reject_truncate BEFORE TRUNCATE ON public.statement_coordination_reversals FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_lifecycle_mutation();

CREATE TRIGGER statement_suggestion_eligibility_declarations_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_eligibility_declarations FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER statement_suggestion_eligibility_declarations_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_eligibility_declarations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER statement_suggestion_protection_ends_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_protection_ends FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER statement_suggestion_protection_ends_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_protection_ends FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER statement_suggestion_protections_immutable BEFORE DELETE OR UPDATE ON public.statement_suggestion_protections FOR EACH ROW EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER statement_suggestion_protections_reject_truncate BEFORE TRUNCATE ON public.statement_suggestion_protections FOR EACH STATEMENT EXECUTE FUNCTION public.reject_statement_suggestion_protection_mutation();

CREATE TRIGGER unreadable_cell_admission_activations_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_admission_activations FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_admission_activations_mutation();

CREATE TRIGGER unreadable_cell_admission_activations_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_admission_activations FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_admission_activations_mutation();

CREATE TRIGGER unreadable_cell_reading_profiles_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_profiles FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation();

CREATE TRIGGER unreadable_cell_reading_profiles_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_profiles FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_profiles_mutation();

CREATE TRIGGER unreadable_cell_reading_runs_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_runs FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_runs_mutation();

CREATE TRIGGER unreadable_cell_reading_runs_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_runs FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_runs_mutation();

CREATE TRIGGER unreadable_cell_reading_steps_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_reading_steps FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_reading_steps_mutation();

CREATE TRIGGER unreadable_cell_reading_steps_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_reading_steps FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_reading_steps_mutation();

CREATE TRIGGER unreadable_cell_resolutions_are_immutable BEFORE DELETE OR UPDATE ON public.unreadable_cell_resolutions FOR EACH ROW EXECUTE FUNCTION public.reject_unreadable_cell_resolutions_mutation();

CREATE TRIGGER unreadable_cell_resolutions_reject_truncate BEFORE TRUNCATE ON public.unreadable_cell_resolutions FOR EACH STATEMENT EXECUTE FUNCTION public.reject_unreadable_cell_resolutions_mutation();

CREATE TRIGGER verbal_dependency_events_are_immutable BEFORE DELETE OR UPDATE ON public.dependency_events FOR EACH ROW EXECUTE FUNCTION public.reject_verbal_dependency_event_mutation();

CREATE TRIGGER verbal_dependency_events_reject_truncate BEFORE TRUNCATE ON public.dependency_events FOR EACH STATEMENT EXECUTE FUNCTION public.reject_verbal_dependency_event_mutation();

CREATE CONSTRAINT TRIGGER verbal_statement_timings_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_event_timings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_verbal_statement_shape();

CREATE CONSTRAINT TRIGGER verbal_statements_match_shape AFTER INSERT OR DELETE OR UPDATE ON public.dependency_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_verbal_statement_shape();

CREATE CONSTRAINT TRIGGER work_decision_milestone_impacts_are_valid AFTER INSERT OR DELETE OR UPDATE ON public.work_decision_milestone_impacts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_milestone_impact();

CREATE TRIGGER work_decisions_are_immutable BEFORE DELETE OR UPDATE ON public.work_decisions FOR EACH ROW EXECUTE FUNCTION public.enforce_work_decision();

CREATE CONSTRAINT TRIGGER work_decisions_have_valid_milestone_impact AFTER INSERT OR DELETE OR UPDATE ON public.work_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_milestone_impact();

CREATE TRIGGER work_decisions_have_valid_subject BEFORE INSERT OR UPDATE ON public.work_decisions FOR EACH ROW EXECUTE FUNCTION public.validate_work_decision_subject();

CREATE TRIGGER work_decisions_reject_truncate BEFORE TRUNCATE ON public.work_decisions FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_work_decision();

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT active_automatic_carry_forward_policies_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT active_extraction_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_document_id_extraction_run_id_fkey FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT active_run_declarations_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.assertions
    ADD CONSTRAINT assertions_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.assignment_notification_dispatches(id);

ALTER TABLE ONLY public.assignment_notification_attempts
    ADD CONSTRAINT assignment_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.assignment_notifications(id);

ALTER TABLE ONLY public.assignment_notification_dispatches
    ADD CONSTRAINT assignment_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.assignment_notifications(id);

ALTER TABLE ONLY public.assignment_notification_feedback
    ADD CONSTRAINT assignment_notification_feedback_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_assignment_decision_id_fkey FOREIGN KEY (assignment_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.assignment_notifications
    ADD CONSTRAINT assignment_notifications_recipient_roster_entry_id_fkey FOREIGN KEY (recipient_roster_entry_id) REFERENCES public.project_roster_entries(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_predecessor_candidate_id_fkey FOREIGN KEY (predecessor_candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_receipt_audit_log_id_fkey FOREIGN KEY (receipt_audit_log_id) REFERENCES public.automatic_carry_forward_receipts(audit_log_id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT automatic_carry_forward_outcomes_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_recei_predecessor_support_transfer_fkey FOREIGN KEY (predecessor_support_transfer_audit_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_origin_admission_audit_id_fkey FOREIGN KEY (origin_admission_audit_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_predecessor_candidate_id_fkey FOREIGN KEY (predecessor_candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT automatic_carry_forward_receipts_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.candidate_dispositions
    ADD CONSTRAINT candidate_dispositions_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_merged_into_fkey FOREIGN KEY (merged_into) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT candidates_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.cohort_receipts
    ADD CONSTRAINT cohort_receipts_revision_comparison_run_id_fkey FOREIGN KEY (revision_comparison_run_id) REFERENCES public.revision_comparison_runs(id);

ALTER TABLE ONLY public.commitment_lineages
    ADD CONSTRAINT commitment_lineages_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.coordination_summary_configurations
    ADD CONSTRAINT coordination_summary_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.coordination_summary_configurations(id);

ALTER TABLE ONLY public.coordination_summary_requests
    ADD CONSTRAINT coordination_summary_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT dependencies_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT dependency_admission_outcomes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.dependency_dismissals
    ADD CONSTRAINT dependency_dismissals_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.dependency_event_evidence
    ADD CONSTRAINT dependency_event_evidence_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);

ALTER TABLE ONLY public.dependency_event_migration_receipts
    ADD CONSTRAINT dependency_event_migration_receipts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisi_supersedes_scope_decision_id_fkey FOREIGN KEY (supersedes_scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);

ALTER TABLE ONLY public.dependency_event_scope_decisions
    ADD CONSTRAINT dependency_event_scope_decisions_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT dependency_event_scopes_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.dependency_event_timings
    ADD CONSTRAINT dependency_event_timings_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT dependency_evidence_sufficiencies_evidence_link_id_fkey FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);

ALTER TABLE ONLY public.discovered_references
    ADD CONSTRAINT discovered_references_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_newer_assertion_id_fkey FOREIGN KEY (newer_assertion_id) REFERENCES public.assertions(id);

ALTER TABLE ONLY public.dispute_history_resolutions
    ADD CONSTRAINT dispute_history_resolutions_older_assertion_id_fkey FOREIGN KEY (older_assertion_id) REFERENCES public.assertions(id);

ALTER TABLE ONLY public.dispute_settlements
    ADD CONSTRAINT dispute_settlements_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.doc_pages
    ADD CONSTRAINT doc_pages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.document_notification_dispatches(id);

ALTER TABLE ONLY public.document_notification_attempts
    ADD CONSTRAINT document_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.document_notifications(id);

ALTER TABLE ONLY public.document_notification_dispatches
    ADD CONSTRAINT document_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_predecessor_document_id_fkey FOREIGN KEY (predecessor_document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_review_confirmation_id_fkey FOREIGN KEY (review_confirmation_id) REFERENCES public.documentation_field_confirmations(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_statement_event_id_fkey FOREIGN KEY (statement_event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.document_notifications
    ADD CONSTRAINT document_notifications_successor_document_id_fkey FOREIGN KEY (successor_document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.document_quarantines
    ADD CONSTRAINT document_quarantines_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT document_rendition_derivations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT documentation_field_confirmations_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.due_action_notification_dispatches(id);

ALTER TABLE ONLY public.due_action_notification_attempts
    ADD CONSTRAINT due_action_notification_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES public.due_action_notifications(id);

ALTER TABLE ONLY public.due_action_notification_dispatches
    ADD CONSTRAINT due_action_notification_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_plan_decision_id_fkey FOREIGN KEY (plan_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.due_action_notifications
    ADD CONSTRAINT due_action_notifications_recipient_roster_entry_id_fkey FOREIGN KEY (recipient_roster_entry_id) REFERENCES public.project_roster_entries(id);

ALTER TABLE ONLY public.due_work_occurrences
    ADD CONSTRAINT due_work_occurrences_scheduled_job_id_fkey FOREIGN KEY (scheduled_job_id) REFERENCES public.due_work_schedules(id);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_occurrence_id_fkey FOREIGN KEY (occurrence_id) REFERENCES public.due_work_occurrences(id);

ALTER TABLE ONLY public.due_work_receipts
    ADD CONSTRAINT due_work_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.due_work_schedules
    ADD CONSTRAINT due_work_schedules_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.event_admission_acceptance_receipts
    ADD CONSTRAINT event_admission_acceptance_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_acceptance_receipt_id_fkey FOREIGN KEY (acceptance_receipt_id) REFERENCES public.event_admission_acceptance_receipts(id);

ALTER TABLE ONLY public.event_admission_activations
    ADD CONSTRAINT event_admission_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT event_admission_outcomes_dependency_event_id_fkey FOREIGN KEY (dependency_event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.event_cohort_receipts
    ADD CONSTRAINT event_cohort_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_start_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.evidence_investigation_candidate_review_starts
    ADD CONSTRAINT evidence_investigation_candidate_review_starts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_capture_contracts
    ADD CONSTRAINT evidence_investigation_capture_contracts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_capture_contract_id_fkey FOREIGN KEY (capture_contract_id) REFERENCES public.evidence_investigation_capture_contracts(id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);

ALTER TABLE ONLY public.evidence_investigation_capture_results
    ADD CONSTRAINT evidence_investigation_capture_results_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);

ALTER TABLE ONLY public.evidence_investigation_packet_receipts
    ADD CONSTRAINT evidence_investigation_packet_receipts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);

ALTER TABLE ONLY public.evidence_investigation_review_observations
    ADD CONSTRAINT evidence_investigation_review_observations_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);

ALTER TABLE ONLY public.evidence_investigation_runs
    ADD CONSTRAINT evidence_investigation_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_cases
    ADD CONSTRAINT evidence_investigation_shadow_cases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_executions
    ADD CONSTRAINT evidence_investigation_shadow_executions_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);

ALTER TABLE ONLY public.evidence_investigation_shadow_outcomes
    ADD CONSTRAINT evidence_investigation_shadow_outcomes_shadow_case_id_fkey FOREIGN KEY (shadow_case_id) REFERENCES public.evidence_investigation_shadow_cases(id);

ALTER TABLE ONLY public.evidence_investigation_step_receipts
    ADD CONSTRAINT evidence_investigation_step_receipts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.evidence_investigation_runs(id);

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.evidence_links
    ADD CONSTRAINT evidence_links_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.external_report_artifacts
    ADD CONSTRAINT external_report_artifacts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT external_report_releases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_configurations
    ADD CONSTRAINT extraction_failure_diagnosis_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.extraction_failure_diagnosis_configurations(id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_extraction_run_id_fkey FOREIGN KEY (extraction_run_id) REFERENCES public.extraction_runs(id);

ALTER TABLE ONLY public.extraction_failure_diagnosis_requests
    ADD CONSTRAINT extraction_failure_diagnosis_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_predecessor_state_id_fkey FOREIGN KEY (predecessor_state_id) REFERENCES public.extraction_measurement_case_states(id);

ALTER TABLE ONLY public.extraction_measurement_case_states
    ADD CONSTRAINT extraction_measurement_case_states_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.extraction_runs
    ADD CONSTRAINT extraction_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.retired_automatic_carry_forward_policy_activations
    ADD CONSTRAINT fk_active_automatic_carry_forward_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);

ALTER TABLE ONLY public.active_run_declarations
    ADD CONSTRAINT fk_active_run_declarations_predecessor FOREIGN KEY (document_id, predecessor_declaration_id) REFERENCES public.active_run_declarations(document_id, id);

ALTER TABLE ONLY public.active_extraction_runs
    ADD CONSTRAINT fk_active_run_document_extraction_run FOREIGN KEY (document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT fk_automatic_carry_forward_outcome_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);

ALTER TABLE ONLY public.automatic_carry_forward_outcomes
    ADD CONSTRAINT fk_automatic_carry_forward_outcome_run_project FOREIGN KEY (project_id, family, run_id) REFERENCES public.policy_runs(project_id, family, id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT fk_automatic_carry_forward_receipt_dependency_evidence FOREIGN KEY (dependency_id, new_evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);

ALTER TABLE ONLY public.automatic_carry_forward_receipts
    ADD CONSTRAINT fk_automatic_carry_forward_receipt_policy_project FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);

ALTER TABLE ONLY public.candidates
    ADD CONSTRAINT fk_candidates_document_extraction_run FOREIGN KEY (source_document_id, extraction_run_id) REFERENCES public.extraction_runs(document_id, id);

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_basis_event FOREIGN KEY (basis_event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_basis_evidence FOREIGN KEY (basis_evidence_link_id) REFERENCES public.evidence_links(id);

ALTER TABLE ONLY public.condition_resolutions
    ADD CONSTRAINT fk_condition_resolution_owned_evidence FOREIGN KEY (dependency_id, evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_external_org_id_external_orgs_id FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_milestone_id_milestones_id FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);

ALTER TABLE ONLY public.dependencies
    ADD CONSTRAINT fk_dependencies_milestone_registration FOREIGN KEY (milestone_registration_id) REFERENCES public.milestone_registrations(id);

ALTER TABLE ONLY public.dependency_admission_outcomes
    ADD CONSTRAINT fk_dependency_admission_outcomes_run_family FOREIGN KEY (family, policy_run_id) REFERENCES public.policy_runs(family, id);

ALTER TABLE ONLY public.dependency_event_scopes
    ADD CONSTRAINT fk_dependency_event_scopes_scope_decision FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_affected_external_org FOREIGN KEY (affected_external_org_id) REFERENCES public.external_orgs(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_closes_commitment_lineage FOREIGN KEY (closes_commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_commitment_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_project FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_stated_external_org FOREIGN KEY (stated_external_org_id) REFERENCES public.external_orgs(id);

ALTER TABLE ONLY public.dependency_events
    ADD CONSTRAINT fk_dependency_events_supersedes_event FOREIGN KEY (supersedes_event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.dependency_evidence_sufficiencies
    ADD CONSTRAINT fk_dependency_evidence_sufficiencies_scope_link FOREIGN KEY (scope_link_id) REFERENCES public.dependency_event_scopes(id);

ALTER TABLE ONLY public.documentation_field_confirmations
    ADD CONSTRAINT fk_documentation_confirmation_owned_evidence FOREIGN KEY (dependency_id, evidence_link_id) REFERENCES public.evidence_links(dependency_id, id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_superseded_by_same_project FOREIGN KEY (project_id, superseded_by) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_supersession_source_page FOREIGN KEY (supersession_source_document_id, supersession_source_page) REFERENCES public.doc_pages(document_id, page_no);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_supersession_source_same_project FOREIGN KEY (project_id, supersession_source_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_audit FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_candidate_disposition FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcome_scope_decision FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);

ALTER TABLE ONLY public.event_admission_outcomes
    ADD CONSTRAINT fk_event_admission_outcomes_run_family FOREIGN KEY (family, policy_run_id) REFERENCES public.policy_runs(family, id);

ALTER TABLE ONLY public.external_report_releases
    ADD CONSTRAINT fk_external_report_releases_artifact FOREIGN KEY (artifact_id) REFERENCES public.external_report_artifacts(id);

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT fk_inbound_threads_bound_by_message FOREIGN KEY (bound_by_message_id) REFERENCES public.inbound_messages(id);

ALTER TABLE ONLY public.key_date_draft_receipts
    ADD CONSTRAINT fk_key_date_draft_receipts_source_same_project FOREIGN KEY (project_id, source_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT fk_milestones_current_registration FOREIGN KEY (current_registration_id) REFERENCES public.milestone_registrations(id);

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT fk_operative_support_evidence_link FOREIGN KEY (evidence_link_id) REFERENCES public.evidence_links(id);

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT fk_operative_support_scope_link FOREIGN KEY (scope_link_id) REFERENCES public.dependency_event_scopes(id);

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT fk_policy_runs_approval_project_family FOREIGN KEY (project_id, family, policy_approval_id) REFERENCES public.policy_approvals(project_id, family, id);

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT fk_rendition_derivation_derived_same_project FOREIGN KEY (project_id, derived_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.document_rendition_derivations
    ADD CONSTRAINT fk_rendition_derivation_source_same_project FOREIGN KEY (project_id, source_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_predecessor_project FOREIGN KEY (project_id, predecessor_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_predecessor_run FOREIGN KEY (predecessor_document_id, predecessor_extraction_run_id) REFERENCES public.extraction_runs(document_id, id);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_successor_project FOREIGN KEY (project_id, successor_document_id) REFERENCES public.documents(project_id, id);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT fk_revision_comparison_successor_run FOREIGN KEY (successor_document_id, successor_extraction_run_id) REFERENCES public.extraction_runs(document_id, id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT fk_statement_coordination_receipt_disposition FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_commitment_lineage FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_commitment_lineage_predecessor FOREIGN KEY (commitment_lineage_id, predecessor_decision_id) REFERENCES public.work_decisions(commitment_lineage_id, id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT fk_work_decisions_predecessor FOREIGN KEY (dependency_id, predecessor_decision_id) REFERENCES public.work_decisions(dependency_id, id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_decision_id_fkey FOREIGN KEY (internal_owner_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_internal_owner_roster_entry_id_fkey FOREIGN KEY (internal_owner_roster_entry_id) REFERENCES public.project_roster_entries(id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_next_action_decision_id_fkey FOREIGN KEY (next_action_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_receipts
    ADD CONSTRAINT follow_up_plan_receipts_resumed_deferral_decision_id_fkey FOREIGN KEY (resumed_deferral_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_deferral_reversal_decision_id_fkey FOREIGN KEY (deferral_reversal_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_internal_owner_reversal_decision__fkey FOREIGN KEY (internal_owner_reversal_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_next_action_reversal_decision_id_fkey FOREIGN KEY (next_action_reversal_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.follow_up_plan_reversals
    ADD CONSTRAINT follow_up_plan_reversals_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.follow_up_plan_receipts(id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_resolved_project_id_fkey FOREIGN KEY (resolved_project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.inbound_route_triage
    ADD CONSTRAINT inbound_route_triage_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_closing_message_id_fkey FOREIGN KEY (closing_message_id) REFERENCES public.inbound_messages(id);

ALTER TABLE ONLY public.inbound_thread_readings
    ADD CONSTRAINT inbound_thread_readings_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.inbound_threads(id);

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.inbound_threads
    ADD CONSTRAINT inbound_threads_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.intake_project_identifiers
    ADD CONSTRAINT intake_project_identifiers_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.key_date_draft_row_receipts
    ADD CONSTRAINT key_date_draft_row_receipts_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.key_date_draft_receipts(id);

ALTER TABLE ONLY public.legacy_ledger_archives
    ADD CONSTRAINT legacy_ledger_archives_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);

ALTER TABLE ONLY public.milestone_registrations
    ADD CONSTRAINT milestone_registrations_predecessor_registration_id_fkey FOREIGN KEY (predecessor_registration_id) REFERENCES public.milestone_registrations(id);

ALTER TABLE ONLY public.milestones
    ADD CONSTRAINT milestones_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.operative_support
    ADD CONSTRAINT operative_support_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.organization_identity_activations
    ADD CONSTRAINT organization_identity_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_external_org_id_fkey FOREIGN KEY (external_org_id) REFERENCES public.external_orgs(id);

ALTER TABLE ONLY public.organization_identity_receipts
    ADD CONSTRAINT organization_identity_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.policy_approvals
    ADD CONSTRAINT policy_approvals_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.policy_runs
    ADD CONSTRAINT policy_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.production_run_explanation_configurations
    ADD CONSTRAINT production_run_explanation_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.production_run_explanation_configurations(id);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.production_run_explanation_requests
    ADD CONSTRAINT production_run_explanation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.project_check_configurations
    ADD CONSTRAINT project_check_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.project_roster_entries
    ADD CONSTRAINT project_roster_entries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.reconfirmation_receipts
    ADD CONSTRAINT reconfirmation_receipts_successor_candidate_id_fkey FOREIGN KEY (successor_candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.record_inclusion_requests
    ADD CONSTRAINT record_inclusion_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.retired_dependency_statuses
    ADD CONSTRAINT retired_dependency_statuses_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.revision_change_explanation_configurations
    ADD CONSTRAINT revision_change_explanation_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_comparison_id_fkey FOREIGN KEY (comparison_id) REFERENCES public.revision_comparison_runs(id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.revision_change_explanation_configurations(id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_finding_id_fkey FOREIGN KEY (finding_id) REFERENCES public.revision_comparison_findings(id);

ALTER TABLE ONLY public.revision_change_explanation_requests
    ADD CONSTRAINT revision_change_explanation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.revision_comparison_findings
    ADD CONSTRAINT revision_comparison_findings_revision_comparison_run_id_fkey FOREIGN KEY (revision_comparison_run_id) REFERENCES public.revision_comparison_runs(id);

ALTER TABLE ONLY public.revision_comparison_runs
    ADD CONSTRAINT revision_comparison_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.revision_reconciliation_requests
    ADD CONSTRAINT revision_reconciliation_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.schedule_governing_derivations
    ADD CONSTRAINT schedule_governing_derivations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.schedule_link_activations
    ADD CONSTRAINT schedule_link_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_milestone_registration_id_fkey FOREIGN KEY (milestone_registration_id) REFERENCES public.milestone_registrations(id);

ALTER TABLE ONLY public.schedule_link_receipts
    ADD CONSTRAINT schedule_link_receipts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_occurrence_id_fkey FOREIGN KEY (occurrence_id) REFERENCES public.due_work_occurrences(id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.scheduled_report_publications
    ADD CONSTRAINT scheduled_report_publications_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.due_work_schedules(id);

ALTER TABLE ONLY public.source_fetch_attempts
    ADD CONSTRAINT source_fetch_attempts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.source_intake_draft_configurations
    ADD CONSTRAINT source_intake_draft_configurations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_configuration_id_fkey FOREIGN KEY (configuration_id) REFERENCES public.source_intake_draft_configurations(id);

ALTER TABLE ONLY public.source_intake_draft_requests
    ADD CONSTRAINT source_intake_draft_requests_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receip_internal_owner_roster_entry__fkey FOREIGN KEY (internal_owner_roster_entry_id) REFERENCES public.project_roster_entries(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receip_milestone_impact_decision_id_fkey FOREIGN KEY (milestone_impact_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_commitment_lineage_id_fkey FOREIGN KEY (commitment_lineage_id) REFERENCES public.commitment_lineages(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_dependency_event_id_fkey FOREIGN KEY (dependency_event_id) REFERENCES public.dependency_events(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_internal_owner_decision_id_fkey FOREIGN KEY (internal_owner_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_next_action_decision_id_fkey FOREIGN KEY (next_action_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.statement_coordination_receipts
    ADD CONSTRAINT statement_coordination_receipts_scope_decision_id_fkey FOREIGN KEY (scope_decision_id) REFERENCES public.dependency_event_scope_decisions(id);

ALTER TABLE ONLY public.statement_coordination_reversal_effects
    ADD CONSTRAINT statement_coordination_reversal_effects_reversal_id_fkey FOREIGN KEY (reversal_id) REFERENCES public.statement_coordination_reversals(id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_audit_log_id_fkey FOREIGN KEY (audit_log_id) REFERENCES public.audit_log(id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_disposition_id_fkey FOREIGN KEY (candidate_disposition_id) REFERENCES public.candidate_dispositions(id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.statement_coordination_reversals
    ADD CONSTRAINT statement_coordination_reversals_receipt_id_fkey FOREIGN KEY (receipt_id) REFERENCES public.statement_coordination_receipts(id);

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.statement_suggestion_eligibility_declarations
    ADD CONSTRAINT statement_suggestion_eligibility_declarations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.statement_suggestion_protection_ends
    ADD CONSTRAINT statement_suggestion_protection_ends_protection_id_fkey FOREIGN KEY (protection_id) REFERENCES public.statement_suggestion_protections(id);

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_candidate_id_fkey FOREIGN KEY (candidate_id) REFERENCES public.candidates(id);

ALTER TABLE ONLY public.statement_suggestion_protections
    ADD CONSTRAINT statement_suggestion_protections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.unreadable_cell_admission_activations
    ADD CONSTRAINT unreadable_cell_admission_activations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.unreadable_cell_reading_profiles
    ADD CONSTRAINT unreadable_cell_reading_profiles_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.unreadable_cell_reading_profiles(id);

ALTER TABLE ONLY public.unreadable_cell_reading_runs
    ADD CONSTRAINT unreadable_cell_reading_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.unreadable_cell_reading_steps
    ADD CONSTRAINT unreadable_cell_reading_steps_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.unreadable_cell_reading_runs(id);

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_corroboration_document_id_fkey FOREIGN KEY (corroboration_document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id);

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);

ALTER TABLE ONLY public.unreadable_cell_resolutions
    ADD CONSTRAINT unreadable_cell_resolutions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.unreadable_cell_reading_runs(id);

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_milestone_id_fkey FOREIGN KEY (milestone_id) REFERENCES public.milestones(id);

ALTER TABLE ONLY public.work_decision_milestone_impacts
    ADD CONSTRAINT work_decision_milestone_impacts_work_decision_id_fkey FOREIGN KEY (work_decision_id) REFERENCES public.work_decisions(id);

ALTER TABLE ONLY public.work_decisions
    ADD CONSTRAINT work_decisions_dependency_id_fkey FOREIGN KEY (dependency_id) REFERENCES public.dependencies(id);

GRANT USAGE ON SCHEMA public TO corridor_statement_retirement;

REVOKE ALL ON FUNCTION public.purge_external_party_statement_rows(target_project_id bigint, target_purpose text) FROM PUBLIC;

GRANT SELECT,DELETE ON TABLE public.commitment_lineages TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.dependency_event_evidence TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.dependency_event_scope_decisions TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.dependency_event_scopes TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.dependency_event_timings TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.dependency_events TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.evidence_links TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.legacy_ledger_archives TO corridor_statement_retirement;

GRANT SELECT,DELETE ON TABLE public.projects TO corridor_statement_retirement;

GRANT SELECT ON TABLE public.work_decisions TO corridor_statement_retirement;
