import { z } from "zod";

const confirmation = z.object({
  id: z.string(), tool_name: z.string(), summary: z.string(),
  arguments: z.record(z.unknown()), status: z.string(), result: z.unknown().optional(),
});
const response = z.object({
  conversation_id: z.string(), message: z.string().nullable(),
  tool_results: z.array(z.object({ tool_name: z.string(), permission: z.string().optional(), result: z.unknown() })),
  confirmation: confirmation.nullable(),
  error: z.object({ code: z.string(), message: z.string() }).nullable(),
});

export function parseChatResponse(value: unknown) {
  const parsed = response.safeParse(value);
  if (!parsed.success) throw new Error("AI Chat returned an invalid response. Please refresh the page and try again.");
  return parsed.data;
}

export function chatErrorMessage(error: unknown): string {
  if (error instanceof TypeError) return "Could not connect to AI Chat. Check the local service and try again.";
  return error instanceof Error ? error.message : "AI Chat is temporarily unavailable.";
}
