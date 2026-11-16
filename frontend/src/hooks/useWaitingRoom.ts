import { useCallback, useEffect, useRef, useState } from "react";
import { joinWaitingRoom, pollStatus } from "../api";
import type { AdmissionEvent, WaitingRoomStatusOut } from "../types";

export type QueueStage =
  | { kind: "idle" }
  | { kind: "joining" }
  | { kind: "waiting"; ticketId: string; position: number | null }
  | {
      kind: "admitted";
      ticketId: string;
      admissionToken: string;
      // Wall-clock deadline computed once, from the server's own
      // expires_in_seconds. Never recomputed from a client guess.
      expiresAt: number;
    }
  | { kind: "error"; message: string };

const STATUS_POLL_FALLBACK_MS = 4000;

/**
 * Joins the waiting room for one product/buyer, then prefers the real SSE
 * stream for admission (app/routes/waiting_room.py: GET
 * /waiting-room/stream/{ticket_id}). If the stream errors (proxy/browser
 * blocks it, connection drops), falls back to polling GET
 * /waiting-room/status/{ticket_id}, exactly as python-service/README.md
 * documents status polling as the fallback, not the primary path.
 */
export function useWaitingRoom(productId: string, buyerId: string) {
  const [stage, setStage] = useState<QueueStage>({ kind: "idle" });
  const eventSourceRef = useRef<EventSource | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  const cleanup = useCallback(() => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const applyStatus = useCallback((status: WaitingRoomStatusOut) => {
    if (status.admitted && status.admission_token) {
      // Status polling has no expires_in_seconds (only the SSE admission
      // payload does). If admission is discovered via polling rather than
      // the stream, there is no real TTL to show, so this is an honest
      // placeholder the UI must treat as "unknown", not a real countdown.
      setStage({
        kind: "admitted",
        ticketId: status.ticket_id,
        admissionToken: status.admission_token,
        expiresAt: NaN,
      });
      return;
    }
    setStage({
      kind: "waiting",
      ticketId: status.ticket_id,
      position: status.position,
    });
  }, []);

  const startPollingFallback = useCallback(
    (ticketId: string) => {
      if (pollTimerRef.current !== null) return;
      pollTimerRef.current = window.setInterval(async () => {
        try {
          const status = await pollStatus(ticketId);
          applyStatus(status);
          if (status.admitted) {
            cleanup();
          }
        } catch {
          // A transient poll failure is not fatal; the next tick retries.
        }
      }, STATUS_POLL_FALLBACK_MS);
    },
    [applyStatus, cleanup],
  );

  const openStream = useCallback(
    (ticketId: string) => {
      const es = new EventSource(`/waiting-room/stream/${ticketId}`);
      eventSourceRef.current = es;

      es.addEventListener("admission", (ev: MessageEvent) => {
        try {
          const payload: AdmissionEvent = JSON.parse(ev.data);
          setStage({
            kind: "admitted",
            ticketId: payload.ticket_id,
            admissionToken: payload.admission_token,
            expiresAt: Date.now() + payload.expires_in_seconds * 1000,
          });
        } catch {
          // Malformed payload: fall back to polling rather than trust it.
          startPollingFallback(ticketId);
        }
        cleanup();
      });

      es.onerror = () => {
        // The stream died before an admission event arrived. Fall back to
        // polling rather than leaving the buyer stuck with no updates.
        es.close();
        eventSourceRef.current = null;
        startPollingFallback(ticketId);
      };
    },
    [cleanup, startPollingFallback],
  );

  const join = useCallback(async () => {
    setStage({ kind: "joining" });
    try {
      const status = await joinWaitingRoom(productId, buyerId);
      if (status.admitted && status.admission_token) {
        // Immediate admission (queue was empty): no SSE wait needed.
        setStage({
          kind: "admitted",
          ticketId: status.ticket_id,
          admissionToken: status.admission_token,
          expiresAt: NaN,
        });
        return;
      }
      setStage({
        kind: "waiting",
        ticketId: status.ticket_id,
        position: status.position,
      });
      openStream(status.ticket_id);
    } catch (err) {
      setStage({ kind: "error", message: String(err) });
    }
  }, [productId, buyerId, openStream]);

  useEffect(() => cleanup, [cleanup]);

  return { stage, join, reset: () => { cleanup(); setStage({ kind: "idle" }); } };
}
