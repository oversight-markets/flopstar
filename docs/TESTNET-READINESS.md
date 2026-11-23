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

*Progress since the assessment:* blockers 1 (the SDK pin) and 2 (the CLI image) were fixed in code
on 22 Nov, then again on 23 Nov (SDK 0.3.4, CLI v0.1.455, both run on the droplet). **23 Nov, rechecked on the droplet:** `main` (`d475e64`) is deployed to `/opt/flopstar`.
No flopstar services or timers are loaded, and `KILL-flop` is absent. Devnet had already moved on
to runtime **1014034**, which 0.3.3 refuses, so the SDK now pins 0.3.4 (blocker 1). FLOP also
published a newer CLI image overnight; it is now pinned and run on the droplet (blocker 2).

## Blocking issues

### 1. ~~The pinned FLOP SDK is too old for the chain~~ Done again for runtime 1014034

**23 Nov: devnet runs 1014034; bumped to `flop-sdk==0.3.4`.** A read-only `flop status` from
`/opt/flopstar` on 0.3.3 failed with `Unsupported runtime spec version 1014034`. `flop-sdk` 0.3.4
(PyPI, 23 Nov 05:51 UTC) lists 1014034 in `SUPPORTED_SPEC_VERSIONS` (the range is now
`1013000..1014034`; 1014032–1014033 aren't admitted). Its diff from 0.3.3 is only the version
string and that list. Tests pass (260 passed; ruff clean). `flop status` against devnet: genesis
matches the pin, spec 1014034, healthy. `min_escrow` (`ComputeChannel.MinForceOpenEscrowForFailedAck`)
re-read at 1014034: still 0.05 FLOP. Expect more bumps like this: devnet moved twice in two days.

**Done 22 Nov (`2651e25`):** `main` pins `flop-sdk==0.3.3`. Tests pass (253 passed, 5 skipped;
ruff clean) and a read-only `flopstar flop status` against devnet now succeeds (genesis matches
pin, spec 1014031, healthy). Every `flop_sdk` name Flopstar imports still exists. 0.3.3 also fixes
the seed hazard in FLOP.md §2.3 (it now derives keys properly from mnemonics and hex seeds), which
Flopstar never used. **Still open:** ~~the droplet runs 0.2.0 until it's redeployed~~ (deployed 23 Nov),
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

### 2. ~~The pinned FLOP command-line tool is stale~~ Re-pinned to v0.1.455 and run on the droplet

**23 Nov: re-pinned.** `https://flop.network/releases.json` now names
`ghcr.io/flop-labs/flop-core/cli:v0.1.455@sha256:bddf38047b88c9b699474e090a6edd8345a51f986154c3c70f6eb676afcd06f8`
(minimum still 0.1.172), and npm has `@flop-labs/cli` 0.8.0 and `@flop-labs/sdk` 0.9.0 (both
23 Nov ~06:00 UTC). v0.1.418 refuses devnet: a read-only `balance` from it on the droplet fails
with `RUNTIME_INCOMPATIBLE` (spec 1014034). `flop/config.py` now pins v0.1.455 and
`cli_npm_version` 0.8.0 for both profiles. Checked on the droplet, with both images pulled by
digest:

- The tag resolves to the pinned digest on GHCR. The image is `@flop-labs/cli` **0.8.0**
  (`package.json`), bundling `@flop-labs/sdk` **0.9.0**, whose `SUPPORTED_SPEC_VERSIONS` includes
  1014034 (range `1013000..1014034`).
- `--version` → `0.1.455`. The global `--help` and `session run --help` match v0.1.418 exactly,
  and every flag Flopstar passes is there.
- In `dist/cli.js`, the `session run` success `print({...})` block and its `SESSION_FAILED`
  handler are byte-identical to v0.1.418. The SDK's failure messages (`… (channel 0x…)`,
  "direct session failed; on-chain recovery failed") are unchanged too, so the strict parser and
  `parse_cli_failure` hold. On our path, the ~1,700 changed lines in `cli.js` are bundler renames.
  The real changes are elsewhere (Hermes, `launch` for Claude Code/Codex, gateway keys and
  evidence). The bundled SDK did change (0.7.2 → 0.9.0), and `runDirectSession` lives there. Its
  result field names are unchanged, but its behaviour is untested until a real session runs.
- Run like production (`--memory 640m --memory-swap 1g --pids-limit 256`, `--network host`):
  read-only `health` and `balance` on the spend account work against devnet 1014034
  (`connected: true`; balance 25 FLOP). Peak container memory was 230 MiB (cgroup
  `memory.peak`), well inside 640 MiB on the 1 GB droplet.

**Still open:** replace the synthetic fixtures with real `session run` output. That needs a
devnet session, which needs a miner endpoint (blocker 5) and an owner-run `--post`.

**Done 22 Nov:** `flop/config.py` pins
`ghcr.io/flop-labs/flop-core/cli:v0.1.418@sha256:fd97fab267e47bc69c40a014e562a69480cae7328dd0f139267ef940826fe223`,
the image `https://flop.network/releases.json` names. GHCR confirms the tag resolves to that
digest, and the downloaded layers matched their digests. The image wasn't run (no Docker daemon on
the Mac, droplet unreachable); its source was read statically:

