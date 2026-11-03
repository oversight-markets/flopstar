# FLOP: what the SDK, CLI and devnet actually provide, and Flopstar's design

Phase 0 findings for making Flopstar a FLOP agent (buy inference, keep verifiable records, report to
Touchstone). Researched 2026-11-01 from the published packages' **source**, FLOP's docs, and
read-only probes of devnet from the Flopstar droplet. Everything below is cited; anything not
confirmed by a source is listed as a `SPEC:` question in §9.

> **Decision (2026-11-01): key design A (§7).** The owner key does nothing on FLOP until FLOP
> publishes the DID-signed flow (S1); a separate capped spend account pays escrow. Phases 0–7 are
> built (§12); live spending waits on a CLI runtime here and FLOP's answers to S1, S3, S4.
>
> Original stop note: the published code does not
> match the brief's key model: nothing links a `did:key` to a chain account, the faucet is not
> authenticated by identity, direct-session request signing uses a fresh per-channel key (not the
> DID key), and on-chain session-key caps cannot be applied to direct sessions with any published
> tool. The Python SDK's agent-wallet and session-key functions do not work against the live
> runtime at all (§2.4).

## 1. What was examined

| Artifact | Version | Integrity checked |
|---|---|---|
| `flop-sdk` (PyPI wheel) | 0.2.0, uploaded 2026-10-29 | sha256 `8951b746…e06e5bd` matches PyPI |
| `@flop-labs/cli` (npm) | 0.4.0 (`dist/cli.js`, one 12.8k-line bundle) | sha512 integrity matches registry |
| `@flop-labs/sdk` (npm) | 0.4.0 | sha512 integrity matches registry |
| `@flop-labs/client` (npm) | 0.2.1 | sha512 integrity matches registry |
| CLI container | `ghcr.io/flop-labs/flop-core/cli:v0.1.220@sha256:c8f3456c48d41cec9d0229d1e1dd9eb8d6247a22f350c51078d85f129601bbbd` | from `https://flop.network/releases.json` (not pulled: no Docker here) |
| Docs | `docs.flop.network/llms.txt?network=devnet`; pages `testnet-and-timeline`, `agent-integration`, `agent-accounts-and-spend-control`, `airdrop-and-testnet-conversion`, `send-to-specific-miner`, `sdk-references` (last updated 2026-08-19 … 2026-10-30) | — |
| Devnet runtime | `tzkv-runtime` spec `1014003`, metadata read over `https://rpc.devnet.flop.network` | — |

File references below are into those packages: `flop_sdk/…` (Python wheel), `cli.js` (CLI
`dist/cli.js`), `sdk:…` (`@flop-labs/sdk/dist/…`).

## 2. `flop-sdk` 0.2.0 (Python)

### 2.1 Layout and public API

Dependencies: `aiohttp`, `langfuse>=2,<3`, `pydantic>=2`, `substrate-interface>=1` (1.8.1
resolved), `typing-extensions`. Python ≥ 3.10.

| Module | What it provides | Signs? |
|---|---|---|
| `client.py` | `FlopClient(FlopClientOptions)`: async aiohttp JSON-RPC. `health()` (`system_health`), `check_htlc_status(hash_lock)` (`hasStation_checkHTLCStatus`), `wait_for_settlement`, `get_balance`/`get_account_info` (`system_account`, returns **float**), `submit_poui`, `get_compute_task`, `get_tee_quote`. `create_htlc` and `buy_verified_inference` always raise `SIGNED_EXTRINSIC_REQUIRED`. `assert_supported_runtime` + `SUPPORTED_SPEC_VERSIONS` (1013000, 1014000–1014005, 1014007–1014011). | no |
| `substrate.py` | `SubstrateClient(SubstrateOptions(rpc_url, seed, ss58_format=42))` over substrate-interface: `create_htlc`, `redeem_htlc`, `check_htlc_status`, `get_balance` (float). Module functions `create_agent_wallet`, `fund_agent_wallet`, `agent_transfer`, `owner_withdraw`, `deactivate_wallet`, `create_session`, `revoke_session`, `execute_via_session`, `get_agent_wallet`, `get_session`. Runtime version re-checked before metadata refresh and before every submit (`substrate.py:44-55`). | yes: every extrinsic with `client.keypair` |
| `agent_wallet.py` | Duplicate wallet/session functions plus `reactivate_wallet`, `get_wallet_info`, `get_total_wallets`, `get_owner_wallet_count`, `generate_session_keypair()` (returns the **mnemonic in a plain dict**, `agent_wallet.py:354-375`). | yes |
| `compute_channel.py` | Pure encoders: `channel_id_v1`, `task_hash_v1`, `h_ids`, transcript leaves V0–V3, `receipt_message_v1` (domain `FLOP/COMPUTE_CHANNEL/RECEIPT`), Merkle root/path, FCC4 transcript blob encode/decode, `encode_verified_turn`. No I/O, no signature verification. | no |
| `utils.py` | `generate_htlc_secrets`, `flop_to_raw`/`raw_to_flop` (**float**), `format_flop`, `format_address`, `validate_address` (checks a `flop1` prefix, which no FLOP address has), `generate_agent_id`, `sign_message` (Ed25519 via PyNaCl). | `sign_message` |
| `x402.py` | `x402_fetch`, payment-header helpers. Broken: calls `.get()` on the `HtlcInfo` dataclass (`x402.py:212`) and expects a `.substrate` attribute `FlopClient` never has (`x402.py:186`). | via `SubstrateClient` |
| `staking_client.py`, `da_client.py` | Miner/validator staking, DA leases, model weights. Take an explicit `(substrate, keypair)`. | yes |
| `stream.py` | Polling watchers for HTLCs/accounts. | no |
| `tracing.py` | Langfuse 2.x tracing; **inactive unless code calls `init_tracing()`** with `LANGFUSE_*` keys (not imported by `__init__`). Default host `cloud.langfuse.com`. | no |
| `errors.py`, `agent/errors.py` | Two more error hierarchies (`errors.FlopError`, `agent.errors.AgentError`), separate from `client.FLOPError`, which is what most code raises. `CircuitBreakerTrippedError` derives from `errors.FlopError`, so `except FLOPError` misses it. | — |

