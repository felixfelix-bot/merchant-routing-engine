/**
 * Quick test client for the Sovereign Demo CVM server.
 * Uses direct nostr-tools (same pattern as reference hermes-insights-cvm test-client).
 *
 * Run: bun src/test-client.ts
 */
import { finalizeEvent, generateSecretKey, getPublicKey, nip44 } from "nostr-tools";
import { Relay } from "nostr-tools/relay";

const SERVER_PUBKEY = "10814a5dc07e3e876867ffb9e8781af47fa599b677fb27e051f6b5261c1d4f35";
const RELAYS = ["wss://nostr.mom", "wss://relay.primal.net", "wss://nos.lol"];

// Generate a fresh client key (MUST be different from server key)
const clientSk = generateSecretKey();
const clientPk = getPublicKey(clientSk);

let reqId = 0;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void; timeout: any }>();

const whitelistedNpub = "npub1demo0000felix0000000000000000000000000000000000000000000000felix";

async function main() {
  console.log(`[test] Client pubkey: ${clientPk}`);
  console.log(`[test] Server pubkey: ${SERVER_PUBKEY}`);
  console.log(`[test] Relays: ${RELAYS.join(", ")}`);

  const relays: Relay[] = [];
  for (const url of RELAYS) {
    try {
      const relay = await Promise.race([
        Relay.connect(url),
        new Promise<never>((_, rej) => setTimeout(() => rej(new Error("timeout")), 10_000)),
      ]);
      console.log(`[test] Connected to ${url}`);

      // Broad filter + client-side p-tag check (NIP-12 gap)
      relay.subscribe(
        [{ kinds: [1059, 21059], limit: 0 }],
        {
          onevent: (event) => {
            try {
              const pTag = event.tags?.find((t) => t[0] === "p");
              if (!pTag || pTag[1] !== clientPk) return;

              const convKey = nip44.v2.utils.getConversationKey(clientSk, event.pubkey);
              const decrypted = nip44.v2.decrypt(event.content, convKey);
              const innerEvent = JSON.parse(decrypted);
              const mcpMsg = JSON.parse(innerEvent.content);

              console.log(`[test] Received response for req ${mcpMsg.id}`);

              if (mcpMsg.id && pending.has(mcpMsg.id)) {
                const { resolve, timeout } = pending.get(mcpMsg.id)!;
                clearTimeout(timeout);
                pending.delete(mcpMsg.id);

                let resultData = mcpMsg.result;
                if (resultData?.content) {
                  for (const item of resultData.content) {
                    if (item.type === "text") {
                      try { resultData = JSON.parse(item.text); } catch {}
                    }
                  }
                }
                resolve(resultData);
              }
            } catch (e: any) {
              console.warn("[test] Failed to handle event:", e.message);
            }
          },
          oneose: () => console.log(`[test] EOSE from ${url}`),
        },
      );
      relays.push(relay);
    } catch (e: any) {
      console.warn(`[test] Failed to connect ${url}: ${e.message}`);
    }
  }

  if (!relays.length) {
    console.error("[test] No relay connections! Aborting.");
    process.exit(1);
  }

  console.log("[test] Waiting 2s for subscriptions to settle…");
  await new Promise((r) => setTimeout(r, 2000));

  // ── Test 1: get_snapshot ─────────────────────────────────────────────────
  console.log("\n═══ TEST 1: get_snapshot ═══");
  try {
    const result = await callTool(relays, "get_snapshot", {});
    console.log("[test] ✓ get_snapshot returned", JSON.stringify(result).length, "bytes");
    console.log("  participants:", result.participants?.count || 0);
    console.log("  pricing keys:", Object.keys(result.pricing || {}).join(", "));
    console.log("  dispatch_gate:", result.dispatch_gate?.can_dispatch);
    console.log("  scarcity:", result.scarcity?.factor, result.scarcity?.level);
  } catch (e: any) {
    console.error("[test] ✗ get_snapshot failed:", e.message);
  }

  // ── Test 2: register_participant ────────────────────────────────────────
  console.log("\n═══ TEST 2: register_participant (whitelisted) ═══");
  try {
    const result = await callTool(relays, "register_participant", { npub: whitelistedNpub });
    console.log("[test] ✓ register_participant:", JSON.stringify(result));
  } catch (e: any) {
    console.error("[test] ✗ register_participant failed:", e.message);
  }

  // ── Test 3: register_participant (NOT whitelisted) ───────────────────────
  console.log("\n═══ TEST 3: register_participant (non-whitelisted — should fail) ═══");
  try {
    const result = await callTool(relays, "register_participant", { npub: "npub1notwhitelisted00000000000000000000000000000000000000000000000xyz" });
    console.log("[test] Result:", JSON.stringify(result));
    if (result.ok === false && result.error?.includes("whitelist")) {
      console.log("[test] ✓ Whitelist correctly rejected non-whitelisted npub");
    } else {
      console.log("[test] ✗ Whitelist did NOT reject non-whitelisted npub!");
    }
  } catch (e: any) {
    console.error("[test] ✗ register_participant (non-whitelisted) error:", e.message);
  }

  // ── Test 4: get_ledger ───────────────────────────────────────────────────
  console.log("\n═══ TEST 4: get_ledger ═══");
  try {
    const result = await callTool(relays, "get_ledger", {});
    console.log("[test] ✓ get_ledger returned", result.length, "participants");
    if (result.length > 0) console.log("  first:", JSON.stringify(result[0]));
  } catch (e: any) {
    console.error("[test] ✗ get_ledger failed:", e.message);
  }

  // ── Test 5: get_price_history ───────────────────────────────────────────
  console.log("\n═══ TEST 5: get_price_history ═══");
  try {
    const result = await callTool(relays, "get_price_history", { hours: 24 });
    console.log("[test] ✓ get_price_history returned", result.points?.length || 0, "points");
    if (result.points?.length > 0) console.log("  first:", JSON.stringify(result.points[0]));
  } catch (e: any) {
    console.error("[test] ✗ get_price_history failed:", e.message);
  }

  // ── Test 6: send_prompt ─────────────────────────────────────────────────
  console.log("\n═══ TEST 6: send_prompt ═══");
  try {
    const result = await callTool(relays, "send_prompt", { prompt: "Say hello in one word", npub: whitelistedNpub });
    console.log("[test] ✓ send_prompt:", JSON.stringify(result).substring(0, 300));
  } catch (e: any) {
    console.error("[test] ✗ send_prompt failed:", e.message);
  }

  // ── Test 7: rate limiting (second prompt within 5s — should fail) ────────
  console.log("\n═══ TEST 7: rate limiting (immediate second prompt — should fail) ═══");
  try {
    const result = await callTool(relays, "send_prompt", { prompt: "Say goodbye in one word", npub: whitelistedNpub });
    if (result.ok === false && result.error?.includes("rate")) {
      console.log("[test] ✓ Rate limiting correctly rejected:", result.error);
    } else {
      console.log("[test] Result:", JSON.stringify(result).substring(0, 200));
      console.log("[test] ⚠ Rate limit may not have triggered (first prompt was > 5s ago)");
    }
  } catch (e: any) {
    console.error("[test] ✗ rate limit test error:", e.message);
  }

  console.log("\n[test] All tests complete. Closing relays.");
  for (const r of relays) r.close();
  process.exit(0);
}

