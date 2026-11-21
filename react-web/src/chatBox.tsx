// src/chatBox.tsx
import { useState } from "react";
import { streamMessageToAgent } from "./adkStream";

export function ChatBox({
  username,
  sessionId,
}: {
  username?: string;
  sessionId: string;
}) {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSend = async () => {
    if (!input.trim() || loading) return;
    const prompt = input;
    setInput("");
    setReply("");
    setLoading(true);

    await streamMessageToAgent({
      username,
      sessionId,
      message: prompt,
      callbacks: {
        onToken: (chunk) => setReply((prev) => prev + chunk),
        onDone: () => setLoading(false),
        onError: (e) => {
          setReply(`เกิดข้อผิดพลาด: ${e.message}`);
          setLoading(false);
        },
      },
    });
  };

  return (
    <div>
      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="สั่งงาน Agent จัดการไฟล์ เช่น ดูรายชื่อไฟล์ของฉัน..."
      />
      <button onClick={handleSend} disabled={loading}>
        {loading ? "กำลังตอบ..." : "ส่งข้อความ"}
      </button>
      <pre>{reply}</pre>
    </div>
  );
}