import { useEffect, useState } from "react";
import { ActionFeedback, useAsyncAction } from "./ActionFeedback";
import type { Course, FileRecord } from "./types";
import { formatUiucDateTime } from "./timezone";

export const FILE_TYPES = [
  "all",
  "lecture",
  "homework",
  "discussion",
  "reading",
  "exam",
  "solution",
  "other",
] as const;
export type FileType = (typeof FILE_TYPES)[number];
const size = (value: number) =>
  value < 1048576
    ? `${(value / 1024).toFixed(1)} KB`
    : `${(value / 1048576).toFixed(1)} MB`;

type Props = {
  files: FileRecord[];
  onChanged: () => void;
  courses?: Course[];
  courseId?: number | null;
  type?: FileType;
  onFiltersChange?: (courseId: number | null, type: FileType) => void;
};

export function FilesView({
  files,
  onChanged,
  courses,
  courseId = null,
  type,
  onFiltersChange,
}: Props) {
  const [localType, setLocalType] = useState<FileType>("all");
  const [menu, setMenu] = useState<number | null>(null);
  const moveAction = useAsyncAction();
  const selectedType = type ?? localType;
  const visible = onFiltersChange
    ? files
    : selectedType === "all"
      ? files
      : files.filter((file) => file.effective_type === selectedType);
  useEffect(() => {
    if (menu === null) return;
    const close = () => setMenu(null);
    const key = (event: KeyboardEvent) => event.key === "Escape" && close();
    addEventListener("click", close);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("click", close);
      removeEventListener("keydown", key);
    };
  }, [menu]);
  function chooseType(next: FileType) {
    if (onFiltersChange) onFiltersChange(courseId, next);
    else setLocalType(next);
  }
  async function move(file: FileRecord, next: string) {
    const response = await fetch(`/api/files/${file.id}/classification`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: next }),
    });
    if (!response.ok) throw new Error("Unable to move file");
    setMenu(null);
    onChanged();
  }
  return (
    <>
      <ActionFeedback action={moveAction} />
      {courses && onFiltersChange ? (
        <div className="file-course-filter">
          <label>
            Course
            <select
              value={courseId ?? "all"}
              onChange={(event) =>
                onFiltersChange(
                  event.target.value === "all"
                    ? null
                    : Number(event.target.value),
                  selectedType,
                )
              }
            >
              <option value="all">All active courses</option>
              {courses.map((course) => (
                <option value={course.id} key={course.id}>
                  {course.display_course_code ?? course.course_code}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      <div className="type-index" role="tablist" aria-label="File types">
        {FILE_TYPES.map((value) => (
          <button
            key={value}
            role="tab"
            aria-selected={selectedType === value}
            className={selectedType === value ? "active" : ""}
            onClick={() => chooseType(value)}
          >
            {value[0].toUpperCase() + value.slice(1)}
          </button>
        ))}
      </div>
      <div className="file-grid">
        {visible.map((file) => (
          <article
            key={file.id}
            className={`file-card ${file.source_url ? "clickable" : ""}`}
          >
            {file.source_url ? (
              <a
                className="file-copy"
                href={file.source_url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`Open source for ${file.original_filename}`}
              >
                <FileCopy file={file} />
              </a>
            ) : (
              <div className="file-copy">
                <FileCopy file={file} />
              </div>
            )}
            <button
              className="icon-button"
              aria-label={`File actions for ${file.original_filename}`}
              aria-haspopup="menu"
              aria-expanded={menu === file.id}
              onClick={(event) => {
                event.stopPropagation();
                setMenu(menu === file.id ? null : file.id);
              }}
            >
              ⋯
            </button>
            {menu === file.id ? (
              <div
                className="overflow-menu"
                role="menu"
                onClick={(event) => event.stopPropagation()}
              >
                <a role="menuitem" href={`/api/files/${file.id}/content`}>
                  Download
                </a>
                <span>Move to…</span>
                {FILE_TYPES.filter((value) => value !== "all").map((value) => (
                  <button
                    role="menuitem"
                    key={value}
                    disabled={moveAction.busy}
                    onClick={() => { void moveAction.run("Move file", () => move(file, value)); }}
                  >
                    {value[0].toUpperCase() + value.slice(1)}
                  </button>
                ))}
              </div>
            ) : null}
          </article>
        ))}
      </div>
      {!visible.length ? (
        <div className="empty">No files match these filters.</div>
      ) : null}
    </>
  );
}

function FileCopy({ file }: { file: FileRecord }) {
  return (
    <>
      <strong>{file.original_filename}</strong>
      <p>
        {file.course_code} · {file.effective_type}
      </p>
      <small>
        Updated {formatUiucDateTime(file.downloaded_at)} ·{" "}
        {size(file.size_bytes)}
      </small>
    </>
  );
}
