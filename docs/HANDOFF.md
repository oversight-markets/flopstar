# Flopstar Handoff: close-1 concluded; FLOP agent built and parked for testnet

**Project**: Flopstar, an agent for FLOP Labs' Close Call contest (`close-1`) on technocore.chat
**Repository**: https://github.com/oversight-markets/flopstar
**Written**: 25 September 2026, 15:40 UTC; **updated 25 September 2026, 23:05 UTC** after the move; **28 September 2026, 19:50 UTC** after
the round-1 split; **4 October 2026, 14:45 UTC** after the lock, the statement and the wind-down;
**1 November 2026, 18:10 UTC** for the FLOP agent (§10); **3 November 2026** after FLOP opened
devnet to builders (§10)
**Host**: droplet `flopstar` (68.183.21.81), running only Flopstar
**Previous host**: `hoodwatch` (167.99.238.68): decommissioned for Flopstar on 25 Sep; its repo
clone was deleted and its copies of the key and seed shredded (owner-confirmed)

**Close-1 is over.** Trading locked at sweep 2556 (4 Oct 09:00 UTC) and the final price is in.
Flopstar didn't place: its best keys scored +312.25 against about +1,340 for 3rd. The statement
naming the 64 tree keys is posted ([STATEMENT.md](STATEMENT.md)), and every service and timer is
stopped and disabled. The keys, credentials and data are all still on the droplet; what to do with
them is the owner's call (§8). §3 stays as the runbook for rebuilding the droplet. Section 2 is
the only place where a mistake can't be undone.

**Since 1 Nov 2026 the droplet also hosts the FLOP agent** (`src/flopstar/flop/`): Flopstar buying
inference on the FLOP Network, with every purchase recorded and reported to Touchstone. It is built,
deployed and holds a spend account, but it is **parked until FLOP's testnet** (target 3 Nov 2026,
not up on the day) and spends nothing. FLOP opened devnet to builders on 3 Nov; that changes no
blocker (§10). State and next steps: **§10**. Design and sources: [FLOP.md](FLOP.md).

---

## 1. State (4 Oct 2026, 14:45 UTC): final

| Item | State |
|---|---|
| Contest | **Concluded.** Lock at sweep 2556 (09:00:29 UTC posts); final price *S* = **234.69**, the `xyz:NVDA` trade at 09:59:40.596 UTC (`d-close1-price` `final` post, 10:00:15 UTC). 18,790,926 owners and 80 rooms at the lock. |
| Result | **No place.** Best keys **+312.25** (the 16 round-0 longs, 43.08 each from 225.19). Worst key −533.43; tree total −12,361.50, exactly its fees. Referee pnl top list at the lock (marked 234.31): #1 1,558.46, #3 1,323.44, #25 1,161.03; none of ours in the top 25. The official standings would be in `d-close1-results`, which the monitor never read. |
| Tree | Round 0 (32 pairs, 43.08 each at 225.19, sweep 132) and the **round 1 split** (+3.06% at sweep 883; closes at 232.62, reopens short 39.55 at 232.77, sweeps 884–885). **Round 2 never triggered**: NVDA stayed between 225.11 and 239.05 from 28 Sep to the lock (high 237.69). 64 trades in all, every one settled, no voids, kill switch never tripped. The paper trader matched throughout. |
| Statement | Posted by Flopstar in `d-flopstar-close1`, **seq 165**, 14:35:11 UTC: the 64 tree DIDs in index order with their sha256. Signature and content verified; record in `data/statement-close1.jsonl` and [STATEMENT.md](STATEMENT.md). |
| Services | **All stopped and disabled** at 14:40 UTC (`systemctl disable --now`): `flopstar-monitor`, `flopstar-trader`, `flopstar-trader-paper`, `flopstar-heartbeat.timer`, `flopstar-reclaim.timer`. Unit files stay in `/etc/systemd/system`. Nothing on the droplet signs or reads technocore.chat any more. |
| Referee store | `data/flopstar.db` holds all five referee rooms complete from seq 1 to the lock (price to seq 2558, with the final post), no gaps, every record signature-verified. |
| Own room `d-flopstar-close1` | Still owned by Flopstar (claim rewritten 3 Oct 22:30 UTC); last write is the statement. With the keepalive off, technocore.chat deletes the room and its notes after 7 days with no write, so **about 11 Oct**. The repo copy of the statement is the durable one. |
| Flopstar owner key | Unchanged: `/etc/flopstar/keys/flopstar.pem` plus the passphrase credential on the droplet, and the Mac copy. DID `did:key:z6MkjLpUAGLtNieLnCFQoUScwJxAKwo5PHZcHRsdyiCJG5Bv`. |
| Tree seed | Unchanged: the droplet credential and the owner's offline backup. |
| Data | `/var/lib/flopstar/data`: referee DB, trader states, signature logs, registration and statement evidence. Not in git. |
| Secrets hook | Enabled in both clones (`/root/var/www/flopstar`, `/opt/flopstar`). |

### Why a dedicated droplet
- **Other services on hoodwatch run as root** (a Bun bot on :8080 and Docker). Any of them being
  compromised exposes the Flopstar key. On its own droplet, nothing else runs.
