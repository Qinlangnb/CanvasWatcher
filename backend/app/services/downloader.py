import hashlib
import io
import mimetypes
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Course, DownloadedFile, SourceItem, utcnow
from app.services.file_types import normalize_file_type

DEFAULT_RULES = [
    (r"(?i)(?<![a-z0-9])(?:solutions?|solns?|sol(?=[\W_]*\d)|answer[\W_]*keys?|answers?)(?![a-z])|参考答案|解答|题解", "solution"),
    (r"(?i)(?<![a-z0-9])(?:exams?|midterms?|final[\W_]*exam|quizzes|quiz)(?![a-z])|期中|期末|试卷", "exam"),
    (r"(?i)(?<![a-z0-9])(?:discussions?|disc|dis(?=[\W_]*\d)|recitations?|tutorials?)(?![a-z])|讨论课|习题课", "discussion"),
    (r"(?i)(?<![a-z0-9])(?:homework|hw|problem[\W_]*sets?|pset|assignments?)(?![a-z])|作业", "assignments"),
    (r"(?i)(?<![a-z0-9])(?:lectures?|lec|slides?|class[\W_]*notes?)(?![a-z])|讲义|课件", "lecture_notes"),
    (r"(?i)(?<![a-z0-9])(?:readings?|optional|supplement(?:al|ary)?|syllabus|handbooks?|papers?)(?![a-z])|阅读|教学大纲", "optional_reading"),
]
OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
ZIP_EXTENSIONS = OFFICE_EXTENSIONS | {".zip"}
OLE_EXTENSIONS = {".doc", ".ppt", ".xls"}
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
}


@dataclass(frozen=True)
class IntegrityResult:
    valid: bool
    status: str
    detected_extension: str | None = None
    reason: str | None = None


@dataclass
class DownloadResult:
    record: DownloadedFile | None
    changed: bool


_BUILTIN_RULES = tuple((re.compile(pattern), category) for pattern, category in DEFAULT_RULES)


@lru_cache(maxsize=16)
def _cached_rules(path: str, mtime_ns: int, size: int):
    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("rules", []), list):
            return _BUILTIN_RULES
        custom = []
        for row in loaded.get("rules", []):
            pattern, category = row["match"]["regex"], row["category"]
            if not isinstance(pattern, str) or not isinstance(category, str):
                return _BUILTIN_RULES
            custom.append((re.compile(pattern), category))
        return tuple(custom) + _BUILTIN_RULES
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, re.error):
        return _BUILTIN_RULES


