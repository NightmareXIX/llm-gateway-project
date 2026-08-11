"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { api } from "@/lib/api";
import { GROUP_ORDER, dateGroup, type DateGroup } from "@/lib/format";
import { useConversations, useMe } from "@/lib/hooks";
import type { Conversation } from "@/lib/types";
import { AccountDialog } from "./AccountDialog";
import { ConversationItem } from "./ConversationItem";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { Button } from "./ui/Button";
import { Dialog } from "./ui/Dialog";
import { Skeleton } from "./ui/Skeleton";

/**
 * The conversation list.
 *
 * Four states, all of them real: loading (skeleton rows shaped like rows),
 * empty (an invitation, not a blank column), error (with a retry, because a
 * sidebar that silently shows nothing is indistinguishable from an account with
 * no history), and loaded.
 *
 * Rows are grouped by recency against `updated_at`, which is the field the
 * backend bumps on every turn — so a thread the user just replied to moves to
 * the top, which is the only ordering that matches how a person thinks about
 * their own conversations.
 */
export function ConversationSidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { conversations, error, isLoading, mutate } = useConversations();
  const params = useParams<{ id?: string }>();
  const activeId = params?.id ?? null;

  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);
  const [accountOpen, setAccountOpen] = useState(false);
  const router = useRouter();

  const groups = useMemo(() => groupByRecency(conversations ?? []), [conversations]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    const target = pendingDelete;
    setDeleting(true);
    setDeleteError(null);

    try {
      // Optimistic: drop the row now, roll the whole list back if the call
      // fails. `revalidate: false` because the DELETE returns 204 — there is
      // nothing to reconcile against until the next natural refetch.
      await mutate(
        async (current) => {
          await api.deleteConversation(target.id);
          return (current ?? []).filter((conversation) => conversation.id !== target.id);
        },
        {
          optimisticData: (current) =>
            (current ?? []).filter((conversation) => conversation.id !== target.id),
          rollbackOnError: true,
          revalidate: false,
        },
      );

      setPendingDelete(null);
      if (activeId === target.id) router.replace("/chat");
    } catch (deleteFailure) {
      setDeleteError(deleteFailure);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="px-3 py-3.5">
        <Link
          href="/chat"
          onClick={onNavigate}
          className="inline-flex items-center gap-2 rounded-control px-1 py-1 text-ink"
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-4 shrink-0 text-accent" aria-hidden>
            <path d="M12 2.5 21 7v10l-9 4.5L3 17V7l9-4.5Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
          <span className="text-sm font-semibold tracking-tight">LLM Gateway</span>
        </Link>
      </div>

      <div className="px-3 pb-3">
        {/* Left-aligned, icon boxed at a fixed size: a centred icon+label pair
            shifts horizontally with the label's width, which is what made this
            button read as unsettled next to a column of left-aligned rows. */}
        <Button
          variant="primary"
          className="w-full justify-start gap-2.5 px-3"
          onClick={() => {
            router.push("/chat");
            onNavigate?.();
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-4 shrink-0" aria-hidden>
            <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
          New conversation
        </Button>
      </div>

      <nav aria-label="Conversations" className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {isLoading && <SidebarSkeleton />}

        {!isLoading && error && (
          <div className="p-1">
            <ErrorState compact error={error} onRetry={() => void mutate()} />
          </div>
        )}

        {!isLoading && !error && conversations?.length === 0 && (
          <EmptyState
            size="sm"
            title="No conversations yet"
            description="Your threads will show up here once you send a message."
          />
        )}

        {!isLoading &&
          !error &&
          GROUP_ORDER.filter((group) => groups[group].length > 0).map((group) => (
            <section key={group} className="mb-4">
              <h2 className="px-2 pb-1.5 pt-2 text-xs font-medium uppercase tracking-wide text-ink-tertiary">
                {group}
              </h2>
              <ul className="flex flex-col gap-0.5">
                {groups[group].map((conversation) => (
                  <ConversationItem
                    key={conversation.id}
                    conversation={conversation}
                    active={conversation.id === activeId}
                    onNavigate={onNavigate}
                    onRequestDelete={() => setPendingDelete(conversation)}
                    onRenamed={() => void mutate()}
                  />
                ))}
              </ul>
            </section>
          ))}
      </nav>

      <AccountButton onOpen={() => setAccountOpen(true)} />
      <AccountDialog open={accountOpen} onClose={() => setAccountOpen(false)} />

      <Dialog
        open={pendingDelete !== null}
        onClose={() => {
          if (!deleting) {
            setPendingDelete(null);
            setDeleteError(null);
          }
        }}
        title="Delete this conversation?"
        description={
          <>
            <p>
              {pendingDelete?.title ? (
                <>
                  <span className="font-medium text-ink">{pendingDelete.title}</span> and its
                  messages will be permanently deleted.
                </>
              ) : (
                "This conversation and its messages will be permanently deleted."
              )}{" "}
              This can&apos;t be undone.
            </p>
            {deleteError !== null && <ErrorState compact className="mt-4" error={deleteError} />}
          </>
        }
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setPendingDelete(null);
                setDeleteError(null);
              }}
              disabled={deleting}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={deleting}
              loadingLabel="Deleting…"
              onClick={confirmDelete}
            >
              Delete
            </Button>
          </>
        }
      />
    </div>
  );
}