- **Rate limits are per IP** (600 reads/min, 300 writes/min, 4 long-polls). hoodwatch shares its
  IP with FlopWatch, which also reads technocore.chat. Flopstar gets its own budget.

---

## 2. Secrets inventory: read before moving anything

| Secret | Where it is now | Copies | Notes |
|---|---|---|---|
| `flopstar.pem` (Ed25519, PKCS8, **passphrase-encrypted**) | Droplet `/etc/flopstar/keys/flopstar.pem` (0600 flopstar) **and** the owner's Mac `~/.config/flopstar/flopstar.pem`. The hoodwatch copy is shredded. | 2 | Never generate a replacement. The Flopstar DID is its identity. |
| Key passphrase | The owner's head / password manager; on the droplet only as `/etc/flopstar/creds/flopstar-passphrase.cred` | – | Never typed into chat, a repo, `data/` or a log. |
| `close1-master.seed` (32 bytes, hex) | Droplet, only as `/etc/flopstar/creds/close1-master-seed.cred`; the owner's offline backup. The hoodwatch copy is shredded. | 2 | The offline backup is now the only way to rebuild the droplet's seed. Recreates all 64 tree keys. Losing it loses the tree; leaking it leaks all 64 keys. |
| `evidence/` (pre-contest proof for the sonnet contest) | The owner's Mac only | 1 | Not needed on the droplet. Keep it backed up. |
| FLOP spend key (sr25519, encrypted Polkadot JSON keystore) | Droplet `/var/lib/flopstar/flop/spend.json` (0600 flopstar). Address `5C9zWSykX2EHGF6pbRkPGTHrjYcnj2WjQRnm2xbPfye4uUFb`. | 1 | Generated on the droplet 1 Nov (§10). **No backup.** It holds only devnet FLOP (none yet), so losing it costs nothing today; back up keystore + passphrase before it holds testnet funds. |
| FLOP spend keystore passphrase | Droplet only, as `/etc/flopstar/creds/flop-spend-passphrase.cred` (32 random bytes, generated straight into `systemd-creds`; nobody has seen it) | 1 | Decryptable only on this droplet (host key). Losing the droplet loses the spend account. |
| Touchstone streak key (32 bytes, base64) | Droplet only, as `/etc/flopstar/creds/touchstone-streak-key.cred` (generated 3 Nov 2026 15:48 UTC straight into `systemd-creds`; nobody has seen it). Touchstone never holds it. | 1 | Encrypts request/response bytes sent to Touchstone (`aes-256-gcm/v1`). Decryptable only on this droplet; losing it makes the stored ciphertexts unreadable to us (the plaintext sha256 fields remain). Leaking it exposes reported prompts/responses. |

**Rules**
- **Transfers:** move secrets only over SSH. Never via git, chat, email or logs.
- **Encrypt at rest:** on the droplet, the passphrase and the seed exist only as **systemd
  encrypted credentials**. Neither ever sits on disk in the clear.
- **One live signer at a time.** Don't run signing on hoodwatch and the droplet together.
- **Delete old copies only after verification.** Shred the hoodwatch copies only after the
  droplet has passed §3.8, and only after the seed has an offline backup.

**Step 0 (on your Mac, before anything else):** back up the seed offline.
```bash
ssh root@167.99.238.68 'cat /root/.config/flopstar/close1-master.seed'
# store the 64 hex characters in your password manager / offline backup
```

---

## 3. New droplet runbook

**Done on 25 September 2026, 21:40–22:30 UTC**, on `flopstar` (68.183.21.81). Kept as the runbook
for rebuilding the droplet. Where the move differed from the steps below:
- **Code (§3.3):** `/opt/flopstar` was cloned from the working copy at `/root/var/www/flopstar`,
  not with a deploy key. Updates are deployed the same way (§7).
- **Seed (§3.6):** copied with `scp -3` from hoodwatch into `/run/flopstar-seed/` (RAM only), then
  encrypted with `systemd-creds encrypt` and the plain copy shredded.
- **Data (§3.7):** hoodwatch no longer had `/root/var/www/flopstar/data`, so the data came from the
  owner's backup tarball of it (taken 25 Sep ~15:51 UTC). The monitor backfilled the rest.
- **Pushing:** the working copy pushes over HTTPS with a fine-grained token saved by
  `credential.helper store` in `/root/.git-credentials`. Replace it when it expires.
- **Paste hazard:** long commands pasted into the droplet's terminal have been split at line
  breaks, running half a command. Signing steps are one-shot units so the command is short.


### 3.1 Droplet
- Ubuntu 24.04 LTS (needs systemd ≥ 254 for `%d` in unit files; 24.04 ships 255). The smallest
  size is plenty: the monitor uses under 100 MB of RAM and about 1 MB/day of disk.
- Log in with SSH keys only. Disable password logins.
- **No inbound ports other than SSH.** Flopstar only makes outbound HTTPS requests.
```bash
ufw default deny incoming && ufw allow OpenSSH && ufw enable
```

### 3.2 User, directories, uv
```bash
useradd --system --home-dir /opt/flopstar --shell /usr/sbin/nologin flopstar
install -d -o flopstar -g flopstar -m 0755 /opt/flopstar
install -d -o flopstar -g flopstar -m 0700 /var/lib/flopstar /var/lib/flopstar/data
install -d -o root     -g flopstar -m 0750 /etc/flopstar
install -d -o flopstar -g flopstar -m 0700 /etc/flopstar/keys
install -d -o root     -g root     -m 0700 /etc/flopstar/creds
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
```

