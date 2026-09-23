import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_v051 import memory_db
from test_v071_changes import candidate

from app.ai.queue import enqueue_change_analysis
from app.db import ChangeAnalysis, ChangeEvent, ChangeFilterAudit, ChangeFilterRule
from app.services.change_filters import FilterSpec, matches, mutate_from_review, set_rule_enabled
from app.services.change_review import normalized_candidate


def rule_for(context):
    return {**{key: context[key] for key in ('course_id', 'source_type', 'source_name', 'change_type')},
        'field': 'title', 'operator': 'contains', 'pattern': 'weekly banner'}


def rejected_family(db):
    event = candidate(db, field='title', old='Weekly banner old', new='Weekly banner new', kind='CONTENT_UPDATED')
    context = normalized_candidate(db, event)
    for i in range(3):
        ev = ChangeEvent(source_item_id=event.source_item_id, change_type=event.change_type,
            old_snapshot_id=event.old_snapshot_id, new_snapshot_id=event.new_snapshot_id, summary=f'Noise {i}')
        db.add(ev)
        db.flush()
        db.add(ChangeAnalysis(change_event_id=ev.id, input_hash=str(i), analysis_status='READY',
            analysis_json={'review_version':'0.7.1', 'notify_user':False, 'context':context}))
    db.flush()
    return event, context


def test_repeated_noise_rule_suppresses_future_and_audits_without_deleting_history():
    with memory_db() as db:
        event, context = rejected_family(db)
        row = mutate_from_review(db, {'operation':'create','rule':rule_for(context),'reason':'Three repeated banners'}, context)
        assert row.created_by == 'AI'
        assert enqueue_change_analysis(db, event) is None
        analysis = db.scalar(select(ChangeAnalysis).where(ChangeAnalysis.change_event_id==event.id))
        assert analysis.analysis_json['matched_rule']['id'] == row.id
        assert row.match_count == 1
        assert enqueue_change_analysis(db, event) is None
        assert row.match_count == 1
        set_rule_enabled(db, row.id, False)
        assert row.enabled is False
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 4
        with pytest.raises(ValueError, match='user_controlled'):
            mutate_from_review(db, {'operation':'create','rule':rule_for(context),'reason':'Repeat'}, context)
        assert len(db.scalars(select(ChangeFilterAudit)).all()) == 2


def test_same_family_updates_instead_of_duplicate_and_user_can_remove_restore():
    with memory_db() as db:
        _, context = rejected_family(db)
        payload = {'operation':'create','rule':rule_for(context),'reason':'Repeated evidence'}
        row = mutate_from_review(db, payload, context)
        payload['rule']['pattern'] = 'weekly banner new'
        same = mutate_from_review(db, payload, context)
        assert same.id == row.id and same.version == 2
        assert db.scalar(select(func.count()).select_from(ChangeFilterRule)) == 1
        set_rule_enabled(db, row.id, False, remove=True)
        assert row.removed
        set_rule_enabled(db, row.id, True)
        assert row.enabled and not row.removed


@pytest.mark.parametrize('patch', [ {'operator':'regex'}, {'pattern':'x'*161}, {'pattern':'x'},
    {'action':'ALWAYS_PUSH_HOME'}, {'expression':'__import__("os")'}, {'field':'due_at'}, {'course_id':0} ])
def test_malformed_regex_executable_and_oversized_rules_are_rejected(patch):
    base = {'course_id':1,'source_type':'canvas','source_name':'test','change_type':'CONTENT_UPDATED',
        'field':'title','operator':'contains','pattern':'weekly banner'}
    with pytest.raises(ValidationError):
        FilterSpec.model_validate({**base, **patch})


def test_one_rejection_cannot_install_rule_or_suppress_multi_field_academic_change():
    with memory_db() as db:
        event = candidate(db, field='title', old='old', new='weekly banner new', kind='CONTENT_UPDATED')
        context = normalized_candidate(db, event)
        spec = rule_for(context)
        with pytest.raises(ValueError, match='three_rejected'):
            mutate_from_review(db, {'operation':'create','rule':spec,'reason':'Noise'}, context)
        assert not matches(FilterSpec.model_validate(spec), {**context,'fields':['due_at','title']})
        assert not matches(FilterSpec.model_validate(spec), {**context,'course_id':999})
