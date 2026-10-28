from copy import deepcopy

import pytest
from sqlalchemy import func, select
from test_v051 import add_course, make_settings, memory_db

from app.api.routes import changes, unread_change_count
from app.auth.models import FetchResult, FetchStatus
from app.db import ChangeEvent, CourseSource, DownloadedFile, PendingResource, SourceSnapshot, Task
from app.schemas import RawSourceItem
from app.services.change_policy import grade_is_released, is_user_facing_event
from app.services.sync import SyncService
from app.sources.website import WebsiteAdapter

BASE = 'https://example.edu/phys225/fa2026'


class Fetcher:
    def __init__(self, protected=FetchStatus.OK, links=True):
        self.protected, self.links, self.calls = protected, links, []

    async def fetch(self, url, auth_rule=None):
        self.calls.append(url)
        if url.endswith('.pdf'):
            return FetchResult(status=self.protected, url=url,
                content=b'%PDF-1.4\nlecture' if self.protected == FetchStatus.OK else None,
                content_type='application/pdf', status_code=200 if self.protected == FetchStatus.OK else 401)
        link = '<a href="schedule.html">Schedule</a>' if url == BASE else (
            '<a href="secure/week5.pdf">Week 5</a>' if self.links else '')
        return FetchResult(status=FetchStatus.OK, url=url + '/' if url == BASE else url,
            content=f'<html><body>{link}</body></html>'.encode(), content_type='text/html', status_code=200)


def website(fetcher, **kwargs):
    return WebsiteAdapter(name='TEST', course_code='TEST', base_url=BASE,
        discovery={'allowed_path_prefixes': ['/phys225/fa2026/'], **kwargs.pop('discovery', {})},
        fetcher=fetcher, **kwargs)


@pytest.mark.asyncio
async def test_default_depth_reaches_schedule_files_and_repeated_sync_dedupes(tmp_path):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        service = SyncService(make_settings(tmp_path))
        fetcher = Fetcher()
        adapter = website(fetcher)
        first = await service.run(db, [(course, adapter)])
        assert first.state == 'healthy'
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1
        events = list(db.scalars(select(ChangeEvent)))
        assert [event.change_type for event in events] == ['NEW_FILE']
        assert is_user_facing_event(db, events[0])
        assert BASE + '/secure/week5.pdf' in fetcher.calls
        await service.run(db, [(course, adapter)])
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 1


@pytest.mark.asyncio
async def test_direct_pending_retry_works_with_depth_zero_and_updates_source_health(tmp_path):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        source = CourseSource(course_id=course.id, name='TEST', source_type='website', url=BASE)
        pending = PendingResource(course_id=course.id, source_name='TEST', url=BASE + '/secure/week5.pdf',
            title='Week5', item_type='website_file', state='AUTH_REQUIRED')
        db.add_all([source, pending])
        db.commit()
        service = SyncService(make_settings(tmp_path))
        fetcher = Fetcher(FetchStatus.AUTH_REQUIRED, links=False)
        adapter = website(fetcher, discovery={'max_depth': 0})
        adapter.course_source_id = source.id
        state = await service.run(db, [(course, adapter)])
        assert state.state == 'degraded' and source.state == 'AUTH_REQUIRED'
        assert pending.state == 'AUTH_REQUIRED'
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 0
        fetcher.protected = FetchStatus.OK
        state = await service.run(db, [(course, adapter)])
        assert state.state == 'healthy' and source.state == 'HEALTHY'
        assert pending.state == 'FETCHED'
        assert db.scalar(select(func.count()).select_from(DownloadedFile)) == 1


@pytest.mark.asyncio
async def test_retry_still_enforces_scope_and_path_budget(tmp_path):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        fetcher = Fetcher()
        adapter = website(fetcher, discovery_path_limit=2)
        adapter.retry_urls = ['https://evil.example/secret.pdf', BASE + '/../other.pdf']
        await adapter.fetch_items(course)
        assert len(fetcher.calls) == 2
        assert adapter.truncated
        assert all(url.startswith(BASE) for url in fetcher.calls)


@pytest.mark.asyncio
async def test_unconfigured_discovery_stays_inside_course_path(tmp_path):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        adapter = WebsiteAdapter(name='TEST', course_code='TEST', base_url=BASE, fetcher=Fetcher())
        assert adapter._allowed(BASE) and adapter._allowed(BASE + '/schedule.html')
        assert not adapter._allowed('https://example.edu/other/schedule.html')
        assert not adapter._allowed(BASE + '-other/schedule.html')
        assert not adapter._allowed(BASE + '/%2e%2e/secret.pdf')
        rows = await adapter.fetch_items(course)
        assert any(row.is_file for row in rows)