### 2.2 What it does **not** have

No direct session / compute-channel client (`runDirectSession` is TypeScript-only), no receipt
verification, no faucet client, no keystore loader, no did:key handling, no gateway client, no
`expire_force_open` / `timeout` / `finalize` calls, no offer discovery. The brief's description of
the Python SDK is accurate on this point.

### 2.3 Key handling

- Key type: substrate-interface `Keypair`, default **sr25519**, SS58 format 42.
- **Critical: `SubstrateClient` turns a secret seed into a public-derived key.** It builds the
  keypair with `Keypair.create_from_uri(f"//{seed.capitalize()}")` and only falls back to
  `create_from_mnemonic` if that raises (`substrate.py:115-119`). A `//X` URI with no phrase derives
  from substrate's public `DEV_PHRASE`, and it does not raise for a mnemonic. Verified in a
  scratch venv with a throwaway mnemonic: the SDK's key equals `DEV_PHRASE//<Mnemonic>` and differs
  from the mnemonic's real key. Anyone who knows the seed string can recompute the key, and the
  operator would believe it came from their mnemonic. **Flopstar never passes a secret to
  `SubstrateOptions.seed`.** It loads an encrypted Polkadot JSON keystore with
  `Keypair.create_from_encrypted_json` and injects the keypair.
- Default `seed="alice"` (`substrate.py:35`, `types.py:24`): a client built without options signs as
  the public dev account `//Alice`.
- `utils.sign_message` silently returns `sha256(message + key)`, which is not a signature, if PyNaCl
  is missing (`utils.py:39-41`). PyNaCl is not a declared dependency (it arrives via
  substrate-interface).

### 2.4 Checked against the live devnet runtime (spec 1014003)

| SDK call (`compose_call`) | Runtime has | Result |
|---|---|---|
| `AgentWallet.createWallet`, `fundWallet`, `agentTransfer`, `ownerWithdraw`, `deactivateWallet`, `reactivateWallet` | `AgentWallet.create_wallet`, `fund_wallet`, … (snake_case) | **fails**: call not found |
| `SessionKeys.createSession` / `revokeSession` / `executeViaSession` | pallet is **`FlopSessionKeys`**; `create_session(delegate, spending_cap, per_tx_limit, daily_cap, duration_blocks, allowed_pallets)`; `execute_via_session(owner, call, spend_amount, nonce)` | **fails**: wrong pallet, missing `per_tx_limit`/`daily_cap`, missing `nonce` |
| `HasStation.create_htlc`, `redeem_htlc`, `create_htlc_for_work`, `redeem_htlc_with_receipt` | present | names match |

The Python SDK's agent-wallet and session-key functions cannot be used as published.

### 2.5 Amounts

