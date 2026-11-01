"""
Watch our compute channels and recover escrow with the permissionless calls (docs/FLOP.md §3.3, S8).

At the finalized head, for each channel in our ledger that may still hold escrow:
  PendingAck{ack_deadline}         block > ack_deadline            -> expire_force_open (refund)
  Open                             block > opened_at + max_duration -> timeout (refund)
  Settling{challenge_end}          block > challenge_end            -> finalize (pay + refund rest)
  Closed                           nothing to do
Layout from the devnet runtime metadata (pallet_compute_channel Channel / ChannelState).

These calls only return our own escrow, so with --post Flopstar submits them itself through
FlopPolicy (which allows exactly these three calls, for channels in our ledger, after a genesis
check). Without --post it prints the command and deadline for a human. Every recovery is an event in
flop_spend_events.
"""

from dataclasses import dataclass
from datetime import timedelta

from .client import Chain, ChannelView
from .config import FlopConfig
from .policy import ActionKind, FlopPolicy

LOOKBACK = timedelta(days=7)


@dataclass
class Item:
    spend_id: int | None
    channel_id: str
    state: str
    call: str | None  # due now
    deadline: int | None  # block after which the call becomes valid
    block: int
    escrow: int | None
    note: str = ""

    @property
    def blocks_left(self) -> int | None:
        return None if self.deadline is None else max(0, self.deadline + 1 - self.block)


def decide(view: ChannelView, block: int) -> tuple[str | None, int | None, str]:
    """(call due now or None, deadline block, explanation)."""
    if view.state == "PendingAck":
        d = view.ack_deadline
        return ("expire_force_open" if block > d else None), d, "unacknowledged force_open"
    if view.state == "Open":
        d = view.opened_at + view.max_duration
        return ("timeout" if block > d else None), d, "open past its SLA refunds via timeout"
    if view.state == "Settling":
        d = view.challenge_end
        return ("finalize" if block > d else None), d, "force-settled; finalize after challenge"
    return None, None, "closed"


def scan(cfg: FlopConfig, policy: FlopPolicy, chain: Chain, agent: str | None) -> list[Item]:
    ledger, net = policy.ledger, cfg.network.name
    head, block = chain.finalized()
    items: list[Item] = []
    known: set[str] = set()
    for row in ledger.open_channels(net):
        cid = row["channel_id"].lower()
        known.add(cid)
        view = chain.channel(cid, head)
        if view is None:
            items.append(Item(row["id"], cid, "missing", None, None, block, None,
                              "not in finalized state (never opened, or not yet final)"))
            continue
        call, deadline, note = decide(view, block)
        if agent and view.agent != agent:
            call, note = None, f"MISMATCH: channel paid by {view.agent}, not our {agent}"
        elif view.state == "Closed" and row["status"] != "recovering":
            note = f"closed on chain; ledger status {row['status']} (outcome not recorded by CLI)"
        items.append(Item(row["id"], cid, view.state, call, deadline, block, view.escrow, note))
    # Runs that died before learning their channel (e.g. a CLI timeout): look for our channels
    # that the ledger does not know.
    if agent and ledger.unchanneled_failures(net, policy.now() - LOOKBACK):
        for view in chain.channels_by_agent(agent, head):
            if view.channel_id in known or view.state == "Closed":
                continue
            call, deadline, note = decide(view, block)
            items.append(Item(None, view.channel_id, view.state, call, deadline, block,
                              view.escrow, "ours on chain but not in the ledger: " + note))
    return items


def attach_orphans(policy: FlopPolicy, items: list[Item]) -> list[Item]:
    """
    If exactly one unchanneled failed request and exactly one orphan channel exist, they are the
    same session: record the channel on that row (fill-once) so recovery is allowed. Otherwise a
    human must decide; nothing is guessed.
    """
    orphans = [i for i in items if i.spend_id is None]
    rows = policy.ledger.unchanneled_failures(policy.network, policy.now() - LOOKBACK)
    if len(orphans) == 1 and len(rows) == 1:
        row, item = rows[0], orphans[0]
        status = "recovering" if row["status"] == "failed" else "failed"
        policy.ledger.transition(row["id"], status, {"attached_channel": item.channel_id},
                                 channel_id=item.channel_id)
        item.spend_id, item.note = row["id"], f"attached to spend {row['id']}; " + item.note
    return items


def human_command(cfg: FlopConfig, item: Item) -> str:
    if item.call == "finalize":
        return (f"flop --network {cfg.network.name} --rpc {cfg.network.rpc_url} --keystore "
                f"{cfg.keystore_path} --password-env FLOP_KEY_PASSWORD channel finalize "
                f"{item.channel_id}")
    return f"flopstar flop recovery --post   # ComputeChannel.{item.call}({item.channel_id})"


def recover(cfg: FlopConfig, policy: FlopPolicy, chain: Chain, keypair, items: list[Item]) -> list[dict]:
    """Submit every due call through FlopPolicy. Stops at the first failure (which trips the switch)."""
    ledger, done = policy.ledger, []
    for item in items:
        if item.call is None or item.spend_id is None:
            continue
        row = policy.authorize_recovery(item.call, item.channel_id)
        if row["status"] in ("sent", "failed"):
            ledger.transition(row["id"], "recovering", {"call": item.call})
        tx = policy.submit_extrinsic(ActionKind.RECOVERY, "ComputeChannel", item.call,
                                     {"channel_id": item.channel_id},
                                     substrate=chain.substrate, keypair=keypair)
        outcome = "settled" if item.call == "finalize" else "refunded"
        fill = {"settlement_tx": tx} if row.get("settlement_tx") is None else {}
        ledger.transition(row["id"], outcome, {"call": item.call, "tx": tx}, **fill)
        done.append({"spend_id": row["id"], "channel": item.channel_id, "call": item.call,
                     "tx": tx, "status": outcome})
    return done
