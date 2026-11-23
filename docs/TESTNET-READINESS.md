# FLOP testnet readiness assessment

**Assessed**: 22 November 2026, two days before FLOP's testnet launch
**Scope**: the FLOP agent (`src/flopstar/flop/`); background in [HANDOFF.md §10](HANDOFF.md) and
[FLOP.md](FLOP.md)
**Method**: local test suite, read-only probes of FLOP's public endpoints and releases, and a
read-only `flopstar flop status` against devnet (local throwaway data dir, no keys). Nothing was
signed or posted.

## Verdict

**Not ready.** The code, safety rails and Touchstone side are done. But FLOP has shipped a lot
since the agent was parked on 4 Nov, and the pinned versions no longer work against FLOP's chain.
The droplet also couldn't be reached over SSH, and testnet itself is still unpublished.

*Progress since the assessment:* blocker 1 (the SDK pin) is fixed in code (22 Nov), not yet
deployed to the droplet.

## Blocking issues

### 1. ~~The pinned FLOP SDK is too old for the chain~~ Done in code, not yet deployed

**Done 22 Nov (`2651e25`):** `main` pins `flop-sdk==0.3.3`. Tests pass (253 passed, 5 skipped;
ruff clean) and a read-only `flopstar flop status` against devnet now succeeds (genesis matches
pin, spec 1014031, healthy). Every `flop_sdk` name Flopstar imports still exists. 0.3.3 also fixes
the seed hazard in FLOP.md §2.3 (it now derives keys properly from mnemonics and hex seeds), which
Flopstar never used. **Still open:** the droplet runs 0.2.0 until it's redeployed (plan step 7),
and 0.3.3's new `x402.py` module hasn't been checked against the "no native route" conclusion.

The original finding: we pinned `flop-sdk==0.2.0` (`pyproject.toml`), which only accepts runtime spec versions up to
1014011 (`SUPPORTED_SPEC_VERSION_RANGE = (1_013_000, 1_014_011)`). Devnet now runs **1014031**
(same genesis `0x99c1…d631`). A read-only `flopstar flop status` against devnet fails:

```
chain: unavailable: FLOPError: Unsupported runtime spec version 1014031; qualified spec versions
are (1013000, …, 1014011). Upgrade flop-sdk to a release supporting this runtime …
```

Testnet will launch on this runtime or a newer one, so as things stand nothing would work.

**Fix:** bump to `flop-sdk` 0.3.3 (PyPI, 20 Nov), whose range ends at 1014031. Its 0.3.0 changes
session creation (`per_tx_limit` and `daily_cap` become required keyword arguments); Flopstar
doesn't use that path under key design A, but the workarounds in FLOP.md §2.3–2.5 need
re-checking.

### 2. The pinned FLOP command-line tool is stale

The direct route runs `ghcr.io/flop-labs/flop-core/cli:v0.1.220@sha256:c8f3…bbbd`
(`flop/config.py`). Current releases are `cli/v0.5.0` on GitHub and `@flop-labs/cli` 0.7.2 on
npm (20 Nov). The matching TS SDK 0.6.0 adds support for runtimes 1014015–1014020 and fixes
faucet retries and unknown settlement outcomes.

Not verified: whether v0.1.220 refuses the new runtime. The bigger risk is our output parser,
which rejects unknown fields and trips the kill switch on malformed output. A newer tool needs a
new pinned image digest and a re-check of the parser against its real output (the current
fixtures are synthetic).

### 3. The droplet couldn't be reached

SSH to `flopstar` (68.183.21.81) opens the TCP connection but then fails with "timed out during
banner exchange" and, on retry, "Connection reset by peer". The services, timers, deployed commit,
kill-switch file (`/var/lib/flopstar/data/KILL-flop`) and spend keystore couldn't be checked. The
droplet holds the spend key, whose passphrase credential decrypts only there (HANDOFF §8.3), so
this is the first thing to fix.

### 4. Testnet details are unpublished

- `testnet.flop.network`, `rpc.testnet…`, `gateway.testnet…`, `faucet.testnet…` and
  `explorer.testnet…` don't resolve.
- FLOP's docs page `network-testnet` says: "Status: planned. Unavailable until published network
  identity, artifacts, and admission policy are verified."
- No genesis hash, RPC URL, supported release or admission policy is published (open questions
  S2, S5).