### 3.3 Code
The repo is private. Add a **read-only deploy key** for the droplet on GitHub (repo → Settings →
Deploy keys) and use it only for this clone.
```bash
git clone git@github.com:oversight-markets/flopstar.git /opt/flopstar
git -C /opt/flopstar config core.hooksPath .githooks     # per-clone, not versioned
chown -R flopstar:flopstar /opt/flopstar
cd /opt/flopstar && sudo -u flopstar env UV_PYTHON=python3.12 UV_CACHE_DIR=/opt/flopstar/.cache/uv uv sync --frozen
sudo -u flopstar /opt/flopstar/.venv/bin/python -m pytest -q     # expect all tests to pass
```

### 3.4 Owner key: from the Mac
Run this **on your Mac**, replacing `NEW` with the droplet's address:
```bash
ssh root@NEW 'umask 077; cat > /etc/flopstar/keys/flopstar.pem && chown flopstar:flopstar /etc/flopstar/keys/flopstar.pem && chmod 0600 /etc/flopstar/keys/flopstar.pem' \
  < ~/.config/flopstar/flopstar.pem
```
Then check it on the droplet. It prompts for the passphrase and must print `MATCH`:
```bash
cd /opt/flopstar && sudo -u flopstar env FLOPSTAR_KEY_PATH=/etc/flopstar/keys/flopstar.pem .venv/bin/flopstar verify-key
```

### 3.5 Passphrase: encrypted credential
You type it once. It's encrypted with the droplet's host key and never written in the clear.
```bash
systemd-ask-password -n "Flopstar key passphrase:" \
  | systemd-creds encrypt --name=flopstar-passphrase - /etc/flopstar/creds/flopstar-passphrase.cred
chmod 0600 /etc/flopstar/creds/flopstar-passphrase.cred
```
With no TPM, `systemd-creds` uses `/var/lib/systemd/credential.secret` (root-only). A copied
`.cred` file is useless off the box. Root on the droplet can still decrypt it, which is why
nothing else should run there.

### 3.6 Master seed: from hoodwatch straight into a credential
Run this **on your Mac**. The seed streams from hoodwatch into `systemd-creds` on the droplet and
never lands on the droplet's disk in the clear:
```bash
ssh root@167.99.238.68 'cat /root/.config/flopstar/close1-master.seed' \
  | ssh root@NEW 'systemd-creds encrypt --name=close1-master-seed - /etc/flopstar/creds/close1-master-seed.cred && chmod 0600 /etc/flopstar/creds/close1-master-seed.cred'
```
Check it by comparing the derived DIDs with the published list. This prints only public DIDs:
```bash
systemd-run --pipe --wait -p User=flopstar -p WorkingDirectory=/opt/flopstar \
  -p LoadCredentialEncrypted=close1-master-seed:/etc/flopstar/creds/close1-master-seed.cred \
  -p Environment=FLOPSTAR_DATA_DIR=/var/lib/flopstar/data \
  /bin/sh -c 'FLOPSTAR_TREE_SEED_PATH=$CREDENTIALS_DIRECTORY/close1-master-seed /opt/flopstar/.venv/bin/flopstar tree dids && head -2 /var/lib/flopstar/data/tree-dids.txt'
```
The first line must be `0 did:key:z6MkoW9KCbdiBrftF8We3uZSXnyjY8HTZvAtkw5Hyg4E9PM2`.

### 3.7 Data: carry history over
The monitor DB can be rebuilt by backfill, but only while the referee rooms still hold the
records. The registration evidence can't be rebuilt at all, because `close1` keeps only about
6 minutes of history. Copy everything, on your Mac:
```bash
ssh root@167.99.238.68 'tar -C /root/var/www/flopstar/data -cz .' \
  | ssh root@NEW 'tar -C /var/lib/flopstar/data -xz && chown -R flopstar:flopstar /var/lib/flopstar/data && chmod 0600 /var/lib/flopstar/data/*'
```

### 3.8 Services and verification
```bash
cp /opt/flopstar/deploy/*.service /opt/flopstar/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start flopstar-signer@verify.service      # decrypts credential, loads key, checks DID
journalctl -u flopstar-signer@verify -n 5           # must show the Flopstar DID; signs nothing
```
Pass criteria: `verify` shows the Flopstar DID; the §3.6 check matches; `pytest` passes; and
`sudo -u nobody cat /etc/flopstar/keys/flopstar.pem` is refused.

### 3.9 Cutover
Keep this order so that exactly one monitor is running at any time:
```bash
# on hoodwatch
pkill -f "flopstar monitor"
# on the droplet
systemctl enable --now flopstar-monitor.service
journalctl -u flopstar-monitor -f                   # expect "Synced ..." then long-polling
```
Enable the timers **as soon as the first heartbeat is posted** (§4), and within 12 hours of it:
```bash
systemctl enable --now flopstar-heartbeat.timer flopstar-reclaim.timer
```

### 3.10 Decommission on hoodwatch

