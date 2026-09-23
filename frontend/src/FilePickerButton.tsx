import { useRef } from "react";

export function FilePickerButton({ disabled, label, onFile }: {
  disabled: boolean; label: string; onFile: (file: File) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  return <span className="file-picker-action">
    <button type="button" className="secondary reimport-button" disabled={disabled}
      aria-label={label} onClick={() => input.current?.click()}>Re-import</button>
    <input ref={input} hidden type="file" accept=".ics,text/calendar" disabled={disabled}
      onChange={event => {
        const file = event.currentTarget.files?.[0];
        event.currentTarget.value = "";
        if (file && !disabled) onFile(file);
      }} />
  </span>;
}
