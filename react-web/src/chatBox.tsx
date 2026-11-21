// src/chatBox.tsx
import { useState, useRef, useEffect } from "react";
import { streamMessageToAgent } from "./adkStream";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
}

const SUGGESTIONS = [
  "📁 List my files",
  "✍️ Create hello.txt with content Hello from ADK",
  "📖 Read hello.txt",
  "🗑️ Delete hello.txt",
  "🔗 Get download link for hello.txt",
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
            ? "Session token has expired. Please refresh the page (F5) to log in again via Keycloak."
            : `An error occurred: ${e.message}`;

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
            <div className="empty-chat-title">Welcome to ADK File Assistant</div>
            <div className="empty-chat-sub">
              Type a message or select a suggestion below to manage your personal files on MinIO.
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`message-row ${msg.role}`}>
              <div className={`avatar ${msg.role}`}>
                {msg.role === "user" ? "You" : "AI"}
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
            placeholder="Ask the agent, e.g. List my files... (Press Enter to send)"
            rows={1}
          />
          <button
            className="send-button"
            onClick={() => handleSend()}
            disabled={loading || !input.trim()}
          >
            {loading ? "Replying..." : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}