Every amount in the Python SDK API is a `float` FLOP value converted with
`int(amount * 10**18)` (`utils.py:58`, `client.py:94-101`), and every read returns a float. That is
lossy above ~2^53 base units (≈0.009 FLOP). Devnet already shows the symptom: explorer channel
`0x1beccc73…954d` has escrow `4727999999999999759` (4.728 FLOP minus 241 base units). Flopstar
keeps amounts as `int` base units and `Decimal`, and never passes floats to the SDK. (Passing a
`Decimal` to `flop_to_raw` happens to be exact, but we don't rely on SDK conversion at all.)

## 3. TypeScript SDK 0.4.0 and `flop` CLI 0.4.0

### 3.1 Network profiles (`sdk:chain.js:23-55`, shared by the CLI)

| | RPC | Gateway | Faucet | Status | Genesis policy |
|---|---|---|---|---|---|
| devnet | `wss://rpc.devnet.flop.network` | `https://api.flop.network` | `https://faucet.flop.network` | live | compare-services |
| testnet | `wss://testnet.flop.network` | `https://gateway.testnet.flop.network` | `https://faucet.testnet.flop.network` | **planned** | **pin-before-live** |

FLOP is "a stock Substrate chain: `MultiSignature` (sr25519 + ed25519 + ecdsa) over `AccountId32`,
SS58 prefix 42" with 18 decimals (`sdk:chain.js:1-14`).

### 3.2 Faucet (`sdk:faucet.js`)

- `GET {faucet}/chain` → `{network, genesisHash, caip2, …}` (`faucet.js:34`). The CLI refuses to
  fund if it differs from the RPC's genesis (`cli.js:5556-5557`).
- `POST {faucet}/fund` with JSON `{address, amount?, ref?, source?}` (`faucet.js:87`).
  **No signature and no identity**: it is keyed by SS58 address only. A non-2xx returns
  `{error}` and may carry `Retry-After`. Default amount 25 FLOP.
- CLI: `flop --network <net> faucet request <ss58> [--amount N]`.

### 3.3 Direct sessions: `flop session run` → `runDirectSession` (`cli.js:5519-5583`, `sdk:compute-channel/direct-session.js`)

- Options: `--miner <ss58>` (required), `--model <slug>`, `--prompt`, `--max-flop <decimal>`
  (parsed exactly to base units, ≤18 fractional digits, `cli.js:5504-5510`), `--max-tokens`
  (default 256), `--via auto|offer|force` (default `auto`), **`--endpoint <URL>` (default
  `http://127.0.0.1:8645`)**, `--auth-token-env`, `--new-wallet`, `--json`.
- **`--via` is the channel-open path, not miner discovery.** `auto` looks up a live signed offer for
  *the named miner* at `{indexer}/api/session-offers?miner=&model=&minExpiry=&limit=100`
  (`sdk:compute-channel/direct-session-chain.js:105-140`), else falls back to `force_open`. A
  miner SS58 and a reachable endpoint are always required.
- Signing: the chain signer (`--keystore` + `--password-env`, or `--signer <uri>`) signs
  `open_channel_with_offer` / `force_open` (the escrow lock), and later `expire_force_open` /
  `timeout` / close. Miner-facing HTTP requests are signed by a **fresh ephemeral `SessionKey`
  generated per channel** (`direct-session.js:39`). The DID key plays no part.
- Verification: model identity resolved from `ModelRegistry` at finality, finalized channel
  identity and enclave key cross-checked against `/session/info`, turn tariff checked against
  escrow before acking, receipts verified before payment (`direct-session.js:136-169`).
- Recovery built in: on failure it waits and calls `expire_force_open` (unacked), `timeout`
  (zero-output open past `max_duration`) or force-settles; if recovery itself fails it throws
  `DirectSessionError` whose **message** ends `"(channel 0x<64 hex>)"` (`direct-session.js:10-16,
  188-205`).
- JSON output (`cli.js:5571-5580`): `reply`, `miner`, `channel`, `settle_tx`, `cooperative`,
  `verified_turns` (= number of receipts; the command throws rather than return an unverified
  receipt), `wallet` (only with `--new-wallet`), `finish_reason`, `completion_tokens`. **Not
  included:** settled amount, receipt bodies, token IDs, transcript or timings.
- Errors (`cli.js:5045-5063`): stderr JSON `{ok:false, code, message, retryable, next?}`, exit 1.
- Keystore: encrypted Polkadot JSON; password only via the env var named by `--password-env`
  (`cli.js:5176-5187`). There is no `--password-env` default.

### 3.4 Other CLI paths relevant to agents

- `flop keys generate --type sr25519|ed25519 --out f.json --password-env VAR` writes an encrypted
  keystore (default type `ed25519` "for custody", `sr25519` "for agents").
- `flop session create --delegate <ss58> --daily-cap --tx-limit`, `flop … chat --session-owner`:
  the HTLC chat/proxy paths route through `execute_via_session`. **`session run` does not.**
- `flop wallet agent create <ss58> --daily-cap --tx-limit` converts caps with `parseFloat` (float).
- `flop --network devnet agent credential` enrols a **gateway API key** by signing the gateway's
  single-use challenge with a local sr25519 account it creates **unencrypted** at
  `~/.flop/devnet-enroll-wallet.json` and funds from the faucet (README "Shared devnet agent
  credential"; `cli.js:6453`). Devnet only.
- `--new-wallet` (devnet only) writes an **unencrypted** wallet to `~/.flop/session-wallets/`
  (`cli.js:5554`, docs `send-to-specific-miner`).

### 3.5 Gateway

`https://api.flop.network/v1`: `GET /models` needs no key; chat completions need a funded API key.
`@flop-labs/client` speaks it; `@flop-labs/contracts` 0.1.1 publishes JSON Schemas for the payloads
(not yet fetched). FLOP's own guidance: "Use a bounded child credential with a spend cap rather than
an owner key" (`sdk-references`).

## 4. Devnet from this server (probed 2026-11-01, read-only)

| Endpoint | Result |
|---|---|
| `https://rpc.devnet.flop.network` (JSON-RPC over HTTPS) | **reachable**. `system_chain` "FLOP Devnet", genesis `0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631`, spec 1014003, ss58 42, 18 decimals, 11 peers, not syncing |
| `https://faucet.flop.network/chain` | **reachable**, same genesis, `network: devnet`. `/fund` not called |
| `https://api.flop.network/v1/models` | **reachable**: one model, `qwen/qwen2.5-0.5b-instruct`, pricing `"0.006"` prompt/completion (unit unstated) |
| `POST /v1/chat/completions` without a key | HTTP 402 `payment_required`: "Direct public payment-proof settlement is not enabled. Use a funded FLOP API key." |
| `https://explorer.devnet.flop.network/api/session-offers` | reachable, **0 live offers** |
| `…/api/miners` | 19 miners (18 active, calibrated). **No endpoint field**, so no public way to find a session endpoint |
| `…/api/accounts/5Dhx2pBY…` (our DID as an ed25519 account) | exists as an empty account: balance 0, nonce 0 |
| `https://mcp.flop.network/mcp` | reachable, read-only tools (`flop_list_session_offers`, `flop_find_best_miner`, `flop_get_compute_channel`, …), no run-session tool |
| `testnet.flop.network`, `faucet.testnet…`, `gateway.testnet…` | **do not resolve** (2 days before the target date) |
| Node.js / Docker / Podman on this server | **none installed**, so the CLI cannot run here yet |

The docs disagree on devnet agent access: "Agents can already reach the devnet endpoints,
rate-limited", but "Routing an agent's inference to FLOP … is not possible on devnet, which is closed
to a small number of institutional partners" (`testnet-and-timeline`).

**Net: reads work. Paid inference is blocked from here on every route:** direct needs a reachable
miner endpoint (none public, no offers), the gateway needs a funded API key (enrolment needs the
CLI), and native doesn't exist in Python.

### 4.1 Re-checked 2026-11-03 after FLOP's "devnet is open for builders" post

FLOP Labs posted that devnet is open for builders, listing explorer, faucet, gateway, MCP, the npm
SDK, docs and `ask.flop.finance`, with the warning that devnet tokens have no value, earn no rewards
or airdrop weight, and the chain may reset. Read-only checks from the droplet the same day:

| Check | Result |
|---|---|
| Links | All match FLOP's [official links page](https://flop.finance/official/) (FLOP Labs, updated 2026-10-23), which `flop.network` links to: `flop.finance` (site), `ask.flop.finance` (docs chatbot), `docs.flop.network`, and `flop.network` for api/mcp/faucet/rpc. No new hosts for Flopstar's config. |
| Chain | Same genesis `0x99c1…d631`, spec **1014003** (unchanged), finalized ~2.79 M, 11 peers. |
| Packages | Unchanged: npm `@flop-labs/sdk` / `cli` 0.4.0, `client` 0.2.1 (all 2026-10-29); PyPI `flop-sdk` 0.2.0. |
| Gateway `/v1/models` | Still one model, `qwen/qwen2.5-0.5b-instruct`, `"0.006"` prompt/completion. New `x-flop.price_sources`: per-miner `devnet-nominal` prices with miner SS58s and ~2-minute expiries, but **no endpoints**. |
| Session offers | Still **0**. |
| MCP (`flop-network` 0.2.0) | 34 tools, still read-only for us: no run-session or pay tool. New since §4: `flop_get_inference_receipt` (possible answer to S6), `flop_agent_pack_*`, `flop_knowledge_*`. `flop_generate_secrets` generates HTLC secrets server-side; **never use it** (a secret the server made isn't ours). |
| Docs status pages | `network-devnet`: "development … administrator-reviewed disposable devnet admission". `network-testnet`: "planned". Testnet hostnames still **don't resolve** on its 3 Nov target date. |
| Faucet | `/chain` OK. Draws #2 (timer, 2 Nov) and #3 (manual, 3 Nov) both got **503 "IP classification unavailable; retry later"** (failed, no trip). The owner got the same 503 from another IP: a faucet-side outage, not our request. |

**Third-party report, 3 Nov (a public devnet user's post; not verified by us):** the faucet is down
for everyone; a wallet needs **1 FLOP from the faucet to activate a gateway API key** (bears on S4);
hosted gateway runs log spend under **one shared gateway account** (211 of 310 sessions) rather than
the caller's address, which matters for which `chain_account` Touchstone can attribute (T3); the
CLI's quick setup writes an unencrypted recovery phrase (§11.4); "2 miners online" for Qwen 2.5 0.5B.
What we could check: no new CLI exists (npm 0.4.0 and `releases.json` v0.1.220 are unchanged, so
"runs without Docker" means the npm package under Node); explorer `/api/sessions` returns 0, so we
can't reproduce the 310 count.

Effect on the blockers: none removed. The post settles which way S10 goes for builders (devnet is
meant to be reachable by agents now), but direct still lacks a miner endpoint (S3), the gateway
still needs an API key (S4), and testnet isn't up (S2).

## 5. Identity: did:key, chain accounts, faucet, signed requests

What FLOP says (`testnet-and-timeline`, updated 2026-10-30): an agent needs "an identity key (the
same DID key used on TechnoCore), the ability to make HTTP requests, and the ability to call the
testnet faucet and submit an inference request signed with that key … Agents draw testnet $FLOP from
a faucet with per-identity limits."

What the published code and chain show:

1. **No published code links a `did:key` to anything.** The string `did:key` does not occur in the
   CLI bundle, the TS SDK `dist/` modules or the Python SDK.
2. **The faucet is not identity-authenticated** (§3.2): it funds an SS58 address with no signature.
   "Per-identity limits" must therefore be enforced by a testnet faucet API that isn't published.
3. **Direct-session requests are signed by an ephemeral per-channel key**, not a long-lived
   identity key (§3.3).
4. **The chain has its own agent identity scheme**: pallet `AgentIdentity` (`register_agent(name,
   agent_type, capabilities, endpoints, metadata_uri, stake_amount)`, storage `DidToAgent`). Its
   DIDs are `did:flop:agent:0x<agent_id>` (6 registered on devnet), not `did:key`.
5. **Mechanically, a did:key can be a chain account.** Our DID's Ed25519 public key is a valid
   `AccountId32` under ed25519 `MultiSignature`, SS58 `5Dhx2pBYBJe7DeWueVRgpQjKMpcva46qGVKyHRo9PwT81wzF`.
   If FLOP's testnet "inference request signed with that key" means an extrinsic (e.g. `force_open`)
   signed by that account, then **the DID key would sign on-chain spends**.

So the testnet DID flow is unpublished, and the most literal reading would make our precious owner
key a spending key. That is the brief's stop condition.

## 6. Spend-bounding primitives (from the live metadata)

- **`FlopSessionKeys`** (real, on-chain): an owner grants a delegate a lifetime `spending_cap`,
  `per_tx_limit`, `daily_cap`, `duration_blocks` expiry and `allowed_pallets` allowlist;
  `revoke_session`, `update_session_limits` (can only lower), circuit breaker. The delegate calls
  `execute_via_session(owner, call, spend_amount, nonce)`. This is exactly the bounded authority the
  brief wants, **but no published tool sends a direct session through it** (the CLI uses it only
  for HTLC chat/proxy), and the Python SDK's wrappers are broken (§2.4).
- **`AgentWallet`**: owner-funded wallet with `per_tx_limit`, `daily_cap`, destination allowlist;
  spends only via `agent_transfer`. Nothing shows a compute-channel escrow can be paid from it.
- **`Proxy`** (stock Substrate): delegation by proxy type, no amount caps.
- **A separate hot account**: its balance is the hard cap. Works with `session run --keystore` today.

## 7. Key design (A chosen 2026-11-01)

Roles:

| Key | Holds | Signs | Storage |
|---|---|---|---|
| Owner (`flopstar.pem`, `flopstar.did`) | identity | Technocore (unchanged); on FLOP **only** what FLOP is shown to require for identity (TBD, §9 S1) | existing encrypted PEM |
| FLOP spend account | ≤ `max_flop_per_day × 3` | channel escrow (`force_open`/`open_channel_with_offer`), recovery calls | new sr25519 encrypted Polkadot JSON keystore, `~/.config/flopstar/flop/spend.json` (0600), passphrase from file/prompt |
| Ephemeral channel keys | nothing | miner HTTP requests | generated per channel by the SDK/CLI, never stored |
| Gateway API key | gateway allowance | gateway requests | 0600 file, read via env var for the subprocess/request only |

Proposed default (**A**): the owner key does nothing on FLOP until FLOP publishes the DID-signed
faucet/inference protocol (§9 S1). The spend account draws from the faucet by SS58 (devnet now,
testnet when live), and its small balance plus `FlopPolicy`'s ledger-backed caps are the bounds.
This works with the published CLI and keeps the precious key out of every spending path. The cost:
if testnet credits only DID-signed requests, this account earns nothing until S1 is answered.

Alternatives, if FLOP confirms the DID account is what must spend on testnet:

- **B, owner + on-chain session cap.** The DID account claims the faucet, then once signs
  `FlopSessionKeys.create_session` granting the spend account a capped, expiring, pallet-limited
  session. The spend account opens channels via `execute_via_session`. The owner key signs only the
  grant and revocations. This needs our own direct-session client (or a CLI change), because no
  published tool routes `force_open` through a session, so it is a multi-day job and won't be ready
  for 3 November.
- **C, DID account spends directly** (`session run` with a keystore of the owner key): the DID key
  signs every escrow. Simplest, but it puts the precious key in a hot path and needs the PEM
  converted into a Polkadot keystore. **Not recommended.**

## 8. Flopstar's routes and what they can record

| Route | How | Verification we can claim | Status from here |
|---|---|---|---|
| direct | pinned CLI subprocess (`session run --json`), spend-account keystore | `sdk_verified` when exit 0 and `verified_turns ≥ 1` (the SDK checks receipts before paying) | blocked: no Node/Docker; no public miner endpoint |
| gateway | `httpx` POST `{gateway}/v1/chat/completions` with a bounded API key | `unverified` unless a receipt is present | blocked: no API key (enrolment needs the CLI) |
| native | none: the Python SDK has no inference-request or DID-signed flow | — | not implementable without S1 |

## 9. Open `SPEC:` questions

- **S1. DID-signed testnet flow.** Is the testnet faucet authenticated by the Technocore did:key? With
  what endpoint, payload, and signature encoding (`<…>|nonce|…` like Technocore, or a Substrate
  extrinsic)? Does "inference request signed with that key" mean the DID account signs the escrow
  extrinsic, an HTTP envelope to the gateway, or an `AgentIdentity` registration? Which account
  earns testnet credit: the DID-derived ed25519 account, or whatever spends?
- **S2. Testnet endpoints and genesis.** Profiles name them but none resolve; genesis policy is
  "pin-before-live". We need the genesis hash to pin, and confirmation the hostnames are final.
- **S3. Miner endpoint discovery.** `session run` needs `--endpoint`. Miners publish no endpoint
  (`/api/miners` has none) and there are 0 offers. How does a public agent find a reachable miner,
  and what is "the gateway's separate miner-pin path" (`send-to-specific-miner`)?
- **S4. Gateway credentials.** How does an agent get a funded, spend-capped API key without the
  unencrypted devnet enrolment wallet? On testnet? Do gateway responses carry a verifiable receipt
  (the `@flop-labs/contracts` schema may say)? What are the units of `pricing.prompt = "0.006"`?
- **S5. CLI version lines.** npm `@flop-labs/cli` is 0.4.0, the container in `releases.json` is
  `v0.1.220`. Are they the same code? Which does FLOP support for testnet `session run`?
- **S6. Settled amount and evidence.** `session run --json` reports neither the settled amount nor
  receipt contents or token IDs. Read `Settled.minerPaid` from chain events by `settle_tx`? Is there
  a CLI flag (or SDK call) that exports the verified receipts/transcript for Touchstone?
- **S7. Faucet limits.** The per-identity draw limit and claim period ("hourly or daily — not yet
  decided").
- **S8. Resolved 2026-11-01** from devnet metadata: `Channels: H256 -> Channel {agent, miner,
  model_hash, measured_root, precision, enclave_key, agent_key, escrow: u128, sla {max_turns,
  max_duration, reattest_every}, nonce, opened_at, last_attested_at, state, transcript_ref,
  weights_pin, locked_compute_consumed}`; `ChannelState = Open | Settling {final_root, claimed_gn,
  challenge_end} | Closed | PendingAck {ack_deadline}`; constant `AckWindow` = 600 blocks.
- **S9. Does `FlopSessionKeys.execute_via_session` admit `ComputeChannel` calls** (`DENIED_PALLETS`
  is not visible in metadata)? Only matters for option B.
- **S10. Devnet agent access.** The docs say both that agents can reach devnet and that devnet
  inference is closed to partners. *2026-11-03:* FLOP's post says devnet is open for builders
  (§4.1); the `network-devnet` page still says admission is administrator-reviewed. Treat devnet as
  open for reads and faucet draws; paid inference still hits S3/S4.

## 10. Touchstone network path (Phase 7)

Touchstone's API stays off the public internet. Expected setup: both servers join one Tailscale
tailnet (or a WireGuard point-to-point link). Touchstone listens only on its tailnet/WireGuard
address, with ACLs allowing only the Flopstar node (tag) to reach that port. Flopstar's
`TOUCHSTONE_API_URL` points at the private address (`100.64.0.0/10`, a tailnet MagicDNS name, or the
WireGuard peer IP). The reporter refuses `http://` URLs whose host is not a private, loopback or
CGNAT address. The bearer token lives in a 0600 file. Request/response bytes are encrypted with the
per-agent streak key before they leave this server, so even a mis-routed request leaks only
ciphertext and hashes.

**Settled 3 Nov 2026 (Touchstone main @ c00367e):** there is no private path. Production publishes
no host ports; the API is reachable only through Caddy at `https://api.touchstone.gold` (TLS), so
`TOUCHSTONE_API_URL` is that URL and `status` will warn that it is public. `GET /v1/healthz`
answered `{"status":"ok"}` from this droplet; `/openapi.json` is 404 in production, so `check`
takes `--spec FILE` (a spec dumped from Touchstone's code). The bearer token is one shared
`API_TOKEN` for the whole API (it can also register agents and approve disputes), delivered out of
band by the operator. Touchstone never holds streak keys: we generate and keep ours.

## 11. Things that worry me

1. `SubstrateClient`'s seed handling (§2.3). Any Python user who passes a mnemonic gets a key
   derived from the public dev phrase.
2. Python SDK wallet/session calls don't match the runtime (§2.4). The SDK claims qualification for
   spec 1014003, so nothing tests these paths against a real chain.
3. Floats in every money path of the Python SDK and in `flop wallet agent create`, with evidence on
   devnet (§2.5).
4. Unencrypted wallets written by `--new-wallet` and `agent credential`, and mnemonics returned in
   plain dicts by `generate_session_keypair`.
5. `utils.sign_message` silently "signs" with a hash when PyNaCl is absent.
6. Spec-version drift. The SDK hard-allowlists runtime versions and raises `RUNTIME_INCOMPATIBLE`
   on anything else. The docs promise forced upgrades during testnet, so each runtime upgrade can
   halt Flopstar until a new `flop-sdk` ships (we pin `==0.2.0`, so we'd need to bump it). Flopstar
   treats that as a kill-switch trip with a clear reason, not a crash loop.
7. `langfuse` is a hard dependency. Harmless unless `init_tracing()` is called, but prompts and
   outputs would go to `cloud.langfuse.com` if anyone enabled it with `LANGFUSE_*` set. Flopstar
   never calls it and its units never set those variables.
8. The gateway's 402 response names a payment `destination` of `5GrwvaEF…5N8F`. Flopstar never pays
   an address taken from a response.
9. Running the pinned CLI needs either the `docker` group for the `flopstar` user (root-equivalent
   on the host) or `npx`, which resolves the CLI's `^`-ranged dependencies at run time (not pinned
   by FLOP's package). Prefer Docker, pinned by digest, on a host with nothing else valuable, or a
   rootless Docker/Podman setup.
   **Memory:** on 2026-11-01 the 1 GB droplet (no swap at the time) hard-reset while an uncapped
   CLI container ran `--version`. It now has a 2 GiB swap file, and every CLI container runs with
   `--memory 640m --memory-swap 1g --pids-limit 256` (`inference.DOCKER_LIMITS`), so a runaway CLI
   is killed inside its cgroup. If the CLI needs more than that, resize the droplet rather than
   raise the cap.
10. `session run` puts the prompt on the command line (it has no stdin or file option). The task
    runner refuses prompts that contain secrets, and our logs record only the prompt's length. But
    other local users can read argv via `ps`, so the droplet should stay single-tenant.

## 12. Implementation status

| Phase | Module | State |
|---|---|---|
| 1 | `flop/config.py`, `flop/amounts.py` | done: per-network profiles, explicit caps (decimal strings, floats refused), SS58 blocklist matched by public key, testnet refuses until its genesis is pinned |
| 2 | `flop/ledger.py`, `flop/policy.py` | done (below) |
| 7 | `flop/touchstone.py` | done (below); schema provisional until `touchstone check` passes against the real service |
| 3 | `flop/client.py`, `flop/wallet.py`, `flop/faucet.py` | done: `status` (live-tested on devnet), spend keystore, faucet draws |
| 4 | `flop/inference.py` | done: direct (CLI) and gateway routes, native refused (S1); CLI parsing tested on **synthetic** fixtures only |
| 5 | `flop/tasks.py`, `deploy/flopstar-flop-run.*` | done |
| 6 | `flop/recovery.py`, `deploy/flopstar-flop-recovery.*` | done |

**Ledger** (`data/flopstar.db`, tables `flop_spend`, `flop_spend_events`, `flop_faucet_draws`). SQLite
triggers make it append-only. Core columns (`ts, network, route, model, max_flop, purpose,
request_sha256`) never change. Result columns (`miner, channel_id, settled_flop, settlement_tx,
response_sha256, receipt_json, verification, touchstone_reported_at`) go from NULL to a value once.
`status` may only change to the status of a newly appended event. Allowed transitions:
prepared→sent|failed, sent→settled|refunded|failed|recovering, failed→recovering,
recovering→refunded|settled|failed.

**Caps** are rolling windows (24 h for spend and faucet draws, 1 h for request count), checked in
the same `BEGIN IMMEDIATE` transaction that inserts the `prepared` row. Spend counts `max_flop` in
full until a row is refunded (0) or settled (`settled_flop`). A request that failed before it was
sent counts 0. Every attempt counts toward the hourly request cap. Transaction fees are not counted
(they're not in `max_flop`).

**FlopPolicy** is the only module that may sign; a test greps the package for signing calls
anywhere else. Its extrinsic allowlist is currently `ComputeChannel.{expire_force_open, timeout,
finalize}` for `recovery`. Each signature goes through: kill-switch check → pinned-genesis check
against `chain_getBlockHash(0)` → compose/sign/submit with `wait_for_inclusion` → success required.
Any failure, any cap or allowlist breach, and any recovery request for a channel not in our ledger
appends a reason to `data/KILL-flop`. The `check_*` methods are pure (dry runs and pre-checks never
trip it). Direct-route signatures made by the CLI subprocess are logged with `log_cli_signature`.

**Touchstone reporter.** Settings: `TOUCHSTONE_API_URL`, `TOUCHSTONE_API_TOKEN_FILE` and
`TOUCHSTONE_STREAK_KEY_FILE`. Both files must be owner-only (no group/other bits) and owned by the
running user. A raw `TOUCHSTONE_API_TOKEN` env var is refused. Commands: `flopstar flop touchstone
status | register [--post] [--chain-account SS58] | check | replay [--post]`.

- Plain `http://` is sent only if the host resolves solely to 10/8, 172.16/12, 192.168/16,
  100.64/10 (Tailscale), loopback or fc00::/7. Python's `is_private` is deliberately not used,
  because it also admits documentation and reserved ranges. Redirects are never followed. Public
  `https://` works but warns.
- `report()` never raises. It replays the buffer oldest-first, stops at the first failure, and
  queues the new record behind any backlog. Only a 2xx marks `touchstone_reported_at`.
- `client_digest` is Touchstone's SDK digest (below), identical for every rebuild of a turn even
  though each encryption uses a fresh nonce.
- Turns carry exactly Touchstone's `TurnRecord` fields (below); nothing extra.

Touchstone-side `SPEC:` items (to settle with whoever is updating Touchstone):

Answers from Touchstone's code (3 Nov, main @ c00367e):
- Registration body is `{did, account_ss58, label}` (fixed in `AGENT_FIELDS`); a repeat returns
  400 "already exists", which `register` treats as success. Register the **spend** account.
- Touchstone stores `request_enc`/`response_enc` opaquely and never decrypts. **Adopted** its
  `aes-256-gcm/v1` (touchstone_streak/crypto.py, streak-bridge/src/streak-key.ts): each field is
  base64 of `0x01 ‖ nonce(12) ‖ ct ‖ tag(16)`, AAD = UTF-8 `agent_did + "\n" + client_digest`
  (newline, not `|`), same AAD for request and response (a swap shows in the sha256 fields, which we
  always send). We send `enc_scheme: "aes-256-gcm/v1"` explicitly. The streak key file is 32 bytes
  as base64 (hex/raw still accepted); Touchstone never holds it.
- `client_digest` is Touchstone's SDK digest: sha256 hex of
  `route|channel_id or ""|turn_index or ""|sha256hex(request)`. Because it dedupes on this, gateway
  turns (no channel) use the ledger spend id as `turn_index`; direct turns use 0 on their own channel.
  (We render a `None` index as `""` and 0 as `"0"`.)
- Extra fields (`schema_version`, `miner`) are accepted and dropped. Null `channel_id` is fine for
  route `gateway`/`other`; route `direct` requires `channel_id` and `turn_index` (else 422).
- Dedupes on `client_digest` (64 lowercase hex): a duplicate returns 200 `{"id", "recorded": false}`.
  An unregistered DID returns 404 "Agent not found"; the buffer resends later.
- Spec: Touchstone keeps the generated spec at `web/openapi.json` (CI keeps it current). A copy at
  c00367e is `tests/fixtures/touchstone-openapi-c00367e.json`; `check --spec` and a test diff our
  payloads against it: **0 mismatches** (3 Nov). To get there, turns now send exactly
  Touchstone's `TurnRecord` fields: `timings` is `{str: int}` (ISO `started_at` → `started_at_ms`),
  `sdk_verification` is `{ok, error_kind?}` or null (null for unverified gateway turns), the SDK
  version goes in `sdk: {name: "flop-sdk", version}`. `model`, `miner`, `network`, `cli_version`
  and `schema_version` are no longer sent (Touchstone would drop them; our ledger keeps them).
  Not sent for lack of a value: `model_identity` (needs registry hashes), `settlement_tx`.

Original questions:

- **T1. Streak encryption** (superseded by v1 above). Was provisional: AES-256-GCM, 12-byte random nonce, base64 of
  `nonce‖ciphertext‖tag`, with associated data `flopstar.touchstone.v0|<agent_did>|<channel_id or
  "">|<turn_index>|request|response`. The key file holds 64 hex characters or 32 raw bytes.
- **T2.** Does Touchstone accept our extra `schema_version` field, and a null `channel_id`/`miner`
  for gateway turns?
- **T3.** Which `chain_account` to register: under design A, the spend account. Should the
  DID-derived ed25519 account also be listed?
- **T4.** Idempotency: we assume Touchstone dedupes on `client_digest` and returns 2xx for a
  duplicate (replays can resend after a crash between send and buffer rewrite).

### Phases 3–6

**Status and wallet.** `flopstar flop status` reports chain identity against the pin, the runtime
spec, `FlopClient.health()`, our DID, the DID-derived identity account (unused under A), the spend
account and its balance, caps, budget and the kill switch. `wallet setup --post` generates a fresh
sr25519 key into an encrypted Polkadot JSON keystore (v3: scrypt + xsalsa20-poly1305, the format
`flop --keystore` reads). The keystore is mode 0600 in a 0700 directory and never overwritten; the
passphrase is ≥ 12 characters, from `FLOPSTAR_FLOP_PASSPHRASE_FILE` (0600) or a prompt. `wallet fund`
checks the 3 × daily cap and then refuses: under design A the owner key funds nothing.

**Faucet.** `faucet --post` requires the faucet's `/chain` genesis to equal both the pin and the RPC's
genesis before drawing, then `POST /fund {address}` for the spend account. Response numbers parse as
`Decimal`. 4xx/5xx/network failures are recorded as failed draws (they don't use the daily
allowance and don't trip). A genesis mismatch or a malformed success trips the switch.

**Inference.** One `plan()` (dry run: local state only) and one `execute()`; `ask` and the task runner
both use them and refuse up front when `plan` reports problems, so routine budget exhaustion never
trips the switch. Direct route:
- The miner comes from `[<net>.miners]` (SS58 → HTTPS endpoint; S3), skipping the blocklist and
  preferring the least recently used.
- The CLI runs from the pinned image digest (`FLOPSTAR_FLOP_CLI=docker`) or `npx
  @flop-labs/cli@<version>`, only via `FlopPolicy.run_signing_cli`, with a minimal environment
  (`PATH`, `HOME`, `FLOP_KEY_PASSWORD`, `FLOP_INDEXER_URL`). The logged argv has the prompt replaced
  by its length.
- The ledger row moves to `sent` before the CLI starts. Output is parsed strictly and unknown fields
  are refused. `sdk_verified` requires exit 0, `verified_turns ≥ 1` and the requested miner (by
  public key).
- A failure records the channel ID parsed from the error message. "on-chain recovery failed", an
  unverified receipt, a miner mismatch or malformed output trips the switch.
- `settled_flop` stays NULL (S6), so the cap keeps counting the full `max_flop`.

Gateway route: bearer key from `FLOPSTAR_FLOP_GATEWAY_KEY_FILE` (0600), strict response check,
always `unverified`. A 402 is a failure; we never pay an address taken from a response. Settled
results go to Touchstone (if configured) with the exact request bytes and the CLI's stdout (direct)
or HTTP body (gateway) as the response bytes.

**Tasks** (`flop run`). Each task spends only when it has real input:
- `contest-digest` needs referee messages stored in the last day.
- `ops-review` needs a kill-switch trip, CLI error or failed request in the last day.
- `research-notes` needs unanswered questions in `data/research-queue.txt` (at most 3 per run).

Every prompt is refused if it contains a PEM private key, a Polkadot keystore, a valid BIP-39
mnemonic, or the contents of any configured secret file. Untrusted input is fenced as data. Outputs
go to `data/digests/`, `data/reports/` and `data/research/` under a "model output is untrusted"
banner. The runner stops at the first refusal or failure.

**Recovery** (`flop recovery`) reads our ledger channels at the finalized head:
- `PendingAck` past `ack_deadline` → `expire_force_open`
- `Open` past `opened_at + max_duration` → `timeout`
- `Settling` past `challenge_end` → `finalize`

If a run died before learning its channel, recovery scans `Channels` for ones paid by the spend
account. It attaches an orphan to a ledger row only when exactly one of each exists. A channel in our
ledger paid by another account is a mismatch and trips the switch. With `--post`, the calls go
through `FlopPolicy.submit_extrinsic` and the ledger records recovering → refunded / settled.
Without it, the command prints the call and deadline (`flop channel finalize <id>` for finalize; the
CLI has no expire/timeout command).

### New open questions

- **S11.** Is `computeChannel.timeout` valid for an `Open` channel that has delivered output? The
  pallet doc says "posted no settlement"; we follow the SDK (`opened_at + max_duration`).
- **S12.** After an SDK-side refund, `session run` exits non-zero but the escrow is back. The ledger
  keeps the row `failed` (counted at `max_flop`) because the CLI doesn't say which outcome happened.
  It needs the `Settled`/`TimedOutRefunded` event by tx (S6).

### Going live (testnet, 3 November): commands in order, all dry runs first

Prerequisites outside this repo:
- Docker (or Node 22+) on the droplet.
- FLOP's testnet genesis hash, HTTPS RPC URL and CLI build (S2/S5).
- At least one testnet miner with a public session endpoint (S3).
- `/etc/flopstar/flop.toml` from `deploy/flop.example.toml`, with `[testnet]` filled in.

```bash
export FLOPSTAR_FLOP_NETWORK=testnet FLOPSTAR_FLOP_CONFIG=/etc/flopstar/flop.toml
flopstar flop config                         # genesis pinned? caps and miners as intended?
flopstar flop status                         # genesis "matches pin", spec supported, healthy
flopstar flop wallet setup                   # review, then:  wallet setup --post
flopstar flop faucet                         # review, then:  faucet --post
flopstar flop status                         # spend balance arrived
flopstar flop ask --model <m> --prompt 'Reply OK.' --purpose research-notes --max-flop 0.01
                                             # review plan, then add --post (one request)
flopstar flop recovery                       # nothing due
flopstar flop touchstone status && flopstar flop touchstone check --spec openapi.json
flopstar flop touchstone register --chain-account <spend SS58>   # then --post
flopstar flop run                            # review the plan, then enable the timers
```

