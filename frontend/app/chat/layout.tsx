import type { ReactNode } from "react";
import { ChatShell } from "@/components/ChatShell";

export const metadata = { title: "LLM Gateway" };

export default function ChatLayout({ children }: { children: ReactNode }) {
  return <ChatShell>{children}</ChatShell>;
}
