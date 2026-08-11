import { ConversationView } from "@/components/ConversationView";

export const metadata = { title: "Conversation · LLM Gateway" };

export default async function ConversationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ConversationView conversationId={id} />;
}
