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

## Launch day (24 Nov, checked 13:25 UTC): blocked on FLOP

FLOP's testnet page (`https://flop.network/testnet`) says "FLOP Testnet is live! This is a very
early release. We'll be activating features over the coming hours." Read-only checks:

| Item | State |
|---|---|
| RPC | `wss://testnet.flop.network/` published; `https://testnet.flop.network` also answers JSON-RPC. 6 peers, not syncing. |
| Genesis | `0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800`: the page and `chain_getBlockHash(0)` agree. **Pinned** in `flop/config.py`. |
| Runtime | `flop-testnet` spec **1014035**. **17:15 UTC: `flop-sdk` 0.3.5 (PyPI, 24 Nov 15:50) admits 1014035; pinned.** Its diff from 0.3.4 is the version string, the list entry and a comment: testnet runs the flop-testnet build of 1014035 (61 pallets: devnet's minus PaidStorage and the voucher pallets 150–155, none of which Flopstar uses). npm now has `@flop-labs/sdk` 0.10.0 and `@flop-labs/cli` 0.8.2 (both ~15:50), and GHCR has CLI images up to v0.1.470, but `releases.json` still names v0.1.455 (bundles sdk 0.9.0, which refuses 1014035), so the CLI is not re-pinned yet. |
| `min_escrow` | 0.05 FLOP (`ComputeChannel.MinForceOpenEscrowForFailedAck`, read via substrate-interface). Pinned. |
| Faucet | Not published. `faucet.flop.network` still serves **devnet**; `faucet.testnet.flop.network` doesn't resolve. |
| Gateway, miners, offer indexer | Not published (S3/S4 unchanged). |
| Explorer | `https://testnet.flopscan.io/` (FLOPScan). |
| Scoring | The opening is "unscored"; agents are credited for "compute purchased in settled sessions" in the later scored phase. Two rules affect us: wallets under common control count as one participant, and spend routed to a miner under common control isn't credited (our `same_control_miners` blocklist). |

**17:15 UTC: both RPC endpoints (https and wss) answer 429 "Too many connections"**, so `flop status` on
0.3.5 hasn't been confirmed against the chain yet.

**18:44 UTC recheck: unchanged.** RPC still 429; `releases.json` still v0.1.455; PyPI 0.3.5, npm sdk
0.10.0 / cli 0.8.2; `faucet.flop.network` still devnet. `flop.network/testnet` now redirects to
`flop.finance/testnet/`, with the same text and no faucet, gateway or miner links.

**21:43 UTC recheck: unchanged.** RPC still 429; `releases.json` still v0.1.455 (GHCR now up to
v0.1.470); PyPI 0.3.5, npm sdk 0.10.0 / cli 0.8.2; `faucet.flop.network` still devnet and
`faucet.testnet`/`gateway.testnet`/`rpc.testnet.flop.network` don't resolve. FLOPScan's page loads
but its API (`/api/stats`, `/api/blocks`) returns 503 "Era-T reader unavailable". The testnet page
now says six founding validators produce and finalize blocks, the first 50 validator seats are
open, and history and balances may be reset.

**Next, as FLOP turns features on:** (1) ~~re-pin flop-sdk~~ (0.3.5, done) and the CLI image once `releases.json` names a release accepting
1014035, using the re-pin check from blocker 2; (2) ~~pin the faucet URL once it's published~~ (25 Nov: `faucet.flop.network`, see "Day 2, second
update"), then run `wallet setup` and `faucet`, each dry run first and then `--post` by the owner; (3) set
`allowed_models` and `[testnet.miners]` once a route exists.

## Day 2 update (FLOP Labs, 25 Nov)

FLOP Labs' day-2 status post, as the owner relayed it (verbatim):

> Flop Network testnet, day 2.
>
> Up
> - Six founding validators finalize about one block per second
> - Public endpoint and bootnodes are up
> - Validator registration is open for the first 50 seats. Target after that: 25% daily growth
> - Testnet docs and validator prep kit: http://docs.flop.network
> - Devnet stays up. Keep what you run there online
>
> Down
> - FLOPScan is unreliable while we fix it. If it looks stuck, use the public endpoint
>
> Next
> - Signed validator kit and testnet miner pack. New validators and miners join after that
> - Testnet faucet, not live yet
> - Routing changes
> - Voucher earning, then trading
>
> Nothing is scored yet. Validators have a two-week grace period. Miner and agent work counts from
> the voucher start block, to be announced. Nothing carries over from Devnet. Test $ FLOP has no
> value.

What it means for Flopstar (not re-probed; from the post alone):

- **Still blocked on FLOP, now confirmed by FLOP:** no testnet faucet yet, and no testnet miners
  until the "testnet miner pack" ships (blocker 5). The order in "Next" puts the miner pack first,
  then the faucet, routing changes, and voucher earning. So no testnet ask is possible yet, and
  the go-live steps after `flop status` stay parked.
- **Scoring clock changed:** agent work counts from a **voucher start block, to be announced**,
  not from the end of the 14-day grace period (that grace is now stated for validators only).
  Nothing done before that block counts, so waiting costs nothing. Watch for the block number.
- **Vouchers:** "voucher earning, then trading" matches the voucher pallets (150–155) that the
  flop-testnet 1014035 build omits (see "Launch day", Runtime). Expect a runtime upgrade that adds
  them, which means another `flop-sdk` (and CLI) re-pin before Flopstar works again.
- **"Routing changes"** may change how asks reach miners (gateway vs. direct session). Re-check
  `[testnet.miners]`/`allowed_models` and the session-run path when they land.
- **FLOPScan:** consistent with the 503 "Era-T reader unavailable" seen at 21:43 UTC on 24 Nov.
  Use the public RPC endpoint for chain reads, as FLOP says; Flopstar already does.
- **Devnet stays up**, and FLOP asks people to keep what they run there online. Flopstar runs
  nothing long-lived on devnet (the spend account is idle and the services are stopped), so
  nothing to keep up. Devnet still counts for nothing ("nothing carries over").
- **Docs:** testnet docs and the validator prep kit are at `docs.flop.network`. Read them for the
  faucet URL and miner/routing details once those features go live.

## Day 2, second update (FLOP Labs, 25 Nov; checked 14:15 UTC)

FLOP Labs' post, as the owner relayed it (verbatim):

> What changed since opening:
>
> - Public RPC no longer drops connections under load
> - Testnet faucet is up at http://faucet.flop.network (Devnet moved to http://faucet.devnet.flop.network)
> - flop-sdk 0.3.6 on PyPI fixes agent wallet calls on Testnet
> - Docs corrected: SDK version, miner capacity bond, network status
>
> Next: agent faucet claims and the first Testnet inference. Thanks to everyone who filed reports.
> Keep them coming.

Checked (read-only GETs and a local `flop status`; nothing posted):

- **Faucet hosts swapped.** `https://faucet.flop.network/chain` now reports `network: testnet` and
  genesis `0x918a…d800` (matches the pin); `https://faucet.devnet.flop.network/chain` reports
  devnet `0x99c1…d631`. Both also answer over HTTPS, so Flopstar keeps HTTPS. Until this commit
  the **devnet profile pointed at what is now the testnet faucet.** A devnet draw would have
  failed the `/chain` genesis check and tripped `KILL-flop`. Nothing draws on a timer, so this
  never happened. Both profiles are now updated in `flop/config.py`, and
  `/etc/flopstar/flop.toml` sets no `faucet_url` override.
- **Testnet faucet rules** (`/limits`): **anonymous** grant 25 FLOP, 1 per address per 24 h, and
  3 grants per IP per day (devnet allows 300). **Linked** (verified human) 500–2,000 FLOP. **Agent**
  grant 10 FLOP at `POST /v1/testnet/agent-claims`, 10 FLOP rolling 24 h cap, `rolloverRequiresSpend:
  true`. 3,820 grants were pending at 14:15 UTC, so expect a delay like devnet draw #4. `/health`
  shows faucet image 0.1.484, and its `technocoreBot` names DID
  `did:key:z6MktjwdcBkwC6xo9wi4KY4ckWvn1GQHdQ1qzkaZtWqHAoxs` in room `flop-testnet-faucet` (the
  chat route; Flopstar doesn't use it).
- **Agent claims aren't implemented yet.** The request format isn't published (`GET` on the claim
  path is 404, and the docs pages are JS-rendered). Neither `flop-sdk` 0.3.6 nor npm
  `@flop-labs/sdk` 0.11.0 mentions `agent-claims`. That matches "Next: agent faucet claims".
  Flopstar's `faucet` uses the anonymous `POST /fund` route (SPEC S1 anticipated a DID-signed
  agent route). Since the airdrop counts what the agent *does*, the agent route is probably the
  one that counts once it ships. Re-check then; the 25 FLOP anonymous grant is enough to fund
  asks before that.
- **flop-sdk 0.3.6: pinned.** Diff from 0.3.5: `AgentWallet` call names in snake_case
  (`create_wallet`, `fund_wallet`, …), wallet balances read from `AgentWallet.WalletFunds`,
  storage reads raise instead of silently returning `None`/0, `get_balance`/`get_account_info`
  decode `System.Account` via `state_getStorage`, and RPCs that nodes don't expose now raise
  `RPC_METHOD_UNAVAILABLE`. Flopstar uses only `FlopClient.health()` and
  `assert_supported_runtime`, both unchanged; the supported range still ends at 1014035. 261
  tests pass; ruff clean.
- **RPC fixed:** local `flop status` on 0.3.6 against testnet now succeeds: genesis matches, spec
  1014035, 18 peers, finalized block 91,020. It returned 429 on 24 Nov, so this is the first
  confirmed testnet status. Devnet is healthy too (spec 1014034).
- **CLI still blocks asks:** `releases.json` still names v0.1.455 (bundles sdk 0.9.0, which refuses
  1014035). npm now has `@flop-labs/sdk` 0.11.0 and `@flop-labs/cli` 0.8.3 (25 Nov ~05:30 UTC).
  "First Testnet inference" is FLOP's next step, so expect a `releases.json` bump; re-pin with the
  blocker-2 check when it lands.

Next for Flopstar: deploy this commit to `/opt/flopstar` and re-run `flop status` there. When the
owner chooses, set up the testnet spend wallet and take the anonymous draw (`flop wallet setup`,
then `flop faucet`, dry run first, `--post` by the owner). Then wait for the agent-claim format,
a CLI release for 1014035, and a miner route.

## Launch announcement (FLOP Labs, 23 Nov)

FLOP Labs' last post before launch, as the owner relayed it (verbatim):

> Flop Network testnet goes live tomorrow, Tuesday Nov 24, at 07:00 UTC.
>
> Got an agent? There's no approval and no waitlist. It claims testnet $ FLOP from the faucet every
> day, in Technocore chat or with one API call. Spend it on inference. The airdrop is measured on
> what your agent does on testnet, so a wallet full of unspent faucet tokens gets you nowhere.
>
> Miners and validators are sponsored. You don't need to hold any $ FLOP to join, so that excuse is
> gone. If you were on Devnet, nothing carries over and you have to rejoin.
>
> Expect a queue on day one. Miners calibrate in turn and validator seats open in daily batches,
> which is why validators get a 14 day grace period. The ranking for mainnet only starts counting
> once those two weeks are over, so joining on day 3 costs you nothing.
>
> KOLs, we haven't forgotten you, and your program switches on at mainnet.
>
> No links today. Setup pages and the Explorer go out at launch.

What it means for Flopstar:

- **Timing:** live **Tue 24 Nov 07:00 UTC**. Genesis, RPC, faucet and explorer URLs only appear
  then ("no links today"), so blocker 4 can't be cleared before launch. Pin them from the
  published setup pages, not from guesses.
- **Admission:** "no approval and no waitlist" for agents answers part of S2 (admission policy).
- **Faucet:** a daily claim, "in Technocore chat or with one API call". Our cap
  `faucet_draws_per_day = 1` already matches. Flopstar draws through the API, never through
  Technocore chat. Treat the chain balance as the truth, as on devnet.
- **Spending is what counts:** the airdrop is measured on what the agent does, not what it holds.
  So the launch-day fallback below ("park again" with a funded wallet) is safe but earns nothing.
  The aim is real asks within the caps once a route opens.
- **Nothing carries over from devnet:** the spend account needs a fresh testnet faucet draw.
  Whether Touchstone's registration (agent 113) needs redoing for testnet is unknown; check at
  launch.
- **Day-one queue:** miners calibrate in turn, so a usable miner endpoint (blocker 5) may lag by a
  day or more. The ranking only starts after the 14-day validator grace period, so a slow start
  costs nothing.

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
   **Owner decision (23 Nov): keep the floor.** At 0.000001 FLOP/token every ask escrows 0.05 FLOP,
   ~500× its catalog cost if a settle still pays the whole escrow. That is accepted so that a
   miner who doesn't answer is held to account (FLOP.md §12). Re-read at 1014034 on 23 Nov:
   still 0.05. No code change; testnet uses whatever its runtime reports.
5. Back up the spend keystore and passphrase.
6. ~~Decide open question S13: should refunded requests count 0 against the daily cap
   (FLOP.md §12)?~~ **Owner decision (23 Nov): count refunded requests at their full escrow.**
   Done in `ledger.spent_since`, with the test covering a direct refund and a recovery refund.
7. ~~Deploy to `/opt/flopstar` (HANDOFF §7) and re-run `flop status` there.~~ `d475e64` deployed
   23 Nov (venv synced). Its status failed on runtime 1014034, which led to the 0.3.4 bump;
   redeploy the bump and re-run status. Repeat after every SDK or CLI re-pin. **23 Nov:** 0.3.4
   and CLI v0.1.455 deployed (`63125b4`), and status is healthy against devnet 1014034.

### On launch day

Follow the go-live sequence at the end of [FLOP.md §12](FLOP.md): dry run first at every step,
each `--post` run by the owner.

If testnet launches without published miner endpoints or a gateway key route, the realistic goal
for launch day is: chain status healthy, a faucet draw done, and the agent parked again. Per the
launch announcement, a funded but idle wallet earns nothing toward the airdrop, so take up the
first route that opens. With the 14-day grace period, a few days' delay costs nothing.

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
