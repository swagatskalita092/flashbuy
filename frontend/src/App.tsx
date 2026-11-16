import { useEffect, useRef, useState } from "react";
import { checkout, getProduct } from "./api";
import { useWaitingRoom } from "./hooks/useWaitingRoom";
import type { CheckoutResult, ProductOut } from "./types";

// Buyer identity for this demo session. A real deployment would come from
// auth; FlashBuy's backend has none yet (documented gap), so this mirrors
// that honestly rather than faking a login.
const BUYER_ID = "frontend-demo-buyer";
const PRODUCT_ID = import.meta.env.VITE_PRODUCT_ID as string | undefined;

type LogLine = { time: string; text: string };

function nowHHMMSS(): string {
  return new Date().toISOString().slice(11, 19);
}

export function App() {
  const [product, setProduct] = useState<ProductOut | null>(null);
  const [clock, setClock] = useState(nowHHMMSS());
  const [log, setLog] = useState<LogLine[]>([]);
  const [checkoutResult, setCheckoutResult] = useState<CheckoutResult | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const idempotencyKey = useRef(crypto.randomUUID());

  const { stage, join, reset } = useWaitingRoom(PRODUCT_ID ?? "", BUYER_ID);

  const appendLog = (text: string) =>
    setLog((lines) => [...lines, { time: nowHHMMSS(), text }]);

  // Live clock, matches the verified prototype's header clock.
  useEffect(() => {
    const id = window.setInterval(() => setClock(nowHHMMSS()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Real stock, polled from the one endpoint that actually exists for this:
  // GET /products/{id} ("return current stock so clients ... can audit
  // inventory", per app/routes/products.py).
  useEffect(() => {
    if (!PRODUCT_ID) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await getProduct(PRODUCT_ID);
        if (!cancelled) setProduct(p);
      } catch {
        // A transient failure here just means the counter goes stale for
        // one tick; it should not crash the page.
      }
    };
    poll();
    const id = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (stage.kind === "waiting") {
      appendLog(`QUEUE_JOINED  Ticket ${stage.ticketId.slice(0, 8)} registered.`);
    }
    if (stage.kind === "admitted") {
      appendLog(`ADMISSION_GRANTED  Token issued.`);
    }
  }, [stage.kind]);

  const handleJoin = () => {
    if (!PRODUCT_ID) {
      appendLog("CONFIG_ERROR  VITE_PRODUCT_ID is not set.");
      return;
    }
    join();
  };

  const handleCheckout = async () => {
    if (stage.kind !== "admitted" || !PRODUCT_ID) return;
    setCheckingOut(true);
    const result = await checkout({
      productId: PRODUCT_ID,
      buyerId: BUYER_ID,
      idempotencyKey: idempotencyKey.current,
      admissionToken: stage.admissionToken,
    });
    setCheckingOut(false);
    setCheckoutResult(result);
    appendLog(
      result.kind === "success"
        ? "CHECKOUT_COMPLETE / 201  Order recorded."
        : result.kind === "sold_out"
          ? "INVENTORY_EXHAUSTED / 409  No order created."
          : result.kind === "token_invalid_or_expired"
            ? "TOKEN_EXPIRED / 403  No order created."
            : `NETWORK_ERROR  ${result.detail}`,
    );
  };

  const handleReset = () => {
    reset();
    setCheckoutResult(null);
    idempotencyKey.current = crypto.randomUUID();
  };

  const badge =
    checkoutResult?.kind === "success"
      ? { text: "● SUCCESS", cls: "badge green" }
      : checkoutResult
        ? { text: "● " + (checkoutResult.kind === "sold_out" ? "SOLD OUT" : "FAILED"), cls: "badge red" }
        : stage.kind === "admitted"
          ? { text: "● ADMITTED", cls: "badge green" }
          : { text: "● WAITING", cls: "badge" };

  return (
    <>
      <header>
        <div className="brand">
          LAUNCH <span className="brand-divider">/</span>{" "}
          <span className="brand-description">CHECKOUT CONTROL</span>
        </div>
        <div className="header-meta">
          <span>REGION EU-WEST-1</span>
          <span>{clock} UTC</span>
        </div>
      </header>

      <main className="workspace">
        <div className="title-row">
          <div>
            <div className="label">FLASHBUY / LIVE DEMO</div>
            <h1>
              {checkoutResult
                ? checkoutResult.kind === "success"
                  ? "Allocation confirmed."
                  : "Checkout did not complete."
                : stage.kind === "admitted"
                  ? "Your checkout window is open."
                  : "You're in the queue."}
            </h1>
            <p>
              Every number on this screen comes from FlashBuy's real backend,
              not a simulated feed.
            </p>
          </div>
          <div className={badge.cls}>{badge.text}</div>
        </div>

        <div className="layout">
          <section className="main-panel">
            <div className="panel-top">
              <span className="label">LIVE ALLOCATION</span>
              <span className="mono live">◦ real backend / 2s poll</span>
            </div>

            <div className="telemetry">
              <div className="label">AVAILABLE INVENTORY</div>
              <div className="mono stock">
                {product ? String(product.stock).padStart(3, "0") : "—"}
              </div>
              <div className="mono stock-bottom">
                <span>UNITS REMAINING</span>
                <span>{product?.name ?? "loading product..."}</span>
              </div>
            </div>

            <div className="queue">
              {stage.kind === "idle" && (
                <button className="primary" onClick={handleJoin}>
                  Join waiting room <span>→</span>
                </button>
              )}

              {stage.kind === "joining" && <p>Joining...</p>}

              {stage.kind === "waiting" && (
                <>
                  <div className="label">YOUR QUEUE POSITION</div>
                  <div className="position mono">
                    {stage.position ?? "—"} <small>ahead of you</small>
                  </div>
                  <p className="note">
                    Connected to the real SSE stream. Keep this tab open.
                  </p>
                </>
              )}

              {stage.kind === "admitted" && !checkoutResult && (
                <>
                  <div className="label" style={{ color: "var(--green)" }}>
                    ACCESS GRANTED
                  </div>
                  <p>
                    Token: <span className="mono">{stage.admissionToken}</span>
                  </p>
                  <button
                    className="primary"
                    disabled={checkingOut}
                    onClick={handleCheckout}
                  >
                    {checkingOut ? "Checking allocation..." : "Confirm checkout →"}
                  </button>
                </>
              )}

              {checkoutResult && (
                <>
                  <p>
                    {checkoutResult.kind === "success"
                      ? "Order reserved. Nothing further happens in this demo."
                      : checkoutResult.kind === "sold_out"
                        ? "Stock hit zero before your checkout finished. No order was created."
                        : checkoutResult.kind === "token_invalid_or_expired"
                          ? "Your admission token was no longer valid. No order was created."
                          : `Request failed: ${checkoutResult.detail}`}
                  </p>
                  <button className="primary" onClick={handleReset}>
                    Start a new session →
                  </button>
                </>
              )}

              {stage.kind === "error" && <p>Error: {stage.message}</p>}
            </div>
          </section>
        </div>

        <section className="log" aria-label="Session events">
          <div className="label">
            SESSION EVENTS <span className="log-arrow">↳</span>
          </div>
          <div className="log-lines">
            {log.map((line, i) => (
              <div key={i}>
                <time>{line.time}</time>
                {line.text}
              </div>
            ))}
          </div>
        </section>

        <footer>
          <div>FLASHBUY FRONTEND v0.1 &nbsp;/&nbsp; REAL BACKEND, NO SIMULATED DATA</div>
        </footer>
      </main>
    </>
  );
}