**Done (25 Sep 2026, owner-confirmed):** the hoodwatch repo clone was deleted and its key and
seed shredded. On a rebuild, the key comes from the Mac (§3.4) and the seed from the owner's
offline backup, piped into `systemd-creds encrypt` the same way as §3.6.

Only after §3.8 has passed and the seed has an offline backup:
```bash
shred -u /root/.config/flopstar/flopstar.pem /root/.config/flopstar/close1-master.seed
rm -rf /root/var/www/flopstar/data        # already copied in §3.7
```
The Mac's copy of `flopstar.pem` remains as the backup.

`deploy/MIGRATION.md` covers the same-host variant (moving within hoodwatch). This section
replaces it for the droplet move.

---

## 4. Own room `d-flopstar-close1`: listed, allow-listed, trading

We trade in our own `d-` room because `close1` keeps only about 100 seconds of history now (200
messages), and a message only counts if the referee reads it before it drops out. The referee
has stalled for 13+ minutes before. A `d-` room can be claimed **only before its first message**,
and a lost claim can never be retaken.

**Done (25 Sep 2026, UTC):**
| Time | Step | How |
|---|---|---|
| 15:42:31 | Claim (owner note) | `room claim --post`, on hoodwatch |
| 22:28:53 | First message | `systemctl start flopstar-signer@heartbeat` |
| 22:29:13 | Registered in `close1` (seq 1039625) | `systemctl start flopstar-signer@register` |
| 22:29:36 | Timers on; second heartbeat and a claim rewrite at once | `systemctl enable --now flopstar-heartbeat.timer flopstar-reclaim.timer` |
| sweep 126 | Listed by the referee | `room status` |
| 22:48:51 | Allow-list: the 64 tree DIDs | `systemctl start flopstar-signer@allow` (`room allow --post`) |
| 22:52 | 64 tree `owner` posts, minted at sweep 131 | `systemctl start flopstar-tree-register` |
| 22:57 | Round 0 posted; settled at sweep 132 | `systemctl enable --now flopstar-trader` |
| 28 Sep 13:36 | Round 1 split: closes settled at sweep 884, reopens (13:41) at sweep 885 | the trader, unattended |
| 3 Oct 22:30 | Last claim rewrite | `flopstar-reclaim.timer` |
| 4 Oct 09:00 | Lock (sweep 2556); final price 234.69 posted 10:00:15 | the referee |
| 4 Oct 14:35 | Statement naming the 64 tree keys (seq 165) | `systemctl start flopstar-signer@statement` |
| 4 Oct 14:40 | Wind-down: all services and timers stopped and disabled | `systemctl disable --now …` |

**Don't run `claim` again**: it would stop with "already has an owner note". If `room status`
ever doesn't show Flopstar as the owner, **stop**: the claim has lapsed or been taken, so post
nothing in the room.

**Claim deadline.** A note with no write for 7 days is deleted. `flopstar-reclaim.timer` rewrote
it every 4 days until the wind-down (last: 22:30:12 on 3 Oct). Both timers are now disabled, so
the room and its notes lapse around 11 Oct.

**Keepalive.**
- **Why:** notes and rooms with no write for 7 days are deleted, and a room with a single message
  goes after 12 hours.
- **The timers:** `flopstar-heartbeat.timer` (every 6 h) and `flopstar-reclaim.timer` (rewrites
  the owner note every 4 days).
- **Their log:** both write to `data/signatures.log`, as every signature does.

---

## 5. Decisions and policies (agreed with the owner)

- **Flopstar's main key stays flat.** It holds the identity, owns the room, and makes the
  registration posts. It never trades.
- **Trading is done by a 64-key tree.** The design, dry-run results and remaining work are in
  `docs/TREE.md`.
  - The close-1 rules allow it: "One operator may run many keys and hold several places";
    `identity_policy: any did:key`; the organiser "disqualifies nobody at discretion".
  - The one-DID rule came from the **sonnet** contest and doesn't apply here.
- **Tree keys are close-1-only.** They sign only in `d-flopstar-close1` (`TreeSigner` enforces
  it), and never anywhere else on technocore.chat.
  - They register in `d-flopstar-close1` after the referee lists it, and each is allow-listed first.
  - After the lock, Flopstar signs and publishes a statement listing all 64 tree DIDs, in its
    room and in this repo.
- **Only a party to a trade posts it.** This is our **policy choice**. The confirmed rule is
  only that the poster must be a registered key.
- **Signer policy.** All owner-key signing goes through `PolicySigner`: rooms `close1` and
  `d-flopstar-close1`; notes `room-owners` and `room-allow` for our room; message types `owner`,
  `room`, `trade`, `heartbeat` and `statement`. Every signature is logged. Never print key material.
- **Never generate a replacement Flopstar key.**

---

## 6. Contest facts (verified live, 25 Sep 2026)

- **Referee DID** (provisional: close-1 has no signed launch record yet):
  `did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte`. Pinned in FLOP Labs' signed sonnet-2
  launch record; it owns the `d-close1-*` rooms and signed the seed.
- **Seed:** `d-close1-price` seq 1 pins package `bae09812…96dafa`, which matches
  `vendor/close-call/manifest.json` (commit 66c1da3). Seed price 226.14.
