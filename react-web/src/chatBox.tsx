// src/chatBox.tsx
import { useState, useRef, useEffect } from "react";
import { streamMessageToAgent } from "./adkStream";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
}

const SUGGESTIONS = [
  "📁 ดูรายชื่อไฟล์ของฉัน",
  "✍️ สร้างไฟล์ hello.txt เนื้อหา สวัสดีจาก ADK",
  "📖 อ่านไฟล์ hello.txt",
  "🔗 ขอลิงก์ดาวน์โหลด hello.txt",
];

export function ChatBox({
  username,
  sessionId,
}: {
  username?: string;
  sessionId: string;
}) {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  const handleSend = async (textToSend?: string) => {
    const prompt = (textToSend || input).trim();
    if (!prompt || loading) return;

    setInput("");
    setLoading(true);

    const userMsgId = `user-${Date.now()}`;
    const assistantMsgId = `assistant-${Date.now()}`;

    setMessages((prev) => [
      ...prev,
      { id: userMsgId, role: "user", text: prompt },
      { id: assistantMsgId, role: "assistant", text: "" },
    ]);

    await streamMessageToAgent({
      username,
      sessionId,
      message: prompt,
      callbacks: {
        onToken: (chunk) => {
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMsgId
                ? { ...msg, text: msg.text + chunk }
                : msg
            )
          );
        },
        onFinal: (fullText) => {
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMsgId ? { ...msg, text: fullText } : msg
            )
          );
        },
        onDone: () => setLoading(false),
        onError: (e) => {
          const isExpired =
            e.message.includes("expired") ||
            e.message.includes("401") ||
            e.message.includes("Token has expired");
          const errorMsg = isExpired
            ? "โทเค็นการยืนยันตัวตนหมดอายุแล้ว กรุณารีเฟรชหน้าเว็บ (F5) เพื่อเข้าสู่ระบบใหม่ผ่าน Keycloak"
            : `เกิดข้อผิดพลาด: ${e.message}`;

          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMsgId
                ? { ...msg, text: errorMsg }
                : msg
            )
          );
          setLoading(false);
        },
      },
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="chat-card">
      <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="empty-chat">
            <div className="empty-chat-icon">💬</div>
            <div className="empty-chat-title">ยินดีต้อนรับสู่ ADK File Assistant</div>
            <div className="empty-chat-sub">
              เริ่มพิมพ์ข้อความหรือเลือกคำสั่งแนะนำด้านล่าง เพื่อจัดการไฟล์ส่วนตัวของคุณบน MinIO
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`message-row ${msg.role}`}>
              <div className={`avatar ${msg.role}`}>
                {msg.role === "user" ? "คุณ" : "AI"}
              </div>
              <div className="message-content">
                {msg.text || (loading && msg.role === "assistant" ? (
                  <div className="typing-indicator">
                    <span className="typing-dot"></span>
                    <span className="typing-dot"></span>
                    <span className="typing-dot"></span>
                  </div>
                ) : null)}
              </div>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="suggestions-container">
        {SUGGESTIONS.map((sug, idx) => (
          <button
            key={idx}
            className="suggestion-chip"
            onClick={() => handleSend(sug)}
            disabled={loading}
          >
            {sug}
          </button>
        ))}
      </div>

      <div className="chat-input-box">
        <div className="input-wrapper">
          <textarea
            className="chat-textarea"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="สั่งงาน Agent เช่น ดูรายชื่อไฟล์ของฉัน... (Press Enter to send)"
            rows={1}
          />
          <button
            className="send-button"
            onClick={() => handleSend()}
            disabled={loading || !input.trim()}
          >
            {loading ? "กำลังตอบ..." : "ส่ง"}
          </button>
        </div>
      </div>
    </div>
  );
}