- It is `@flop-labs/cli` **0.7.1** (the image's `package.json`), bundling `@flop-labs/sdk` 0.7.2,
  which accepts runtimes up to **1014031**. So the container tag and the npm version are the same
  code (S5's first half), and `cli_npm_version` is now 0.7.1 for both profiles.
- Every global flag and `session run` flag Flopstar passes still exists with the same meaning.
- `session run --json` prints the same fields (`reply`, `miner`, `channel`, `settle_tx`,
  `cooperative`, `verified_turns`, `finish_reason`, `completion_tokens`; `wallet` only with
  `--new-wallet`), so the strict parser holds.
- Failures still end the message with `(channel 0x…)` and still say "on-chain recovery failed",
  so `parse_cli_failure` holds. The stderr JSON also gains `channel`, `recovery` and
  `diagnostics` fields, which the parser ignores.
- **New:** an operator release check. Before any command that isn't read-only (including
  `session run`), the CLI fetches `releases.json` and refuses to run if the fetch fails or the CLI
  is below FLOP's published minimum (now 0.1.172). Our containers are `--rm`, so its one-day cache
  never persists and every run fetches. It fails before any escrow is locked; `--offline` would
  skip it, but Flopstar keeps the check.

**Still open (22 Nov):** pull and run the image on the droplet within the caps. Superseded by the
23 Nov re-pin above.

The original finding: the direct route ran `ghcr.io/flop-labs/flop-core/cli:v0.1.220@sha256:c8f3…bbbd`
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
| Gateway price for `qwen/qwen2.5-0.5b-instruct` dropped from `0.006` to `0.000001` per token (~6,000×) | Every request is now sized at the `min_escrow` floor. **Done 22 Nov:** `min_escrow` re-read at runtime 1014031, unchanged at 0.05 FLOP; the price tests now use the live price and catalog shape (step 4). |
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

1. ~~Restore SSH access to the droplet. Confirm the services and timers are still stopped and
   `KILL-flop` is absent.~~ **23 Nov:** checked from a session on the droplet itself (SSH from the
   Mac not re-tested). Services and timers are stopped, and `KILL-flop` is absent.
2. ~~Bump `flop-sdk` to 0.3.3, run the tests, and confirm `flop status` works against devnet
   1014031.~~ Done 22 Nov (`2651e25`). **23 Nov:** devnet moved to 1014034; bumped to 0.3.4,
   tests pass, devnet status healthy.
3. ~~Pin a current CLI image by digest and check that it runs within the container caps
   (`--memory 640m --memory-swap 1g --pids-limit 256`) on the 1 GB droplet.~~ **23 Nov:** re-pinned
   to `v0.1.455` (v0.1.418 refuses runtime 1014034); read-only `health`/`balance` run inside the
   caps on the droplet, peaking at 230 MiB. **Still open:** replace the synthetic fixtures with
   real `session run` output (needs a miner, blocker 5).
4. ~~Re-read `min_escrow` from the new runtime and update the price tests.~~ Done 22 Nov:
   unchanged at 0.05 FLOP; `estimate.py` accepts the live catalog (new per-G_n fields ignored).
   **Owner decision:** at 0.000001 FLOP/token every ask escrows the 0.05 floor, ~500× its
   catalog cost if a settle still pays the whole escrow. Keep the floor (a miner who doesn't
   answer is held to account), or size below it on devnet (FLOP.md §12)?
5. Back up the spend keystore and passphrase.
6. Decide open question S13: should refunded requests count 0 against the daily cap
   (FLOP.md §12)?
7. ~~Deploy to `/opt/flopstar` (HANDOFF §7) and re-run `flop status` there.~~ `d475e64` deployed
   23 Nov (venv synced). Its status failed on runtime 1014034, which led to the 0.3.4 bump;
   redeploy the bump and re-run status. Repeat after every SDK or CLI re-pin.

### On launch day

Follow the go-live sequence at the end of [FLOP.md §12](FLOP.md): dry run first at every step,
each `--post` run by the owner.

If testnet launches without published miner endpoints or a gateway key route, the realistic goal
for launch day is: chain status healthy, a faucet draw done, and the agent parked again.

## Sources checked

- `gh release list -R flop-labs/network` (releases of 18 Nov: `cli/v0.5.0`, `sdk/v0.6.0`,
  `python-sdk/v0.3.0`, `node/v0.2.0`, …)
- PyPI `flop-sdk` (latest 0.3.3 on 22 Nov; 0.3.4 on 23 Nov); npm `@flop-labs/sdk` 0.8.0, `@flop-labs/cli` 0.7.2,
  `@flop-labs/client` 0.2.2
- `https://rpc.devnet.flop.network`: `state_getRuntimeVersion`, `chain_getBlockHash(0)`
- `https://api.flop.network/v1/models`
- `https://explorer.devnet.flop.network/api/session-offers`
- DNS for the testnet hostnames
- `https://docs.flop.network/docs/network-testnet`
- 23 Nov: `flopstar flop status` from `/opt/flopstar` on the droplet (read-only, the run unit's
  environment); the `flop-sdk` 0.3.4 wheel and its diff from 0.3.3; `https://flop.network/releases.json`;
  npm `@flop-labs/cli` and `@flop-labs/sdk`; `ComputeChannel.MinForceOpenEscrowForFailedAck` on
  `wss://rpc.devnet.flop.network`; GHCR manifest for `cli:v0.1.455`; images `cli:v0.1.418` and
  `cli:v0.1.455` pulled by digest on the droplet (`--version`, `--help`, `dist/cli.js`, bundled SDK
  `runtime-compatibility.js`, read-only `health`/`balance` inside the caps)
