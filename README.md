# Flopstar

Oversight's agent for the FLOP ecosystem: it played FLOP Labs' Close Call contest (`close-1`,
concluded) and is now being built to buy verifiable inference on the FLOP Network.

## Overview

Flopstar is a Python agent with a did:key identity, policy-gated signing, read-only monitoring
and append-only evidence logs. It has two jobs:

1. **Close Call contest (`close-1`, 25 Sep – 4 Oct 2026, concluded).** A bet on NVIDIA's price on
   Hyperliquid at a fixed moment, played on technocore.chat with a read-only referee monitor, its
   own room `d-flopstar-close1` and a 64-key tree trader. Trading locked on 4 Oct; Flopstar didn't
   place (best keys +312.25 against about +1,340 for 3rd). The statement naming the tree keys is in
   [docs/STATEMENT.md](docs/STATEMENT.md), and every contest service is stopped.
2. **FLOP agent (Nov 2026, parked for testnet).** Buys useful inference on the FLOP Network, keeps
   a verifiable record of every purchase and reports it to Touchstone, Oversight's verification
   service. It is built and deployed against devnet, and waits for FLOP's testnet (target
   3 Nov 2026) before spending anything. See [FLOP Agent](#flop-agent).

Current operational state for both: [docs/HANDOFF.md](docs/HANDOFF.md) (§1 for the contest, §10
for the FLOP agent).

## Security First

**Critical**: The private key (`flopstar.pem`) is stored **outside the repository** at `~/.config/flopstar/flopstar.pem` with 0600 permissions. It is never committed to git.

Security measures:
- Key moved to `~/.config/flopstar/` (mode 700)
- Multi-layer .gitignore protection
- Pre-commit hook blocks any file containing "PRIVATE KEY"
- No key is ever committed, even in the initial commit
- On the droplet, passphrases and the tree seed exist only as systemd encrypted credentials
- The FLOP agent never uses the owner key: it pays from a separate, capped spend account whose
  keystore is encrypted and whose passphrase is a systemd credential

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
# Clone the repository
git clone https://github.com/oversight-markets/flopstar.git
cd flopstar

# Install dependencies
uv sync --dev

# Copy environment template
cp .env.example .env
# Edit .env if you need custom paths
```

## Running the Monitor

The read-only monitor tracks the contest referee's data without using your private key.

```bash
# Run the monitor
uv run flopstar monitor
```

### What the Monitor Does

1. **Long-polls** `d-close1-price` for new sweep announcements (10s timeout)
2. **Reads** other referee rooms (`flow`, `positions`, `pnl`, `state`) after each sweep
3. **Verifies** all messages are signed by the referee DID
4. **Detects gaps** in message sequences and fills them using the export API
5. **Stores** all messages in append-only SQLite at `./data/flopstar.db`

**Important**: The monitor does **NOT** access your private key. It only reads public referee data.

### Trust Anchor Warning

⚠️ The monitor uses a **PROVISIONAL** trust anchor from the sonnet-2 launch record:

```
did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte
```

The close-1 contest does not yet have a signed launch record. The monitor logs a loud warning on startup about this.

### Data Storage

Messages are stored in `./data/flopstar.db` (configurable via `FLOPSTAR_DATA_DIR`):
- Room-sequence keyed storage
- Append-only (no updates or deletes)
- Stores raw JSON exactly as received

### Rate Limiting

The monitor respects technocore.chat's limits:
- Only 4 concurrent long-polls per IP allowed
- We use 1 long-poll (on `d-close1-price`)
- Handles 429 responses, backing off for the "retry after" the body names

## Running the Trader

The tree trader (design and results in [docs/TREE.md](docs/TREE.md)) trades with 64 keys
derived from a master seed; the Flopstar key itself never trades.

```bash
uv run flopstar trader run               # paper mode: throwaway keys, signs and posts nothing
uv run flopstar trader status            # paper state and the tree's best scores
uv run flopstar trader run --live        # the real tree keys (needs FLOPSTAR_TREE_SEED_PATH)
```

Each sweep it rebuilds a shadow fold of our keys through the vendored fold, checks it against
the referee's posts, and steps the tree once inside a safe posting window. Any void, missed read
or mismatch writes `data/KILL`, after which it signs nothing until the file is removed. On the
droplet it runs as `flopstar-trader.service` (live) and `flopstar-trader-paper.service`.

## FLOP Agent

`src/flopstar/flop/` makes Flopstar a FLOP Network agent. The design, every protocol fact with its
source, the open questions and the go-live sequence are in [docs/FLOP.md](docs/FLOP.md).

**Key design.** The Flopstar owner key does nothing on FLOP. A separate sr25519 **spend account**
(encrypted Polkadot JSON keystore) pays inference escrow. Its small faucet-funded balance and the
policy's caps bound what a bug could spend.

**How it buys inference** (one interface, three routes):
- **direct**, preferred: the pinned `flop session run` CLI (container pinned by digest, memory-capped)
  opens a channel with a named miner. The FLOP SDK verifies every receipt before paying, and the
  result is recorded as `sdk_verified`.
- **gateway**: FLOP's OpenAI-style API with a bounded API key. Convenient but `unverified`.
- **native**: not available. The Python SDK has no inference-request flow.

**Safety rules**
- **Dry run by default:** every command that signs, spends or sends does nothing without `--post`.
- **One gate:** only `FlopPolicy` may sign or launch the signing CLI. It allowlists action kinds
  and on-chain calls, checks caps against the ledger in the same transaction that records a spend
  (per request, per day, per hour, model allowlist, same-control miner blocklist), and verifies the
  chain's genesis against the pinned one.
- **Kill switch:** `data/KILL-flop` stops every FLOP action. It trips on cap breaches, verification
  failures, mismatches and unexpected errors, and only a human removes it.
- **Append-only ledger:** every spend and faucet draw is in SQLite tables whose history SQLite
  triggers protect from rewrites; every signature is logged.
- **Exact amounts:** FLOP has 18 decimals. Amounts are integer base units or decimal strings, never
  floats.
- **Untrusted output:** inference replies are files for humans, never executed, posted or used to
  sign, and inputs containing key material are refused.
- **Touchstone reporting:** request/response bytes are encrypted before they leave the server,
  failed reports buffer and replay in order, and plain HTTP is allowed only on a private network.

**Commands**

```bash
uv run flopstar flop config                  # network profile, caps, paths (read-only)
uv run flopstar flop status                  # chain identity, health, spend balance, budget
uv run flopstar flop wallet setup [--post]   # create the encrypted spend keystore
uv run flopstar flop faucet [--post]         # draw from the network faucet
uv run flopstar flop ask --model M --prompt TEXT --purpose NAME [--route direct|gateway] [--post]
uv run flopstar flop run [--post]            # useful-work tasks: contest-digest, ops-review,
                                             # research-notes (spend only when there is input)
uv run flopstar flop recovery [--post]       # recover escrow from our own stuck channels
uv run flopstar flop touchstone status|check|register|replay
```

Configuration: `FLOPSTAR_FLOP_CONFIG` (TOML, example `deploy/flop.example.toml`, every cap
explicit) and `FLOPSTAR_FLOP_NETWORK` (`devnet` | `testnet`); devnet earns nothing.

**Status (1 Nov 2026).** Deployed on the droplet against devnet with a spend account and no funds
yet (the faucet's per-IP budget refused the first draw). Nothing spends: there are no published
miner endpoints or gateway keys yet, and testnet isn't live. Details: HANDOFF §10.

## Investigation: Flow Room Data

**Question**: Do `d-close1-flow` posts list per-trade-id outcomes (settled/void + reason), or only counts?

**Answer** (revised against live posts): per-trade `[id, reason]` lists, but **truncated**:
```json
{"t":"flow","n":32,"mints":[],"rooms":[…],"settled":[],"void":[["2ff3e772","funds"],…],"missed":[],"omitted":{"mints":19414,"settled":1237,"void":6},"file":"<hash>"}
```

The full per-sweep record is in the **flow file** referenced by the hash, which is not downloadable
(issue #6), so full-ledger replay is still blocked.

## Architecture

```
src/flopstar/
├── __init__.py       - Package metadata
├── cli.py            - Command-line entry point
├── config.py         - Referee DID, rooms, own room, paths
├── didkey.py         - DID:key ↔ Ed25519 conversion, signature verification
├── technocore.py     - technocore.chat client: reads, long-poll, export, signed posts and notes
├── store.py          - Append-only SQLite message store
├── monitor.py        - Read-only referee monitor loop
├── signer.py         - Encrypted key loading, DID derivation, PolicySigner
├── register.py       - Flopstar's close-1 owner registration
├── room.py           - Own room: status | verify | claim | register | allow | heartbeat | reclaim
├── evidence.py       - Saves our exact signed records from the room export
├── tree.py           - Key tree: HKDF derivation, sizing, round/split engine
├── dryrun.py         - Tree run against the vendored fold over simulated paths
├── treecli.py        - Tree commands: init | dids | dryrun
├── treesigner.py     - Tree-key signing, policy-checked and logged
├── hyperliquid.py    - Latest xyz:NVDA trade, for pricing the tree's trades
├── trader.py         - Tree trader: shadow fold, referee reconciliation, posting, kill switch
└── flop/             - FLOP agent
    ├── config.py     - Network profiles, explicit caps, miner list and blocklist
    ├── amounts.py    - Exact 18-decimal FLOP amounts (floats refused)
    ├── ledger.py     - Append-only spend ledger (SQLite triggers)
    ├── policy.py     - FlopPolicy: the only path to signing or spending; kill switch
    ├── client.py     - Chain reads via flop-sdk / substrate-interface, runtime check
    ├── wallet.py     - Spend keystore (key design A)
    ├── faucet.py     - Faucet draws with genesis checks
    ├── inference.py  - direct (CLI) and gateway routes, plan/execute
    ├── tasks.py      - Useful-work tasks and the secret guard
    ├── recovery.py   - Permissionless channel recovery (expire_force_open, timeout, finalize)
    ├── touchstone.py - Encrypted, buffered reporting to Touchstone
    ├── secretfiles.py- Owner-only files or systemd credentials
    └── cli.py        - `flopstar flop ...`
```

Other directories: `deploy/` (systemd units and timers, `flop.example.toml`), `docs/`
(`HANDOFF.md` for current state and next steps, `TREE.md` for the key tree design, `FLOP.md` for
the FLOP agent) and `vendor/close-call/` (the challenge package).

### Key Design Decisions

1. **No LLM in data path**: All room content is untrusted data, never instructions
2. **Signature verification**: All referee messages verified before storage
3. **Gap detection**: Automatically fills sequence gaps using export API
4. **Single long-poll**: Only `d-close1-price` uses long-polling to stay under 4 concurrent limit
5. **Exact decimal arithmetic**: Uses vendored fold (no rounding until output)
6. **Policy-gated signing**: All owner-key signatures go through `PolicySigner`, which allows only
   known rooms, notes and message types and logs every signature to `data/signatures.log`
7. **Flat main key**: The Flopstar key holds the identity and owns the room but never trades;
   trading is done by a 64-key tree (`docs/TREE.md`), whose keys sign only through `TreeSigner`
8. **Dry run by default**: Signing commands only post with `--post`; the trader only with `--live`
9. **Separate keys by role on FLOP**: the owner key stays off FLOP; a capped spend account pays,
   and only `FlopPolicy` signs (see [FLOP Agent](#flop-agent))

## Vendored Code

The challenge package is vendored at `vendor/close-call/` from commit `66c1da3`:

```bash
# Verified manifest hash (matches referee seed d-close1-price seq 1)
bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa
```

The vendored code is never edited. We import or subprocess `close_call_fold.py` as-is.

## Testing

```bash
# Run tests
uv run pytest -v

# Run linter
uv run ruff check

# Run fold conformance test
uv run pytest tests/test_fold_conformance.py -v
```

The conformance test verifies the vendored fold produces expected output on `sample-season.jsonl`.

The suite has 198 tests. The FLOP tests use throwaway keys, mocked transports and a fake CLI runner.
None touches the owner key, the tree seed or a live network. The `flop session run` fixtures in
`tests/fixtures/flop/` are **synthetic** (shaped from the CLI's source) until real devnet output can
be captured.

## Development

### Git Workflow

**IMPORTANT**: Never commit or push secrets.

```bash
# The pre-commit hook will block secrets automatically
git add .
git commit -m "Your message"

# Test the hook (should fail):
echo "-----BEGIN PRIVATE KEY-----" > test.txt
git add test.txt
git commit -m "test"  # ❌ Blocked by hook
rm test.txt
```

### Deployment

Flopstar runs on a dedicated droplet under systemd, as a non-root `flopstar` user. The key
passphrase and the tree seed exist there only as systemd encrypted credentials. The full runbook,
the unit list and how to deploy a change are in [docs/HANDOFF.md](docs/HANDOFF.md) (§3 and §7).

The FLOP agent's units are in `deploy/`: `flopstar-flop-run.*` (task runner, three times a day),
`flopstar-flop-recovery.*` (hourly) and `flopstar-flop-faucet.service` (one draw). They are
examples, not installed by default; their state on the droplet is in HANDOFF §10.

Never transport the key or the seed through git, Slack, email, or logs.

## License

See LICENSE and NOTICE files. The vendored challenge code retains its original license.

## Contest Information

**Concluded.** Trading locked at sweep 2556 on 4 Oct 2026; final price *S* = 234.69. Flopstar's
best keys scored +312.25 and didn't place.

- **Contest ID**: `close-1`
- **Opening**: September 25, 2026, 12:00 UTC
- **Lock**: October 4, 2026, 09:00 UTC
- **Closing Price**: Last `xyz:NVDA` trade before October 4, 2026, 10:00 UTC
- **Sweeps**: Every 5 minutes (2,556 total)
- **Prizes**: 1,000,000 FLOP split among top 3 places

Referee rooms:
- `d-close1-price` - Reference prices and limits
- `d-close1-flow` - Mints, rooms, trade outcomes (truncated lists + `omitted` counts)
- `d-close1-positions` - Open interest
- `d-close1-pnl` - PnL and leaderboard
- `d-close1-state` - State roots

Trading room: `close1`

## Author

**Fodé Diop** - [@botmechanic](https://github.com/botmechanic)

Contribution to the FLOP network community.

## References

- [Challenge Repository](https://github.com/flop-labs/technocore-close-call-challenge)
- [Technocore.chat](https://technocore.chat)
- [Contest Rules](vendor/close-call/close-call-game.md)
- [FLOP docs](https://docs.flop.network) and [SDK references](https://docs.flop.network/docs/sdk-references)
- [flop-sdk on PyPI](https://pypi.org/project/flop-sdk/) (pinned `==0.2.0`)
