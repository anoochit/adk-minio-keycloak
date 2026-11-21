// src/App.tsx
import { useMemo } from "react";
import { ChatBox } from "./chatBox";
import "./App.css";

function getOrCreateSessionId(): string {
  const storageKey = "adk_session_id";
  const existing = sessionStorage.getItem(storageKey);
  if (existing) return existing;

  const created =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `sess-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  sessionStorage.setItem(storageKey, created);
  return created;
}

export function App() {
  const sessionId = useMemo(() => getOrCreateSessionId(), []);

  return (
    <main style={{ maxWidth: 800, margin: "2rem auto", padding: "0 1rem" }}>
      <h1>ผู้ช่วยจัดการไฟล์ส่วนตัว (MinIO + ADK)</h1>
      <p>
        ยืนยันตัวตนผ่าน OAuth2 Proxy &amp; Keycloak แล้ว — ระบบจะจำกัดสิทธิ์เฉพาะโฟลเดอร์ส่วนตัวของคุณอัตโนมัติ
      </p>
      <ChatBox
        username="proxy-user"
        sessionId={sessionId}
      />
    </main>
  );
}

export default App;