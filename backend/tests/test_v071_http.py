from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.change_filters import router as filters_router
from app.api.routes import router
from app.db import (
    Base,
    ChangeEvent,
    Course,
    SourceItem,
    SourceSnapshot,
    Task,
    TaskSourceLink,
    get_db,
)


@pytest.mark.asyncio
async def test_task_today_and_detail_share_exact_navigation_contract():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source='canvas', external_id='http-test', course_code='TEST', name='Test', lifecycle_state='ACTIVE')
        db.add(course)
        db.flush()
        task = Task(course_id=course.id, title='HTTP navigation fixture', due_at=datetime.now(UTC)+timedelta(hours=2))
        item = SourceItem(course_id=course.id, source_type='canvas', source_name='test', external_id='assignment:1',
            item_type='assignment', title=task.title, current_hash='fixture', url='https://canvas.example/courses/1/assignments/2')
        db.add_all([task,item])
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id))
        db.add(SourceSnapshot(source_item_id=item.id, content_hash='fixture', structured_json={'html_url':item.url}))
        db.commit()
        app=FastAPI()
        app.include_router(router)
        app.include_router(filters_router)
        app.dependency_overrides[get_db]=lambda: db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            listing = await client.get('/api/tasks')
            detail = await client.get(f'/api/tasks/{task.id}')
            today = await client.get('/api/today')
            assert listing.status_code == detail.status_code == today.status_code == 200
            for row in [listing.json()[0], detail.json()['task'], today.json()['work'][0]]:
                assert row['source_url'] == item.url
                assert row['source_link_kind'] == 'assignment'
            diagnostics = await client.get('/api/settings/change-filters')
            assert diagnostics.status_code == 200 and diagnostics.json()['rules'] == []
            assert (await client.patch('/api/settings/change-filters/999', json={'enabled':'yes'})).status_code == 422
            assert (await client.patch('/api/settings/change-filters/999', json={'enabled':False})).status_code == 404


@pytest.mark.asyncio
async def test_changes_prefilter_preserves_legacy_without_snapshots():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source='canvas', external_id='changes-test', course_code='TEST', name='Test',
            lifecycle_state='ACTIVE')
        db.add(course)
        db.flush()
        item = SourceItem(course_id=course.id, source_type='canvas', source_name='test',
            external_id='file:1', item_type='file', title='Lecture.pdf', current_hash='fixture')
        db.add(item)
        db.flush()
        snapshot = SourceSnapshot(source_item_id=item.id, content_hash='fixture',
            structured_json={'title': 'Lecture.pdf'}, normalized_text='Lecture.pdf')
        db.add(snapshot)
        db.flush()
        visible = ChangeEvent(source_item_id=item.id, change_type='CANVAS_FILE_UPDATED',
            new_snapshot_id=snapshot.id, summary='Lecture changed')
        hidden = ChangeEvent(source_item_id=item.id, change_type='CANVAS_FILE_METADATA_UPDATED',
            new_snapshot_id=snapshot.id, summary='Metadata churn')
        legacy = ChangeEvent(source_item_id=item.id, change_type='CANVAS_FILE_METADATA_UPDATED',
            summary='Legacy metadata event')
        db.add_all([visible, hidden, legacy])
        db.commit()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            listing = await client.get('/api/changes')
            assert listing.status_code == 200
            assert {row['id'] for row in listing.json()} == {visible.id, legacy.id}
            unread = await client.get('/api/changes/unread-count')
            assert unread.status_code == 200 and unread.json() == {'count': 2}
            marked = await client.post('/api/changes/read-all')
            assert marked.status_code == 200 and marked.json() == {'updated': 2}
            assert (await client.get('/api/changes/unread-count')).json() == {'count': 0}
        assert db.get(ChangeEvent, hidden.id).read_at is None
        assert db.get(ChangeEvent, visible.id).read_at is not None
        assert db.get(ChangeEvent, legacy.id).read_at is not None