def classify_file(name_or_path: str, rules_path: Path | None = None) -> str:
    rules = _BUILTIN_RULES
    if rules_path:
        try:
            stat = rules_path.stat()
            rules = _cached_rules(str(rules_path.absolute()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    # Ignore URL query/fragment values: signed URLs and IDs are not file names.
    context = re.sub(r"https?://[^\s]+", lambda match: urlsplit(match.group()).path, name_or_path)
    context = unquote(context)
    for pattern, category in rules:
        if pattern.search(context):
            return category
    return "other"


def effective_file_type(record: DownloadedFile, rules_path: Path | None = None) -> str:
    if record.user_override_type is not None:
        return normalize_file_type(record.user_override_type)
    detected = normalize_file_type(record.source_detected_type or record.category)
    if detected != "other":
        return detected
    return normalize_file_type(classify_file(f"{record.source_url or ''} {record.original_filename or ''}", rules_path))


def _url_extension(url: str) -> str:
    return Path(unquote(urlsplit(url).path)).suffix.lower()


def safe_filename(url: str, suggested: str | None = None, mime_type: str | None = None) -> str:
    source_name = Path(unquote(urlsplit(url).path)).name
    raw = suggested or source_name or "download"
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")
    extension = _url_extension(url) or MIME_EXTENSIONS.get((mime_type or "").lower())
    if extension and not Path(value).suffix:
        value += extension
    return value[:240] or f"download{extension or ''}"


def versioned_path(directory: Path, filename: str, version: int) -> Path:
    candidate = directory / filename
    if version <= 1 and not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    candidate = directory / f"{stem}__{timestamp}{suffix}"
    serial = 2
    while candidate.exists():
        candidate = directory / f"{stem}__{timestamp}_{serial}{suffix}"
        serial += 1
    return candidate


def _looks_like_html(content: bytes) -> bool:
    prefix = content[:1024].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def validate_course_file(
    content: bytes,
    *,
    url: str,
    filename: str | None = None,
    mime_type: str | None = None,
    expected_length: int | None = None,
) -> IntegrityResult:
    if not content:
        return IntegrityResult(False, "INVALID_DOWNLOAD", reason="empty_response")
    if expected_length is not None and expected_length != len(content):
        return IntegrityResult(
            False,
            "INVALID_DOWNLOAD",
            reason=f"content_length_mismatch:{expected_length}!={len(content)}",
        )
    extension = _url_extension(url) or Path(filename or "").suffix.lower()
    detected: str | None = None
    if content.startswith(b"%PDF-"):
        detected = ".pdf"
    elif content.startswith(b"PK"):
        detected = ".zip"
    elif content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        detected = ".ole"
    elif _looks_like_html(content):
        detected = ".html"

    expected_binary = extension in {".pdf"} | ZIP_EXTENSIONS | OLE_EXTENSIONS
    if detected == ".html" and expected_binary:
        return IntegrityResult(
            False,
            "INVALID_DOWNLOAD",
            detected_extension=detected,
            reason="html_response_disguised_as_binary",
        )
    if extension == ".pdf" and detected != ".pdf":
        return IntegrityResult(
            False,
            "INVALID_DOWNLOAD",
            detected_extension=detected,
            reason="invalid_pdf_signature",
        )
    if extension in ZIP_EXTENSIONS:
        if not zipfile.is_zipfile(io.BytesIO(content)):
            return IntegrityResult(
                False,
                "INVALID_DOWNLOAD",
                detected_extension=detected,
                reason="invalid_zip_container",
            )
        detected = extension
    if extension in OLE_EXTENSIONS and detected != ".ole":
        return IntegrityResult(
            False,
            "INVALID_DOWNLOAD",
            detected_extension=detected,
            reason="invalid_ole_signature",
        )
    if mime_type and mime_type.lower() == "text/html" and expected_binary:
        return IntegrityResult(
            False,
            "INVALID_DOWNLOAD",
            detected_extension=detected,
            reason="html_content_type_for_binary",
        )
    return IntegrityResult(True, "VALID", detected_extension=detected or extension or None)


def _normalized_component(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return re.sub(r"_+", "_", value)


def phys225_export_filename(title: str, url: str, extension: str) -> str:
    path_name = Path(unquote(urlsplit(url).path)).stem
    lowered = path_name.lower()
    match = re.search(r"lecture[-_ ]?0*(\d+)", lowered)
    if match:
        base = f"Lecture_{int(match.group(1)):02d}"
    else:
        match = re.search(r"hw[-_ ]?0*(\d+)", lowered)
        if match:
            base = f"HW_{int(match.group(1)):02d}"
            if "solution" in lowered:
                base += "_Solutions"
        else:
            match = re.search(r"discussion[-_ ]?0*(\d+)", lowered)
            if match:
                base = f"Discussion_{int(match.group(1)):02d}"
                if "solution" in lowered:
                    base += "_Solutions"
            elif "midterm" in lowered and "formula" in lowered:
                base = "Midterm_Formula_Sheet"
            elif "midterm" in lowered and "practice" in lowered:
                base = "Midterm_Practice" + ("_Solutions" if "solution" in lowered else "")
            elif "final" in lowered and "formula" in lowered:
                base = "Final_Formula_Sheet"
            elif "final" in lowered and "practice" in lowered:
                base = "Final_Practice" + ("_Solutions" if "solution" in lowered else "")
            else:
                base = _normalized_component(title or path_name) or "Course_Material"
    return f"PHYS225_{base}{extension.lower()}"


class FileDownloader:
    def __init__(
        self,
        root: Path,
        rules_path: Path | None = None,
        exports: dict[str, dict[str, Any]] | None = None,
    ):
        self.root = root
        self.rules_path = rules_path
        self.exports = exports or {}

    async def download(
        self,
        db: Session,
        *,
        course_code: str,
        course_id: int,
        source_item: SourceItem,
        url: str,
        filename: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DownloadResult:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        length = response.headers.get("content-length")
        return self.save_bytes(
            db,
            course_code=course_code,
            course_id=course_id,
            source_item=source_item,
            url=url,
            content=response.content,
            filename=filename,
            mime_type=response.headers.get("content-type", "").split(";")[0] or None,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            http_status=response.status_code,
            expected_length=int(length) if length and length.isdigit() else None,
        )

    @staticmethod
    def _write_part(path: Path, content: bytes) -> None:
        with path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def _atomic_write(self, target: Path, content: bytes, validation_kwargs: dict[str, Any]) -> None:
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)
        try:
            self._write_part(part, content)
            actual = part.read_bytes()
            result = validate_course_file(actual, **validation_kwargs)
            if not result.valid:
                raise ValueError(result.reason or "post_write_validation_failed")
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)

    def save_bytes(
        self,
        db: Session,
        *,
        course_code: str,
        course_id: int,
        source_item: SourceItem,
        url: str,
        content: bytes,
        filename: str | None = None,
        classification_context: str | None = None,
        mime_type: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        http_status: int | None = 200,
        expected_length: int | None = None,
    ) -> DownloadResult:
        name = safe_filename(url, filename, mime_type)
        digest = hashlib.sha256(content).hexdigest()
        latest = db.scalar(
            select(DownloadedFile)
            .where(DownloadedFile.source_item_id == source_item.id)
            .order_by(DownloadedFile.version_number.desc())
            .limit(1)
        )
        if latest and latest.sha256 == digest:
            if latest.integrity_status == "VALID" and latest.local_path and Path(latest.local_path).exists():
                self._export(db, course_code, source_item, latest)
            return DownloadResult(latest, False)
        version = int(
            db.scalar(
                select(func.max(DownloadedFile.version_number)).where(
                    DownloadedFile.source_item_id == source_item.id
                )
            )
            or 0
        ) + 1
        category = classify_file(f"{url} {name}", self.rules_path)
        if category == "other" and classification_context:
            category = classify_file(classification_context, self.rules_path)
        detected_type = normalize_file_type(category)
        override_type = latest.user_override_type if latest else None
        effective_type = override_type or detected_type
        validation = validate_course_file(
            content,
            url=url,
            filename=name,
            mime_type=mime_type,
            expected_length=expected_length,
        )
        if http_status is not None and not 200 <= http_status < 300:
            validation = IntegrityResult(False, "INVALID_DOWNLOAD", reason=f"http_status_{http_status}")
        if not validation.valid:
            record = DownloadedFile(
                course_id=course_id,
                source_item_id=source_item.id,
                source_url=url,
                original_filename=name,
                local_path="",
                category=effective_type,
                source_detected_type=detected_type,
                user_override_type=override_type,
                mime_type=mime_type or mimetypes.guess_type(name)[0],
                size_bytes=len(content),
                sha256=digest,
                etag=etag,
                last_modified=last_modified,
                source_updated_at=source_item.source_updated_at,
                version_number=version,
                integrity_status="INVALID_DOWNLOAD",
                validation_error=validation.reason,
                export_status="BLOCKED_INVALID",
            )
            db.add(record)
            db.flush()
            return DownloadResult(record, True)

        directory = self.root / re.sub(r"[^A-Za-z0-9._-]", "_", course_code) / category
        directory.mkdir(parents=True, exist_ok=True)
        path = versioned_path(directory, name, version)
        self._atomic_write(
            path,
            content,
            {
                "url": url,
                "filename": name,
                "mime_type": mime_type,
                "expected_length": len(content),
            },
        )
        actual = path.read_bytes()
        record = DownloadedFile(
            course_id=course_id,
            source_item_id=source_item.id,
            source_url=url,
            original_filename=name,
            local_path=str(path),
            category=effective_type,
            source_detected_type=detected_type,
            user_override_type=override_type,
            mime_type=mime_type or mimetypes.guess_type(name)[0],
            size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(actual).hexdigest(),
            etag=etag,
            last_modified=last_modified,
            source_updated_at=source_item.source_updated_at,
            version_number=version,
            integrity_status="VALID",
        )
        db.add(record)
        db.flush()
        self._export(db, course_code, source_item, record, validation.detected_extension)
        return DownloadResult(record, True)

    def _export(
        self,
        db: Session,
        course_code: str,
        source_item: SourceItem,
        record: DownloadedFile,
        detected_extension: str | None = None,
    ) -> None:
        config = self.exports.get(course_code)
        if not config or not config.get("enabled"):
            record.export_status = "NOT_CONFIGURED"
            return
        export_root = Path(str(config.get("path", "")))
        if not export_root.is_dir():
            record.export_status = "EXPORT_UNAVAILABLE"
            record.export_error = f"export_directory_missing:{export_root}"
            return
        source_path = Path(record.local_path)
        if record.integrity_status != "VALID" or not source_path.is_file():
            record.export_status = "BLOCKED_INVALID"
            return
        extension = _url_extension(record.source_url) or detected_extension or source_path.suffix
        export_name = (
            phys225_export_filename(source_item.title, record.source_url, extension)
            if course_code.upper() == "PHYS225"
            else f"{_normalized_component(course_code)}_{_normalized_component(source_item.title)}{extension}"
        )
        target = export_root / export_name
        managed = db.scalar(
            select(DownloadedFile)
            .where(
                DownloadedFile.source_item_id == source_item.id,
                DownloadedFile.export_path == str(target),
            )
            .limit(1)
        )
        if target.exists() and managed is None:
            existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_digest != record.sha256:
                suffix = hashlib.sha256(record.source_url.encode()).hexdigest()[:8]
                target = export_root / f"{Path(export_name).stem}_{suffix}{Path(export_name).suffix}"
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == record.sha256:
            record.export_name = target.name
            record.export_path = str(target)
            record.export_status = "CURRENT"
            record.export_error = None
            record.exported_at = utcnow()
            return
        self._atomic_write(
            target,
            source_path.read_bytes(),
            {
                "url": record.source_url,
                "filename": target.name,
                "mime_type": record.mime_type,
                "expected_length": record.size_bytes,
            },
        )
        record.export_name = target.name
        record.export_path = str(target)
        record.export_status = "CURRENT"
        record.export_error = None
        record.exported_at = utcnow()

    def audit_existing(self, db: Session) -> dict[str, int]:
        summary = {"valid": 0, "invalid": 0, "missing": 0, "renamed": 0, "exported": 0}
        records = list(db.scalars(select(DownloadedFile).order_by(DownloadedFile.id)))
        latest_versions = {
            source_item_id: version
            for source_item_id, version in db.execute(
                select(DownloadedFile.source_item_id, func.max(DownloadedFile.version_number)).group_by(
                    DownloadedFile.source_item_id
                )
            )
        }
        for record in records:
            path = Path(record.local_path) if record.local_path else None
            if not path or not path.is_file():
                if record.integrity_status != "INVALID_DOWNLOAD":
                    record.integrity_status = "MISSING"
                    record.validation_error = "managed_file_missing"
                    summary["missing"] += 1
                else:
                    summary["invalid"] += 1
                continue
            content = path.read_bytes()
            validation = validate_course_file(
                content,
                url=record.source_url,
                filename=record.original_filename,
                mime_type=record.mime_type,
                expected_length=record.size_bytes,
            )
            if not validation.valid:
                record.integrity_status = "INVALID_DOWNLOAD"
                record.validation_error = validation.reason
                record.export_status = "BLOCKED_INVALID"
                summary["invalid"] += 1
                continue
            corrected_name = safe_filename(record.source_url, record.original_filename, record.mime_type)
            if corrected_name != record.original_filename:
                corrected_path = path.with_name(corrected_name)
                if corrected_path.exists() and corrected_path != path:
                    corrected_path = path.with_name(
                        f"{Path(corrected_name).stem}_{record.sha256[:8]}{Path(corrected_name).suffix}"
                    )
                os.replace(path, corrected_path)
                path = corrected_path
                record.original_filename = corrected_name
                record.local_path = str(path)
                summary["renamed"] += 1
            record.size_bytes = path.stat().st_size
            record.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            record.integrity_status = "VALID"
            record.validation_error = None
            summary["valid"] += 1
            if latest_versions.get(record.source_item_id) == record.version_number:
                source_item = db.get(SourceItem, record.source_item_id)
                course = db.get(Course, record.course_id)
                if source_item and course:
                    self._export(
                        db,
                        course.course_code,
                        source_item,
                        record,
                        validation.detected_extension,
                    )
                    summary["exported"] += int(record.export_status == "CURRENT")
        db.commit()
        return summary