@pytest.mark.parametrize('grade,expected', [
    ({'submission': {'score': 0}}, True),
    ({'submission': {'grade': 'A'}}, True),
    ({'submission': {'score': None, 'grade': None}}, False),
    ({'submission': {'score': 10, 'posted_at': None}}, False),
    ({'submission': {'score': 0, 'posted_at': '2026-10-02'}}, True),
    ({'provider_facts': {'score': 0, 'grade_published': True}}, True),
    ({'provider_facts': {'score': 0, 'grade_published': False}}, False),
    ({'provider_facts': {'score': None, 'max_score': 100}}, False),
])
def test_grade_evidence_zero_hidden_and_unpublished(grade, expected):
    assert grade_is_released(grade) is expected


@pytest.mark.parametrize('provider', ['canvas', 'gradescope', 'prairielearn'])
def test_first_grade_release_wins_over_simultaneous_deadline_and_submission(tmp_path, provider):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        service = SyncService(make_settings(tmp_path))
        raw = RawSourceItem(source_type=provider, source_name=provider + ':1', external_id='grade:1',
            item_type='assignment', title='HW1', structured={'title': 'HW1', 'submission': {'score': None}})
        if provider != 'canvas':
            raw.structured['provider_facts'] = {'provider': provider, 'score': None, 'grade_published': False}
        assert service.store_observation(db, course, raw)[0] is None
        released = deepcopy(raw)
        released.structured['due_at'] = '2026-10-04T12:00:00Z'
        released.structured['submission'] = {'score': 0, 'workflow_state': 'graded'}
        if provider != 'canvas':
            released.structured['provider_facts'].update(score=0, grade_published=True, submission_state='graded')
        event, _ = service.store_observation(db, course, released)
        assert event.change_type == provider.upper() + '_GRADE_RELEASED'
        assert is_user_facing_event(db, event)
        assert service.store_observation(db, course, released)[0] is None
        correction = deepcopy(released)
        correction.structured['submission']['score'] = 1
        if provider != 'canvas':
            correction.structured['provider_facts']['score'] = 1
        assert service.store_observation(db, course, correction)[0] is None


@pytest.mark.parametrize('kind', ['page', 'website_page', 'module', 'syllabus', 'assignment'])
def test_other_changes_do_not_emit_but_snapshots_remain(tmp_path, kind):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        service = SyncService(make_settings(tmp_path))
        raw = RawSourceItem(source_type='canvas', source_name='canvas:1', external_id='noise', item_type=kind,
            title='Before', structured={'title': 'Before'}, normalized_text='Before')
        assert service.store_observation(db, course, raw)[0] is None
        changed = raw.model_copy(update={'title': 'After', 'structured': {'title': 'After'}, 'normalized_text': 'After'})
        assert service.store_observation(db, course, changed)[0] is None
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 2


def test_new_announcement_reports_once_and_edits_do_not(tmp_path):
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        service = SyncService(make_settings(tmp_path))
        raw = RawSourceItem(source_type='canvas', source_name='canvas:1', external_id='announcement:1',
            item_type='announcement', title='Welcome', structured={'title': 'Welcome'}, normalized_text='Welcome')
        event, _ = service.store_observation(db, course, raw)
        assert event.change_type == 'NEW_ANNOUNCEMENT'
        raw.structured['title'] = 'Welcome again'
        assert service.store_observation(db, course, raw)[0] is None


def test_history_and_unread_hide_noise_without_deleting_it():
    with memory_db() as db:
        _, item = add_course(db, 'TEST')
        types = ['NEW_FILE', 'NEW_ANNOUNCEMENT', 'CANVAS_GRADE_RELEASED',
                 'CANVAS_PAGE_UPDATED', 'FILE_CHANGE', 'DEADLINE_CHANGED', 'CANVAS_ASSIGNMENT_CREATED']
        rows = [ChangeEvent(source_item_id=item.id, change_type=kind, importance='important', summary=kind)
                for kind in types]
        db.add_all(rows)
        db.commit()
        shown = changes(limit=100, course_id=None, include_archived=False, db=db)
        assert {row['change_type'] for row in shown} == set(types[:3])
        assert unread_change_count(False, db)['count'] == 3
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == len(types)


@pytest.mark.asyncio
async def test_deadline_tasks_still_sync_without_reports(tmp_path):
    class AssignmentAdapter:
        name = 'canvas:1'
        async def fetch_items(self, course):
            return [RawSourceItem(source_type='canvas', source_name=self.name, external_id='assignment:7',
                item_type='assignment', title='HW7', structured={'title': 'HW7', 'due_at': '2026-10-08T23:59:00Z'})]
    with memory_db() as db:
        course, _ = add_course(db, 'TEST')
        await SyncService(make_settings(tmp_path)).run(db, [(course, AssignmentAdapter())])
        assert db.scalar(select(Task)).due_at is not None
        assert db.scalar(select(func.count()).select_from(ChangeEvent)) == 0