- **Timeline:** sweeps every 5 min from 12:05 UTC on 25 Sep; lock at sweep 2556, 4 Oct 09:00 UTC;
  *S* = the last `xyz:NVDA` trade before 10:00 UTC on 4 Oct.
  - **The referee runs late:** sweep 32 posted 3 minutes late, then nothing came for 13+ minutes.
  - **Stale reference:** the reference can be old (`age_s` 3002 at sweep 32), because
    `xyz:NVDA` trades thinly.
- **Field size:** 352,876 owners and 51 registered rooms at sweep 32, so sybil farms are active.
  The top PnL was about +104.
- **Record format:** `{"seq","ts","from","text","nonce","sig"}`.
  - `from` is the signer DID. `sig` is 86 characters of unpadded base64url over
    `<room>|<nonce>|<text>`.
  - Nonces can be 19 digits: parse them as ints or strings, never floats.
  - Signed POSTs send `nonce` as a **digit string**.
- **Flow posts** list `settled` ids and `void` `[id, reason]` pairs, but they're **truncated**;
  an `omitted` object counts what was left out. The full flow files can't be downloaded (issue
  #6), so a full replay is blocked. Our own trades are confirmed by the shadow fold. In practice
  mints and settlements are heavily truncated but the void list is usually complete (no `void`
  key in `omitted`), so "not in a complete void list, and `missed` empty" means settled.
- **Price posts** carry `n`, `ref` (Hyperliquid's last trade: sweep n's close), `applied` (the
  previous sweep's `ref.px`: the reference sweep n's trades were limited against), `for` (n+1),
  `limits` for n+1, `global` and `age_s`. Sweep n cuts at 12:00 UTC + 5n minutes and posts about
  13 s later.
- **Trade signatures** (checked against live posts): the maker signs
  `close-1|terms|<terms>` and the taker `close-1|accept|<terms>|<taker did>`, with `terms`
  compact JSON with sorted keys. Many trades posted in `close1` carry invalid signatures.
- **Field size:** 835,056 owners and 138 rooms at sweep 132. The positions top list is ±44.6
  all-in pairs and the pnl top list dozens of identical scores: other trees are running.
- **API behavior:**
  - `?since=S&limit=L` returns the **newest** L messages after S.
  - `/r/<room>/export` returns the whole retained history as raw JSONL and takes no parameters.
  - A 429 body says "retry after: Ns".
  - The server refuses the same text more than 5 times in 120 s (a 422).
  - A message's nonce must be above the last nonce that key used in that room.
  - A room keeps about 10 MiB of history; a message can be up to 4,096 characters.
  - A signed POST replies in text (`# room … messages N range a..b`), not JSON; the newest
    messages read right after a post sometimes don't include it yet, so the trader falls back to
    the export.
- **Claim notes:**
  - `room-owners` and `room-allow` are signed writes over `<ns>|<key>|<nonce>|<value>`.
  - Both share `/kv/room-nonce/<room>` as their replay counter; a new nonce must be above it.
  - The allow-list note is capped at 8,192 characters, which is enough for about 140 DIDs.
    It is the DIDs separated by single spaces.

---

## 7. Code map and commands

```
src/flopstar/
├── cli.py         entry point
├── config.py      referee DID, rooms, OWN_ROOM, paths
├── didkey.py      did:key → Ed25519, signature verification (base64url)
├── technocore.py  HTTP client: reads, long-poll, export, signed posts, signed notes
├── store.py       append-only SQLite (room, seq) → raw record
├── monitor.py     referee monitor: long-poll price, backfill gaps, sync the other rooms
├── signer.py      encrypted key loading, did derivation, PolicySigner
├── register.py    Flopstar's close-1 owner registration
├── room.py        own room: status | verify | claim | register | allow | heartbeat | reclaim | statement
├── evidence.py    save our exact signed records from the room export
├── tree.py        key tree: HKDF derivation, sizing, round/split engine
├── dryrun.py      tree against the vendored fold over simulated paths
├── treecli.py     tree: init | dids | dryrun
├── treesigner.py  tree-key signing, policy-checked and logged
├── hyperliquid.py latest xyz:NVDA trade, for pricing
├── trader.py      the tree trader: shadow fold, reconciliation, posting, kill switch
└── flop/          the FLOP agent (§10, FLOP.md): config, amounts, ledger, policy, client,
                   wallet, faucet, inference, tasks, recovery, touchstone, secretfiles, cli
deploy/            systemd units, timers, same-host MIGRATION.md, flop.example.toml
docs/TREE.md       key tree design, dry-run results and how the trader runs it
vendor/close-call/ challenge package at 66c1da3. Never edit it.
```

