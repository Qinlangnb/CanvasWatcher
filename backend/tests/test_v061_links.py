import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base, Course, SourceItem, SourceSnapshot, Task, TaskSourceLink
from app.services.task_links import safe_task_url, task_source_link


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'file:///C:/secret', 'https://user:secret@example.org/task',
    'https://example.org/api/v1/assignments/1',
    'https://example.org/tasks/1?access_token=secret',
    'https://example.org/files/1/download', 'https://example.org/tasks/1#token=secret',
    'https://example.org:bad/tasks/1', 'https://example.org/\nsecret',
])
def test_unsafe_or_temporary_navigation_is_rejected(url):
    assert safe_task_url(url) is None


def test_stored_assignment_html_url_wins_and_missing_link_is_honest():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source='canvas', external_id='fixture-course', course_code='TEST', name='Test')
        db.add(course)
        db.flush()
        task = Task(course_id=course.id, title='Test assignment')
        item = SourceItem(course_id=course.id, source_type='canvas', source_name='fixture', external_id='assignment:1', item_type='assignment', title=task.title, url='https://example.org/api/v1/assignments/1', current_hash='fixture')
        db.add_all([task, item])
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id, relationship_type='primary'))
        db.add(SourceSnapshot(source_item_id=item.id, content_hash='fixture', structured_json={'html_url': 'https://example.org/courses/2/assignments/1'}))
        db.flush()
        assert task_source_link(db, task.id) == {'source_url': 'https://example.org/courses/2/assignments/1', 'source_link_kind': 'assignment'}
        assert task_source_link(db, task.id + 1) == {'source_url': None, 'source_link_kind': 'unavailable'}


@pytest.mark.parametrize('resource', [None, 'https://example.org/hw.pdf?token=secret'])
def test_schedule_without_safe_assignment_pdf_is_labelled_as_fallback(resource):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source='website', external_id='site', course_code='TEST', name='Test')
        db.add(course)
        db.flush()
        task = Task(course_id=course.id, title='HW 1')
        url = 'https://example.org/schedule.html'
        item = SourceItem(course_id=course.id, source_type='website', source_name='site',
                          external_id=url + '#homework-1', item_type='assignment', title='HW 1',
                          url=url, current_hash='fixture')
        db.add_all([task, item])
        db.flush()
        db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id, relationship_type='primary'))
        db.add(SourceSnapshot(source_item_id=item.id, content_hash='fixture',
                              structured_json={'schedule_url': url, 'assignment_resource_url': resource}))
        db.flush()
        assert task_source_link(db, task.id) == {'source_url': url, 'source_link_kind': 'course_page'}


@pytest.mark.parametrize('provider,url', [
    ('gradescope', 'https://www.gradescope.com/courses/2/assignments/4'),
    ('prairielearn', 'https://us.prairielearn.com/pl/course_instance/2/assessment/4'),
])
def test_bound_execution_platform_wins_over_canvas_and_deleted_is_ignored(provider, url):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        course = Course(source='canvas', external_id='course', course_code='TEST', name='Test')
        db.add(course)
        db.flush()
        task = Task(course_id=course.id, title='Homework')
        db.add(task)
        db.flush()
        items = []
        for kind, target in [('canvas', 'https://canvas.example/courses/2/assignments/1'), (provider, url)]:
            item = SourceItem(course_id=course.id, source_type=kind, source_name=kind, external_id=kind,
                              item_type='assignment', title='Homework', url=target, current_hash=kind)
            db.add(item)
            db.flush()
            db.add(TaskSourceLink(task_id=task.id, source_item_id=item.id, relationship_type='primary'))
            db.add(SourceSnapshot(source_item_id=item.id, content_hash=kind,
                                  structured_json={'provider_facts': {'direct_assignment_url': target}}))
            items.append(item)
        db.flush()
        assert task_source_link(db, task.id)['source_url'] == url
        items[1].is_deleted = True
        db.flush()
        assert task_source_link(db, task.id)['source_url'] == items[0].url