/**
 * The sidebar's account row — one control, not two.
 *
 * Everything lives inside a single button whose parts are flex children, so the
 * email can only ever truncate; it cannot run underneath an adjacent control the
 * way a separate identity block and Sign out button did. What it opens is the
 * account modal, which is where sign-out and appearance now live.
 */
function AccountButton({ onOpen }: { onOpen: () => void }) {
  const { me, isLoading } = useMe();

  return (
    <div className="border-t border-subtle p-2">
      {isLoading ? (
        <Skeleton className="h-11 w-full" />
      ) : (
        <button
          type="button"
          onClick={onOpen}
          aria-haspopup="dialog"
          className="flex w-full items-center gap-2.5 rounded-control px-2 py-2 text-left transition-colors hover:bg-sunken"
        >
          <span
            aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-full bg-accent-wash text-sm font-medium text-accent"
          >
            {(me?.email?.[0] ?? "?").toUpperCase()}
          </span>

          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm text-ink">{me?.email ?? "Signed in"}</span>
            {me?.tier && (
              <span className="block truncate text-xs text-ink-tertiary">{me.tier} tier</span>
            )}
          </span>

          <svg viewBox="0 0 24 24" fill="none" className="size-4 shrink-0 text-ink-tertiary" aria-hidden>
            <circle cx="12" cy="12" r="3.2" stroke="currentColor" strokeWidth="1.6" />
            <path
              d="M12 3.6v1.7m0 13.4v1.7M4.8 12H3.1m17.8 0h-1.7M6.9 6.9 5.7 5.7m12.6 12.6-1.2-1.2m0-10.2 1.2-1.2M6.9 17.1l-1.2 1.2"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
          <span className="sr-only">Open account settings</span>
        </button>
      )}
    </div>
  );
}

function SidebarSkeleton() {
  return (
    <div className="flex flex-col gap-1 p-1" role="status" aria-label="Loading conversations">
      <Skeleton className="mb-1 ml-1 h-3 w-14" />
      {[0, 1, 2].map((row) => (
        <Skeleton key={row} className="h-9 w-full" />
      ))}
      <Skeleton className="mb-1 ml-1 mt-4 h-3 w-20" />
      {[3, 4].map((row) => (
        <Skeleton key={row} className="h-9 w-full" />
      ))}
    </div>
  );
}

function groupByRecency(conversations: Conversation[]): Record<DateGroup, Conversation[]> {
  const groups: Record<DateGroup, Conversation[]> = {
    Today: [],
    Yesterday: [],
    "Previous 7 days": [],
    Older: [],
  };

  for (const conversation of conversations) {
    groups[dateGroup(conversation.updated_at)].push(conversation);
  }
  return groups;
}