| Command | Signs? | Needs |
|---|---|---|
| `flopstar monitor` | no | network |
| `flopstar verify-key` | no | key + passphrase |
| `flopstar register [--post]` | yes | key + passphrase (done; repeating is harmless) |
| `flopstar room status` | no | network |
| `flopstar room verify` | no | key + passphrase |
| `flopstar room claim/register/heartbeat/reclaim/statement [--post]` | only with `--post` | key + passphrase |
| `flopstar room allow [--post]` | only with `--post` | `data/tree-dids.txt`; key + passphrase to post |
| `flopstar tree init` | no | creates the seed; **refuses if one exists**. Never run it: the seed already exists, and a new one would be a different tree (rebuild from the offline backup, §3.10). |
| `flopstar tree dids` | no | seed |
| `flopstar tree dryrun [paths]` | no | nothing (throwaway seed) |
| `flopstar trader run` / `trader status` | no | paper mode: throwaway keys (`data/trader-paper.json`) |
| `flopstar trader run --live` / `status --live` | run: yes | seed (`data/trader.json`) |
| `flopstar trader register [--post]` | only with `--post` | seed (done; re-running skips registered keys) |

Units (in `/etc/systemd/system`, copied from `deploy/`). Since the wind-down all of them are
stopped and disabled:

| Unit | Does | Credential |
|---|---|---|
| `flopstar-monitor.service` | referee monitor | none |
| `flopstar-signer@<action>.service` | one `room <action> --post` (heartbeat, reclaim, register, allow, statement, verify) | passphrase |
| `flopstar-heartbeat.timer` / `flopstar-reclaim.timer` | keepalive every 6 h / claim rewrite every 4 d | – |
| `flopstar-tree-register.service` | one `trader register --post` | seed |
| `flopstar-trader.service` | the live trader | seed |
| `flopstar-trader-paper.service` | the paper trader | none |

**Deploying a change:** commit in the working copy `/root/var/www/flopstar`, then
```bash
git -c safe.directory=/opt/flopstar -C /opt/flopstar pull --ff-only /root/var/www/flopstar main
chown -R flopstar:flopstar /opt/flopstar
cp /opt/flopstar/deploy/<changed units> /etc/systemd/system/ && systemctl daemon-reload
systemctl restart flopstar-trader flopstar-trader-paper    # state is saved; they resume
```

Environment variables: `FLOPSTAR_KEY_PATH`, `FLOPSTAR_PASSPHRASE_FILE`, `FLOPSTAR_DATA_DIR`,
`FLOPSTAR_TREE_SEED_PATH`.

Checks: `uv run pytest -q` (198 tests), `uv run ruff check`, and `python3 vendor/close-call/scripts/verify.py`.

---

## 8. After close-1: what's left

The contest work is done:
- round 0 and round 1 traded and settled;
- the statement is posted and recorded;
- the services are wound down.

The rest is optional, and the owner decides:

1. **Secrets on the droplet.** The owner key (with its passphrase credential) and the tree seed
   credential are still there.
   - The tree seed has no further use; shred `/etc/flopstar/creds/close1-master-seed.cred` once the
     offline backup is confirmed, or keep it if the tree keys might be reused.
   - The Flopstar key is the agent's identity for future FLOP Labs contests, so keep the Mac copy.
     Remove the droplet copy (`flopstar.pem` and `flopstar-passphrase.cred`) if the droplet is
     retired.
2. **Data.** Archive `/var/lib/flopstar/data` (about 30 MB) off the droplet before retiring it.
   The registration and statement evidence can't be rebuilt once the rooms expire.
3. **The droplet itself.** ~~Can be powered off or destroyed after 1–2.~~ **No longer**: since
   1 Nov it hosts the FLOP agent and its spend key (§10), whose passphrase credential can only be
   decrypted on this droplet. Destroying it loses the spend account. The close-1 secrets in item 1
   can still be cleaned up independently.
4. **Official results.** `d-close1-results` (reserved in `contest.json`) should hold the final
   standings. Read it once if a confirmed final score is wanted; our own +312.25 comes from the
   shadow fold at *S*.
5. **Lessons for a next contest** (see TREE.md "Outcome"):
   - The 3% tree got only one split in 9 days: NVDA's reference ranged 223.01–237.69.
   - The leaders realised about +12–15% on that range, with all-in positions sized like ours
     (47.12 contracts). We never worked out how; that is the first thing to study before
     another close-call contest.
   - A kill-switch alert (it only wrote `data/KILL`) was never built; it never tripped.

To restart anything (e.g. the monitor, to read `d-close1-results`):
`systemctl enable --now flopstar-monitor`.

---

## 9. Operations

Kept for reference; since the wind-down nothing is running, and the commands that read live state
need the monitor (for the DB) or the network.

```bash
systemctl status flopstar-monitor flopstar-trader; journalctl -u 'flopstar-*' -n 50
cd /opt/flopstar && sudo -u flopstar env FLOPSTAR_DATA_DIR=/var/lib/flopstar/data .venv/bin/flopstar room status
cd /opt/flopstar && sudo -u flopstar env FLOPSTAR_DATA_DIR=/var/lib/flopstar/data .venv/bin/flopstar trader status --live
journalctl -u flopstar-trader -f | grep -v "HTTP Request"
cat /var/lib/flopstar/data/KILL            # exists only if the kill switch tripped; holds the reason
```

```sql
-- sqlite3 /var/lib/flopstar/data/flopstar.db
SELECT room, COUNT(*), MIN(seq), MAX(seq) FROM messages GROUP BY room;

-- gaps (should be empty)
SELECT room, prev_seq, seq FROM (
    SELECT room, seq, LAG(seq) OVER (PARTITION BY room ORDER BY seq) AS prev_seq FROM messages
) WHERE prev_seq IS NOT NULL AND seq != prev_seq + 1;

-- referee messages by type
SELECT json_extract(json_extract(raw_json, '$.text'), '$.t') AS t, COUNT(*) FROM messages GROUP BY t;
```

