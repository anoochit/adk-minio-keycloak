// src/adkStream.ts
export interface StreamCallbacks {
  onToken: (chunkText: string) => void;
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

  if (!res.ok) {
    throw new Error(`Failed to ensure ADK session: HTTP ${res.status}`);
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
        for (const part of parts) {
          if (typeof part?.text === "string" && part.text.length > 0) {
            callbacks.onToken(part.text);
          }
        }
      }
    }

    callbacks.onDone?.();
  } catch (err) {
    callbacks.onError?.(err instanceof Error ? err : new Error(String(err)));
  }
}