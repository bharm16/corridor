"""Pause scheduled writers and bind them to the same image a release proved.

The expiry rule used to sit outside release/drain/disposition: it could launch
an old Batch revision during migration. Its original state now survives a
failed release in rule tags. Only a verified new target permits restoration.
AWS rule/target changes propagate asynchronously, so delivery has no retries,
a one-minute event-age limit, and a quiet interval before the task census.
This module creates no SDK client; both release and disposition inject theirs.
"""
from __future__ import annotations

from copy import deepcopy
import math
import time

from corridor.ecs_tasks import verify_task_definition

PRIOR_STATE_TAG = "corridor:maintenance-prior-state"
PAUSED_AT_TAG = "corridor:maintenance-paused-at"
MAX_EVENT_AGE_SECONDS = 60
QUIET_SECONDS = MAX_EVENT_AGE_SECONDS + 5
RETRY_POLICY = {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": MAX_EVENT_AGE_SECONDS}


class ScheduleRefused(RuntimeError):
    """The schedule has not proved it can safely be paused or restored."""


def _read(events, rule_name):
    rule = events.describe_rule(Name=rule_name)
    if rule.get("Name") != rule_name or not rule.get("Arn") or rule.get("State") not in {"ENABLED", "DISABLED"}:
        raise ScheduleRefused("schedule identity or state is unavailable")
    targets = [target for page in events.get_paginator("list_targets_by_rule").paginate(Rule=rule_name)
               for target in page["Targets"]]
    if len(targets) != 1 or not targets[0].get("Id") or not targets[0].get("EcsParameters", {}).get("TaskDefinitionArn"):
        raise ScheduleRefused("schedule needs exactly one declared ECS target")
    if targets[0].get("RetryPolicy") != RETRY_POLICY:
        raise ScheduleRefused("schedule delivery retry/age policy must be bounded before maintenance")
    tags = {tag["Key"]: tag["Value"] for tag in events.list_tags_for_resource(ResourceARN=rule["Arn"])["Tags"]}
    return rule, targets[0], tags


def _require_target(rule, target, cluster, task_definition_arn):
    arn = rule["Arn"].split(":")
    expected_cluster = f"arn:{arn[1]}:ecs:{arn[3]}:{arn[4]}:cluster/{cluster.rsplit('/', 1)[-1]}"
    if (target.get("Arn") != expected_cluster
            or target["EcsParameters"]["TaskDefinitionArn"].rsplit(":", 1)[0] != task_definition_arn.rsplit(":", 1)[0]):
        raise ScheduleRefused("schedule target is outside the declared cluster or task family")


def require_paused_schedule(events, *, rule_name: str, now=time.time) -> dict:
    """Read-only proof that launch suppression has had its quiet interval."""
    rule, target, tags = _read(events, rule_name)
    try:
        paused_at = float(tags[PAUSED_AT_TAG])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScheduleRefused("schedule lacks a retained pause observation") from exc
    if (rule["State"] != "DISABLED" or tags.get(PRIOR_STATE_TAG) not in {"ENABLED", "DISABLED"}
            or not math.isfinite(paused_at) or now() - paused_at < QUIET_SECONDS):
        raise ScheduleRefused("schedule must remain disabled through its quiet interval")
    return target


def pause_schedule(events, *, rule_name: str, cluster: str, task_definition_arn: str,
                   wait=time.sleep, now=time.time) -> dict:
    """Retain original intent before disabling; retries keep the first intent."""
    rule, target, tags = _read(events, rule_name)
    _require_target(rule, target, cluster, task_definition_arn)
    prior = tags.get(PRIOR_STATE_TAG, rule["State"])
    if prior not in {"ENABLED", "DISABLED"}:
        raise ScheduleRefused("schedule's retained restoration intent is invalid")
    events.tag_resource(ResourceARN=rule["Arn"], Tags=[{"Key": PRIOR_STATE_TAG, "Value": prior}])
    events.disable_rule(Name=rule_name)
    if events.describe_rule(Name=rule_name).get("State") != "DISABLED":
        raise ScheduleRefused("schedule disable was not observed")
    events.tag_resource(ResourceARN=rule["Arn"], Tags=[{"Key": PAUSED_AT_TAG, "Value": str(now())}])
    wait(QUIET_SECONDS)
    if require_paused_schedule(events, rule_name=rule_name, now=now) != target:
        raise ScheduleRefused("schedule target changed while pausing")
    return {"rule": rule_name, "prior_state": prior, "state": "DISABLED"}


def resume_schedule(events, ecs, *, rule_name: str, cluster: str, task_definition_arn: str,
                    repository_uri: str, digest: str, wait=time.sleep, now=time.time) -> dict:
    """Repoint only the task revision, verify it, then restore the original state."""
    require_paused_schedule(events, rule_name=rule_name, now=now)
    rule, target, tags = _read(events, rule_name)
    _require_target(rule, target, cluster, task_definition_arn)
    verify_task_definition(ecs, task_definition_arn=task_definition_arn,
                           repository_uri=repository_uri, digest=digest)
    replacement = deepcopy(target)
    replacement["EcsParameters"]["TaskDefinitionArn"] = task_definition_arn
    result = events.put_targets(Rule=rule_name, Targets=[replacement])
    if result.get("FailedEntryCount") != 0 or result.get("FailedEntries"):
        raise ScheduleRefused("schedule target update failed")
    wait(QUIET_SECONDS)
    observed_rule, observed_target, observed_tags = _read(events, rule_name)
    if observed_rule["State"] != "DISABLED" or observed_target != replacement or observed_tags != tags:
        raise ScheduleRefused("schedule target/state changed before restoration")
    prior = tags[PRIOR_STATE_TAG]
    if prior == "ENABLED":
        events.enable_rule(Name=rule_name)
    if events.describe_rule(Name=rule_name).get("State") != prior:
        # Keep it closed if enable/readback was incomplete. Retained tags let
        # the next release recover the original intent rather than guess it.
        events.disable_rule(Name=rule_name)
        raise ScheduleRefused("schedule restoration was not observed")
    events.untag_resource(ResourceARN=rule["Arn"], TagKeys=[PRIOR_STATE_TAG, PAUSED_AT_TAG])
    return {"rule": rule_name, "state": prior, "task_definition": task_definition_arn, "digest": digest}