**Troubleshooting**
- **Long-poll quiet:** compare the latest `d-close1-price` `ts` with the clock. A referee stall
  looks exactly like this and isn't our fault.
- **INVALID SIGNATURE in a referee room:** stop and investigate. Either the data is forged or
  the referee key changed. Check for a signed close-1 launch record.
- **429:** the client waits for the time the server names. Check that nothing else on the droplet
  hits technocore.chat.
- **`room status` owner isn't Flopstar:** the claim has lapsed or been taken. Don't post anything in the room.
- **Note reads start with "!! UNTRUSTED CONTENT":** that banner is normal; `parse_note_body` strips it.
- **The hook didn't run on a commit:** run `git config core.hooksPath .githooks` in that clone.
- **`data/KILL` exists:** the trader has stopped signing. Read the reason and the journal, check
  `trader status --live`, and delete the file only once the cause is understood. To stop it by
  hand: `sudo -u flopstar touch /var/lib/flopstar/data/KILL`, or `systemctl stop flopstar-trader`.

**Security checklist (droplet)**
- [x] Seed backed up offline before any move (owner, step 0)
- [x] SSH keys only; `ufw` allows only OpenSSH
- [x] `flopstar` user is non-root; key `0600 flopstar`; creds `0600 root`
- [x] Passphrase and seed exist only as `.cred` files
- [x] `flopstar-signer@verify` passes; `sudo -u nobody cat` on the key is refused
- [x] `core.hooksPath` set in `/opt/flopstar`
- [x] hoodwatch copies shredded after verification; only one monitor and one signer running
- [x] `git log --all -- '*.pem' '*.seed'` is empty
- [ ] **Changed 1 Nov (§10):** Docker is installed and `flopstar` is in the `docker` group, which
  is root-equivalent on this host. Nothing running as `flopstar` may take commands from model
  output or remote input. No container publishes ports (the CLI uses `--network host` with no
  `-p`), so Docker's iptables rules don't open anything past `ufw`. Undo: `gpasswd -d flopstar docker`.

---

## 10. FLOP agent: built, parked for testnet (1 Nov 2026, 18:10 UTC)

Flopstar's second job: buy useful inference on the FLOP Network, keep a verifiable record of every
purchase, and report it to Touchstone. The full design, sources, open questions (S1–S12) and the
go-live sequence are in [FLOP.md](FLOP.md); this section is the operational state.

**Decision (owner, 1 Nov):** don't ask FLOP about the open questions; testnet (target Tue 3 Nov,
not yet live, hostnames don't resolve) should answer them. Until then the agent spends nothing.

**Update, 3 Nov:** FLOP announced devnet is open for builders (explorer, faucet, gateway, MCP, SDK;
devnet tokens are worthless, earn nothing, and the chain may reset). Re-checked read-only the same
day (FLOP.md §4.1): every announced link is on FLOP's official-links page; genesis, runtime spec and
packages are unchanged; still 0 session offers and no published miner endpoints; testnet hostnames
still don't resolve. `flop status` is healthy (genesis matches pin, kill switch clear, spend balance
0). Nothing is spendable yet, so the agent stays parked. (Later on 3 Nov the spend account got 25 FLOP from the faucet; still no miners to spend it on.)

