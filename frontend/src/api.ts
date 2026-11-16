import type {
  CheckoutResult,
  ProductOut,
  WaitingRoomStatusOut,
} from "./types";

// Every function here calls a real, verified FlashBuy endpoint. None of
// these shapes or status codes are guessed: they were read directly from
// app/routes/products.py, app/routes/waiting_room.py, and app/schemas.py.

export async function getProduct(productId: string): Promise<ProductOut> {
  const res = await fetch(`/products/${productId}`);
  if (!res.ok) {
    throw new Error(`GET /products/${productId} failed: ${res.status}`);
  }
  return res.json();
}

export async function joinWaitingRoom(
  productId: string,
  buyerId: string,
): Promise<WaitingRoomStatusOut> {
  const res = await fetch("/waiting-room/join", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_id: productId, buyer_id: buyerId }),
  });
  if (!res.ok) {
    throw new Error(`POST /waiting-room/join failed: ${res.status}`);
  }
  return res.json();
}

// Fallback only, per python-service/README.md: "the documented fallback for
// clients or networks that cannot use SSE." The primary path is the SSE
// stream opened in useWaitingRoom.
export async function pollStatus(
  ticketId: string,
): Promise<WaitingRoomStatusOut> {
  const res = await fetch(`/waiting-room/status/${ticketId}`);
  if (!res.ok) {
    throw new Error(`GET /waiting-room/status/${ticketId} failed: ${res.status}`);
  }
  return res.json();
}

export async function checkout(args: {
  productId: string;
  buyerId: string;
  idempotencyKey: string;
  admissionToken: string;
}): Promise<CheckoutResult> {
  let res: Response;
  try {
    res = await fetch("/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        product_id: args.productId,
        buyer_id: args.buyerId,
        idempotency_key: args.idempotencyKey,
        admission_token: args.admissionToken,
      }),
    });
  } catch (err) {
    return { kind: "network_error", detail: String(err) };
  }

  if (res.status === 201) {
    return { kind: "success", order: await res.json() };
  }
  if (res.status === 409) {
    return { kind: "sold_out" };
  }
  if (res.status === 403) {
    return { kind: "token_invalid_or_expired" };
  }
  return {
    kind: "network_error",
    detail: `unexpected checkout status ${res.status}`,
  };
}
