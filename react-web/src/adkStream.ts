// src/adkStream.ts
export interface StreamCallbacks {
  /** Called with each streaming delta (payload.partial === true). */
  onToken: (chunkText: string) => void;
  /**
   * Called once with the complete text of the turn (payload.partial === false).
   * ADK re-sends the full text after the deltas, so consumers should REPLACE
   * the accumulated reply with this value instead of appending it.
   */
  onFinal?: (fullText: string) => void;
  onToolEvent?: (event: unknown) => void;
  onDone?: () => void;
  onError?: (err: Error) => void;
}

export async function ensureAdkSession(
  baseUrl: string,
  appName: string,
  username: string,
  sessionId: string
): Promise<void> {
  const res = await fetch(
    `${baseUrl}/apps/${appName}/users/${encodeURIComponent(username)}/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ state: {} }),
    }
  );

  if (!res.ok && res.status !== 409) {
    // 409 Conflict = the session already exists, which is exactly what
    // "ensure" asks for, so only a non-409 failure is a real error.
    let detail = "";
    try {
      detail = (await res.text()).slice(0, 300);
    } catch {
      // body unavailable; status alone is still worth reporting
    }
    throw new Error(
      `Failed to ensure ADK session: HTTP ${res.status}${
        detail ? ` — ${detail}` : ""
      }`
    );
  }
}

export async function streamMessageToAgent(params: {
  baseUrl?: string;
  appName?: string;
  username?: string;
  sessionId: string;
  message: string;
  callbacks: StreamCallbacks;
}): Promise<void> {
  const {
    baseUrl = "",
    appName = "my_agent",
    username = "proxy-user",
    sessionId,
    message,
    callbacks,
  } = params;

  try {
    await ensureAdkSession(baseUrl, appName, username, sessionId);

    const response = await fetch(`${baseUrl}/run_sse`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        app_name: appName,
        user_id: username,
        session_id: sessionId,
        streaming: true,
        new_message: {
          role: "user",
          parts: [{ text: message }],
        },
      }),
    });

    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let sawDelta = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;

        const jsonStr = trimmed.slice(5).trim();
        if (!jsonStr || jsonStr === "[DONE]") continue;

        const payload = JSON.parse(jsonStr);
        callbacks.onToolEvent?.(payload);

        const parts = payload?.content?.parts ?? payload?.event?.content?.parts ?? [];
        const texts = parts
          .map((p: { text?: unknown }) => p?.text)
          .filter((t: unknown): t is string => typeof t === "string" && t.length > 0);
        if (texts.length === 0) continue;

        if (payload?.partial === true) {
          // Streaming delta: append it.
          sawDelta = true;
          for (const text of texts) callbacks.onToken(text);
        } else {
          // partial === false (or absent): complete text for this turn.
          const fullText = texts.join("");
          if (callbacks.onFinal) {
            callbacks.onFinal(fullText);
          } else if (!sawDelta) {
            // No deltas arrived (single-shot reply) — append once.
            callbacks.onToken(fullText);
          }
          // else: deltas already rendered this text; appending again would
          // duplicate the reply, so drop the redundant final event.
        }
      }
    }

    callbacks.onDone?.();
  } catch (err) {
    callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
  }
}