| Item | State |
|---|---|
| Code | Phases 0–7 complete in `src/flopstar/flop/`, pushed to `main` and deployed to `/opt/flopstar` (venv synced; `flop-sdk==0.2.0`). |
| Key design | **A**: the owner key (`flopstar.pem`) does nothing on FLOP. A separate sr25519 **spend account** pays escrow; its balance plus FlopPolicy's caps bound spending. Only `flop/policy.py` signs or launches the signing CLI. |
| Network | **devnet** (earns nothing). `/etc/flopstar/flop.toml` (0640 root:flopstar) is the example config: genesis pinned `0x99c1…d631`, caps 0.01 FLOP/request, 0.5/day, 6 requests/h, 1 faucet draw/day, model `qwen/qwen2.5-0.5b-instruct`, **no miners listed**, empty blocklist. |
| Spend account | `5C9zWSykX2EHGF6pbRkPGTHrjYcnj2WjQRnm2xbPfye4uUFb`, **25 FLOP** (faucet grant, landed 2026-11-03 ~12:40 UTC; read over RPC: free `25000000000000000000`, nonce 0, providers 1). Keystore and passphrase credential in §2. Verified: decrypts via the credential as `flopstar`. |
| Faucet | Draw #1 (2026-11-01 18:03 UTC): 429 "IP daily grant budget reached", although this IP had never drawn. Draw #2 (the retry timer, 2026-11-02 18:10 UTC) and draw #3 (manual, owner's go-ahead, 2026-11-03): 503 "IP classification unavailable; retry later", no `Retry-After`. The owner got the same 503 from another machine the same day, and a public devnet user reported the faucet down for everyone, so it was a **FLOP-side outage**, not our IP or request. Before draw #4 the faucet's `/health` was `ok` (connected, ~9.97M FLOP free, image `0.1.245`; limits 300 grants/IP/day, 1/address/24 h) and `/chain` matched our genesis pin. Draw #4 (manual, owner's go-ahead, 2026-11-03 12:38 UTC): 503 **"Grant pending; retry the original request later"**, no `Retry-After`. The grant was in flight, not refused: **25 FLOP** (the faucet's default grant) landed between 12:40:18 and 12:40:50 UTC. The ledger records all four draws as failed and, being append-only, keeps draw #4 that way; the chain is the truth. Kill switch not tripped. **Don't draw again:** the faucet allows 1 grant per address per 24 h. |
| Timers | **No FLOP timers installed.** The one-off `flopstar-flop-faucet-retry.timer` fired on 2 Nov and was disabled and deleted on 3 Nov. The task runner and recovery units (`deploy/flopstar-flop-run.*`, `flopstar-flop-recovery.*`) are **not installed**. |
| Kill switch | `/var/lib/flopstar/data/KILL-flop`: absent (clear). |
| Ledger | `flop_spend`, `flop_spend_events`, `flop_faucet_draws` in `/var/lib/flopstar/data/flopstar.db` (append-only via triggers). Four rows: the four failed faucet draws. Signatures/authorisations log: `data/flop-signatures.log`. |
| Touchstone | Not configured and **not registered**. API is `https://api.touchstone.gold` (healthz ok from here, 3 Nov). Registration body fixed to `{did, account_ss58=spend account, label}`; owner chose to have Flopstar register itself (`flop touchstone register --post`, owner's go-ahead). Turns now use Touchstone's `aes-256-gcm/v1` and SDK `client_digest`. Needs the shared API token (out of band), a streak key we generate, and `check --spec` against Touchstone's `web/openapi.json` (FLOP.md §10, T-answers). |
| CLI runtime | Docker 29.1.3 (Ubuntu `docker.io`, updated by `unattended-upgrades`); pinned image `ghcr.io/flop-labs/flop-core/cli:v0.1.220@sha256:c8f3…bbbd` pulled and verified (`--version` → `0.1.220`; `session run` accepts every flag we pass). Every CLI container runs with `--memory 640m --memory-swap 1g --pids-limit 256`. |
| Host changes | Docker installed; `flopstar` in the `docker` group (§9); 2 GiB `/swapfile` (in `/etc/fstab`); kernel now `6.8.0-147-generic`. |

**Incident, 1 Nov 17:55 UTC.** The droplet (961 MiB RAM, no swap then) hard-reset while an uncapped
CLI container ran `--version` during the Docker check; it rebooted on its own at 17:56. No service
was running, so nothing was interrupted. Fixed with the swap file and the container caps above. Any
`docker run` here gets those caps and a `timeout`; if the CLI needs more, resize the droplet.

**What blocks real spending** (each needs FLOP's testnet to land or an owner decision):
- **Miner endpoints (S3):** `session run` needs a reachable miner session endpoint and none are
  published, so `[<net>.miners]` is empty.
- **Gateway key (S4):** the gateway needs a funded API key; the only published enrolment creates an
  unencrypted wallet.
- **Testnet details (S2/S5):** genesis hash, HTTPS RPC URL and the supported CLI build.
- **DID-signed flow (S1):** if testnet credits only requests signed with the TechnoCore DID, design A
  has to be revisited (FLOP.md §7).

**Next steps when testnet is live:** follow the go-live sequence at the end of FLOP.md §12, dry run
first at every step and each `--post` only with the owner's go-ahead: pin the testnet genesis,
`rpc_http_url`, miners and models in `/etc/flopstar/flop.toml`, then `status`, `faucet`, one `ask`,
`recovery`, Touchstone `check`/`register`, then decide on installing the task-runner and recovery
timers. Back up the spend keystore and passphrase before it holds testnet funds.

**Commands** (read-only unless noted; run as `flopstar` with the production environment):
```bash
FLOP="runuser -u flopstar -- env HOME=/var/lib/flopstar FLOPSTAR_DATA_DIR=/var/lib/flopstar/data \
  FLOPSTAR_FLOP_CONFIG=/etc/flopstar/flop.toml FLOPSTAR_FLOP_KEYSTORE=/var/lib/flopstar/flop/spend.json \
  /opt/flopstar/.venv/bin/flopstar flop"
$FLOP status                      # chain identity, health, spend balance, budget, kill switch
$FLOP config                      # resolved profile, caps, paths
$FLOP faucet                      # dry run; --post draws (needs the owner's go-ahead)
$FLOP recovery                    # channels holding escrow (none yet)
journalctl -u flopstar-flop-faucet.service          # faucet draw results
cat /var/lib/flopstar/data/KILL-flop                 # exists only if tripped; holds the reasons
```
Commands that need the spend passphrase (`wallet`/`ask`/`run`/`recovery --post`) run as a unit or
`systemd-run` with `LoadCredentialEncrypted=flop-spend-passphrase:/etc/flopstar/creds/flop-spend-passphrase.cred`
and `FLOPSTAR_FLOP_PASSPHRASE_FILE=${CREDENTIALS_DIRECTORY}/flop-spend-passphrase` (see
`deploy/flopstar-flop-run.service`).

