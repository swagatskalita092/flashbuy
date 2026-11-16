// Mirrors app/schemas.py exactly. If the backend schema changes, this file
// must change with it, not the other way around.

export interface ProductOut {
  id: string;
  name: string;
  stock: number;
  price_cents: number;
}

export interface WaitingRoomStatusOut {
  ticket_id: string;
  product_id: string;
  buyer_id: string;
  position: number | null;
  admitted: boolean;
  admission_token: string | null;
  admission_expired: boolean;
}

// The JSON body built by admission_event_payload() in app/waiting_room.py,
// pushed as the single SSE "admission" event. Not the same shape as
// WaitingRoomStatusOut: this one carries expires_in_seconds, the status
// endpoint does not.
export interface AdmissionEvent {
  ticket_id: string;
  admitted: true;
  admission_token: string;
  expires_in_seconds: number;
}

export interface OrderOut {
  id: string;
  product_id: string;
  buyer_id: string;
  status: string;
  idempotency_key: string;
  expires_at: string;
  created_at: string;
}

// The three real outcomes checkout() can produce, per python-service/README.md:
// 201 reserved, 409 out of stock, 403 no/invalid token. "network" covers a
// genuine fetch failure, which is not a backend response at all and must be
// shown differently from a real 409/403.
export type CheckoutResult =
  | { kind: "success"; order: OrderOut }
  | { kind: "sold_out" }
  | { kind: "token_invalid_or_expired" }
  | { kind: "network_error"; detail: string };
