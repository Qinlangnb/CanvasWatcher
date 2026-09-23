import { api } from "./api";

export async function setTaskIgnored(taskId: number, ignored: boolean) {
  return api(`/api/tasks/${taskId}/${ignored ? "ignore" : "unignore"}`, {
    method: "POST",
  });
}
