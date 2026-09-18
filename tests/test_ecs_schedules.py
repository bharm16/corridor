"""Scheduled writers remain stopped until their released target is verified."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from corridor import ecs_schedules as schedules

RULE = 'expiry'
ARN = 'arn:aws:events:us-east-2:123456789012:rule/expiry'
CLUSTER = 'arn:aws:ecs:us-east-2:123456789012:cluster/corridor'
OLD = 'arn:aws:ecs:us-east-2:123456789012:task-definition/batch:1'
NEW = OLD.rsplit(':', 1)[0] + ':2'
REPO = '123456789012.dkr.ecr.us-east-2.amazonaws.com/corridor'
DIGEST = 'sha256:' + 'a' * 64


class EventBridge:
    def __init__(self, state='ENABLED'):
        self.state, self.tags, self.calls = state, {}, []
        self.fail_update = False
        self.target = {'Id': 'one', 'Arn': CLUSTER, 'RoleArn': 'role', 'Input': '{"command":"expiry"}',
            'EcsParameters': {'TaskDefinitionArn': OLD, 'TaskCount': 1},
            'RetryPolicy': {'MaximumRetryAttempts': 0, 'MaximumEventAgeInSeconds': 60}}
    def describe_rule(self, *, Name):
        assert Name == RULE
        return {'Name': Name, 'Arn': ARN, 'State': self.state}
    def get_paginator(self, operation):
        assert operation == 'list_targets_by_rule'
        return SimpleNamespace(paginate=lambda **kwargs: [{'Targets': [deepcopy(self.target)]}])
    def list_tags_for_resource(self, **kwargs):
        return {'Tags': [{'Key': key, 'Value': value} for key, value in self.tags.items()]}
    def tag_resource(self, *, ResourceARN, Tags):
        assert ResourceARN == ARN
        self.tags.update({r['Key']: r['Value'] for r in Tags})
    def untag_resource(self, *, ResourceARN, TagKeys):
        for key in TagKeys: self.tags.pop(key, None)
    def disable_rule(self, **kwargs):
        self.calls.append('disable'); self.state = 'DISABLED'
    def enable_rule(self, **kwargs):
        self.calls.append('enable'); self.state = 'ENABLED'
    def put_targets(self, *, Rule, Targets):
        assert self.state == 'DISABLED'
        self.calls.append('target')
        if self.fail_update: return {'FailedEntryCount': 1, 'FailedEntries': [{'TargetId': 'one'}]}
        self.target = deepcopy(Targets[0])
        return {'FailedEntryCount': 0, 'FailedEntries': []}


class Clock:
    now = 1000.0
    def wait(self, seconds): self.now += seconds
    def time(self): return self.now


def pause(events, clock):
    return schedules.pause_schedule(events, rule_name=RULE, cluster=CLUSTER,
        task_definition_arn=OLD, wait=clock.wait, now=clock.time)


def resume(events, clock, task=NEW):
    client = SimpleNamespace(describe_task_definition=lambda **kwargs: {'taskDefinition': {
        'taskDefinitionArn': task, 'containerDefinitions': [{'name': 'batch', 'image': REPO + '@' + DIGEST}]}})
    return schedules.resume_schedule(events, client, rule_name=RULE, cluster=CLUSTER,
        task_definition_arn=task, repository_uri=REPO, digest=DIGEST, wait=clock.wait, now=clock.time)


def test_pause_retry_preserves_original_intent_and_two_releases_move_the_target():
    events, clock = EventBridge(), Clock()
    original = deepcopy(events.target)
    pause(events, clock)
    assert events.state == 'DISABLED'
    # A failed release leaves its original ENABLED intent in provider custody.
    pause(events, clock)
    resume(events, clock)
    assert events.state == 'ENABLED' and events.tags == {}
    expected = deepcopy(original); expected['EcsParameters']['TaskDefinitionArn'] = NEW
    assert events.target == expected
    pause(events, clock)
    third = OLD.rsplit(':', 1)[0] + ':3'
    resume(events, clock, third)
    assert events.target['EcsParameters']['TaskDefinitionArn'] == third
    assert events.calls[-2:] == ['target', 'enable']


def test_initially_disabled_schedule_stays_disabled_after_release():
    events, clock = EventBridge('DISABLED'), Clock()
    pause(events, clock); resume(events, clock)
    assert events.state == 'DISABLED' and 'enable' not in events.calls


def test_partial_target_failure_keeps_schedule_paused_with_recovery_intent():
    events, clock = EventBridge(), Clock()
    pause(events, clock); events.fail_update = True
    with pytest.raises(schedules.ScheduleRefused, match='target'):
        resume(events, clock)
    assert events.state == 'DISABLED' and events.tags
    events.fail_update = False
    pause(events, clock); resume(events, clock)
    assert events.state == 'ENABLED'


def test_unbounded_delivery_retry_policy_refuses_before_mutation():
    events, clock = EventBridge(), Clock()
    events.target.pop('RetryPolicy')
    with pytest.raises(schedules.ScheduleRefused, match='retry'):
        pause(events, clock)
    assert events.calls == []
