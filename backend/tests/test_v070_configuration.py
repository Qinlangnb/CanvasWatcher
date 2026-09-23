from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, Course, CourseSource, CredentialProfile, SourceConnection
from app.services.auth import AuthService
from app.services.canvas_configuration import canvas_settings
from app.services.source_connections import ensure_existing_connections


def test_legacy_origin_recovery_does_not_retarget_unbound_environment_secret(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(canvas_base_url="", canvas_access_token=SecretStr("unbound-fixture"),
                        courses_config=tmp_path / "absent.yaml")
    with Session(engine) as db:
        profile = CredentialProfile(credential_id="old-id", auth_type="canvas_token",
            probe_url="https://school.example/api/v1/users/self/profile", metadata_json={}, state="ACTIVE")
        course = Course(source="canvas", external_id="123", course_code="TEST", name="Course")
        db.add_all([profile, course])
        db.flush()
        db.add(CourseSource(course_id=course.id, name="canvas:123", source_type="canvas",
            external_id="123", url="https://school.example/courses/123"))
        db.commit()
        effective = canvas_settings(db, settings)
        assert effective.canvas_base_url == "https://school.example"
        assert effective.canvas_credential_id == "old-id"
        assert not effective.legacy_canvas_token
        assert ensure_existing_connections(db, settings) == 1
        connection = db.scalar(select(SourceConnection))
        assert connection.base_url == "https://school.example"
        assert connection.credential_id == "old-id"
        assert ensure_existing_connections(db, settings) == 0
        AuthService(settings).ensure_profiles(db)
        assert profile.state == "AUTH_REQUIRED"


def test_unconfigured_installation_does_not_invent_an_institution(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(canvas_base_url="", courses_config=tmp_path / "absent.yaml")
        assert ensure_existing_connections(db, settings) == 0
        assert AuthService(settings).canvas_status(db) == {"configured": False, "credential_state": "NOT_CONFIGURED"}
