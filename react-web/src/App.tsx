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
    <div className="app-viewport">
      <header className="app-header">
        <div className="header-top">
          <div className="header-title-group">
            <div className="header-icon">📂</div>
            <h1 className="app-title">ผู้ช่วยจัดการไฟล์ส่วนตัว (MinIO + ADK)</h1>
          </div>
          <div className="badges-group">
            <span className="status-badge active">
              <span className="status-dot"></span>
              Keycloak Authenticated
            </span>
            <span className="status-badge">
              🔒 MinIO STS Scoped
            </span>
          </div>
        </div>
        <p className="header-description">
          ยืนยันตัวตนผ่าน OAuth2 Proxy &amp; Keycloak แล้ว — ระบบจำกัดสิทธิ์เฉพาะพื้นที่จัดเก็บไฟล์ส่วนตัวของคุณโดยอัตโนมัติ
        </p>
      </header>

      <ChatBox username="proxy-user" sessionId={sessionId} />

      <footer className="app-footer">
        ADK Agent powered by Gemini 2.5 Flash &bull; Secured with Keycloak OIDC &amp; MinIO STS
      </footer>
    </div>
  );
}

export default App;