function callTool(relays: Relay[], toolName: string, args: any = {}): Promise<any> {
  return new Promise((resolve, reject) => {
    const id = ++reqId;
    const timeout = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`Timeout (30s) for ${toolName}`));
      }
    }, 30_000);

    pending.set(id, { resolve, reject, timeout });

    const mcpRequest = {
      jsonrpc: "2.0",
      method: "tools/call",
      params: { name: toolName, arguments: args },
      id,
    };

    const innerEvent = {
      pubkey: clientPk,
      kind: 25910,
      tags: [["p", SERVER_PUBKEY]],
      content: JSON.stringify(mcpRequest),
      created_at: Math.floor(Date.now() / 1000),
    };

    const signedEvent = finalizeEvent(innerEvent, clientSk);

    const wrapSk = generateSecretKey();
    const wrapPk = getPublicKey(wrapSk);
    const convKey = nip44.v2.utils.getConversationKey(wrapSk, SERVER_PUBKEY);
    const encrypted = nip44.v2.encrypt(JSON.stringify(signedEvent), convKey);

    const giftWrap = finalizeEvent({
      kind: 1059,
      content: encrypted,
      tags: [["p", SERVER_PUBKEY]],
      created_at: Math.floor(Date.now() / 1000),
      pubkey: wrapPk,
    }, wrapSk);

    for (const relay of relays) {
      relay.publish(giftWrap).catch(() => {});
    }
  });
}

main().catch((e) => { console.error("FATAL:", e); process.exit(1); });