Flopstar's config refuses testnet until its genesis is pinned. That's intended, but it means the
genesis, RPC URL, `min_escrow`, miners and models all have to be filled in on launch day.

### 5. No miner to send requests to

- **Direct route (S3):** no miner publishes a session endpoint. The devnet explorer's
  `/api/session-offers` currently returns `{"error":"Writer unavailable"}`.
- **Gateway route (S4):** still no way to get a funded, spend-capped API key without FLOP's
  unencrypted enrolment wallet.

Even with everything else fixed, the agent can't buy inference until one of these routes opens.

## Changed since 4 Nov, needs updating

| Change | Effect |
|---|---|
| Gateway price for `qwen/qwen2.5-0.5b-instruct` dropped from `0.006` to `0.000001` per token (~6,000×) | Every request is now sized at the `min_escrow` floor. `min_escrow` (0.05 FLOP) was read from runtime 1014003 and must be re-read from the new runtime; tests that assume the old price need updating. |
| Two new models: `qwen/qwen3-14b` (`0.000015`), `qwen/qwen3-coder-30b-a3b-instruct-fp8` (`0.0000035`) | `allowed_models` only lists qwen2.5-0.5b. Fine unless the others are wanted. |
| TS SDK 0.6.0: a successful faucet response can't be bound to the grant transaction | Matches our draw #4 (HANDOFF §10). Keep treating the chain balance as the truth. |
| FLOP published versioned `node`, `miner`, `miner-worker`, `da-validator` images (`v0.2.0`, 18 Nov) | More miners may come up for testnet, which could help with item 5. |

## Ready

- **Code:** phases 0–7 done in `src/flopstar/flop/`. Locally: `uv run pytest -q` → 253 passed,
  5 skipped; `uv run ruff check` → clean.
- **Safety design:** key design A (a separate, capped spend account; the owner key does nothing on
  FLOP), explicit caps, append-only ledger, kill switch, and testnet refuses to run until its
  genesis is pinned.
- **Touchstone:** Flopstar is registered (3 Nov), and our payloads matched its API spec at
  c00367e (0 mismatches).
- **Spend account:** `5C9zWSykX2EHGF6pbRkPGTHrjYcnj2WjQRnm2xbPfye4uUFb` held 25 devnet FLOP as of
  3 Nov (not re-checked). The same keystore can be used on testnet, but it needs its own faucet
  draw there.

## Plan

### Before launch

1. Restore SSH access to the droplet. Confirm the services and timers are still stopped and
   `KILL-flop` is absent.
2. ~~Bump `flop-sdk` to 0.3.3, run the tests, and confirm `flop status` works against devnet
   1014031.~~ Done 22 Nov (`2651e25`); deploys with step 7.
3. Pin a current CLI image by digest, check that it runs within the container caps
   (`--memory 640m --memory-swap 1g --pids-limit 256`) on the 1 GB droplet, and update the parser
   and test fixtures to its real output.
4. Re-read `min_escrow` from the new runtime and update the price tests.
5. Back up the spend keystore and passphrase.
6. Decide open question S13: should refunded requests count 0 against the daily cap
   (FLOP.md §12)?
7. Deploy to `/opt/flopstar` (HANDOFF §7) and re-run `flop status` there.

### On launch day

Follow the go-live sequence at the end of [FLOP.md §12](FLOP.md): dry run first at every step,
each `--post` run by the owner.

If testnet launches without published miner endpoints or a gateway key route, the realistic goal
for launch day is: chain status healthy, a faucet draw done, and the agent parked again.

## Sources checked

- `gh release list -R flop-labs/network` (releases of 18 Nov: `cli/v0.5.0`, `sdk/v0.6.0`,
  `python-sdk/v0.3.0`, `node/v0.2.0`, …)
- PyPI `flop-sdk` (latest 0.3.3); npm `@flop-labs/sdk` 0.8.0, `@flop-labs/cli` 0.7.2,
  `@flop-labs/client` 0.2.2
- `https://rpc.devnet.flop.network`: `state_getRuntimeVersion`, `chain_getBlockHash(0)`
- `https://api.flop.network/v1/models`
- `https://explorer.devnet.flop.network/api/session-offers`
- DNS for the testnet hostnames
- `https://docs.flop.network/docs/network-testnet`
