/**
 * Standalone send_prompt test — run after rate limit window expires.
 * bun src/test-prompt.ts
 */
import { finalizeEvent, generateSecretKey, getPublicKey, nip44 } from "nostr-tools";
import { Relay } from "nostr-tools/relay";

const SERVER_PUBKEY = "10814a5dc07e3e876867ffb9e8781af47fa599b677fb27e051f6b5261c1d4f35";
const RELAYS = ["wss://nostr.mom", "wss://relay.primal.net", "wss://nos.lol"];
const whitelistedNpub = "npub1demo0000felix0000000000000000000000000000000000000000000000felix";

const clientSk = generateSecretKey();
const clientPk = getPublicKey(clientSk);
let reqId = 0;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void; timeout: any }>();

async function main() {
  console.log(`[test] Client pubkey: ${clientPk}`);
  const relays: Relay[] = [];
  for (const url of RELAYS) {
    try {
      const relay = await Promise.race([
        Relay.connect(url),
        new Promise<never>((_, rej) => setTimeout(() => rej(new Error("timeout")), 10_000)),
      ]);
      relay.subscribe([{ kinds: [1059, 21059], limit: 0 }], {
        onevent: (event) => {
          try {
            const pTag = event.tags?.find((t) => t[0] === "p");
            if (!pTag || pTag[1] !== clientPk) return;
            const convKey = nip44.v2.utils.getConversationKey(clientSk, event.pubkey);
            const decrypted = nip44.v2.decrypt(event.content, convKey);
            const innerEvent = JSON.parse(decrypted);
            const mcpMsg = JSON.parse(innerEvent.content);
            if (mcpMsg.id && pending.has(mcpMsg.id)) {
              const { resolve, timeout } = pending.get(mcpMsg.id)!;
              clearTimeout(timeout);
              pending.delete(mcpMsg.id);
              let resultData = mcpMsg.result;
              if (resultData?.content) {
                for (const item of resultData.content) {
                  if (item.type === "text") { try { resultData = JSON.parse(item.text); } catch {} }
                }
              }
              resolve(resultData);
            }
          } catch (e: any) { console.warn("[test] event error:", e.message); }
        },
      });
      relays.push(relay);
      console.log(`[test] Connected to ${url}`);
    } catch (e: any) { console.warn(`[test] Failed ${url}: ${e.message}`); }
  }

  await new Promise((r) => setTimeout(r, 2000));

  console.log("\n═══ send_prompt test ═══");
  try {
    const result = await callTool(relays, "send_prompt", { prompt: "Say hello in one word", npub: whitelistedNpub });
    console.log("[test] send_prompt result:", JSON.stringify(result, null, 2));
    if (result.ok) {
      console.log("[test] ✓ send_prompt succeeded");
      console.log("  provider:", result.provider);
      console.log("  model:", result.model);
      console.log("  tokens_used:", result.tokens_used);
      console.log("  cost_usd:", result.cost_usd);
      console.log("  new_balance:", result.new_balance);
      console.log("  scarcity_factor:", result.scarcity_factor);
    } else {
      console.log("[test] send_prompt returned:", result.error);
    }
  } catch (e: any) {
    console.error("[test] ✗ send_prompt failed:", e.message);
  }

  for (const r of relays) r.close();
  process.exit(0);
}

function callTool(relays: Relay[], toolName: string, args: any = {}): Promise<any> {
  return new Promise((resolve, reject) => {
    const id = ++reqId;
    const timeout = setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`Timeout (60s) for ${toolName}`)); }
    }, 60_000);
    pending.set(id, { resolve, reject, timeout });

    const mcpRequest = { jsonrpc: "2.0", method: "tools/call", params: { name: toolName, arguments: args }, id };
    const signedEvent = finalizeEvent({
      pubkey: clientPk, kind: 25910, tags: [["p", SERVER_PUBKEY]],
      content: JSON.stringify(mcpRequest), created_at: Math.floor(Date.now() / 1000),
    }, clientSk);

    const wrapSk = generateSecretKey();
    const wrapPk = getPublicKey(wrapSk);
    const convKey = nip44.v2.utils.getConversationKey(wrapSk, SERVER_PUBKEY);
    const encrypted = nip44.v2.encrypt(JSON.stringify(signedEvent), convKey);
    const giftWrap = finalizeEvent({
      kind: 1059, content: encrypted, tags: [["p", SERVER_PUBKEY]],
      created_at: Math.floor(Date.now() / 1000), pubkey: wrapPk,
    }, wrapSk);

    for (const relay of relays) relay.publish(giftWrap).catch(() => {});
  });
}

main().catch((e) => { console.error("FATAL:", e); process.exit(1); });