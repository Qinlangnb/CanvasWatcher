import { useEffect, useRef } from "react";

/** Opening owns initial focus; polling renders must never steal it back. */
export function useDrawerFocus(open: boolean, close: () => void, target: { current: HTMLElement | null }) {
  const latestClose = useRef(close);
  useEffect(() => { latestClose.current = close; }, [close]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement;
    target.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) latestClose.current();
    };
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("keydown", key);
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, [open, target]);
}
