"""Touchstone reporting: buffer/replay order, private-network rule, idempotent digest, dry runs."""

import json
import os

import httpx
import pytest

from flopstar.flop import touchstone as ts
from flopstar.flop.cli import run_touchstone
from tests.flop_helpers import CENT, MINER, MODEL, make

DID = "did:key:z6MkjLpUAGLtNieLnCFQoUScwJxAKwo5PHZcHRsdyiCJG5Bv"
KEY = bytes(range(32))
REQ, RESP = b'{"prompt":"Summarise the day"}', b'{"reply":"A quiet day."}'


def resolver(table):
    def resolve(host, port):
        return [(None, None, None, "", (addr, port)) for addr in table[host]]
    return resolve


PRIVATE = resolver({"touchstone.tail1234.ts.net": ["100.101.102.103"]})
SETTINGS = ts.TouchstoneSettings("http://touchstone.tail1234.ts.net:8080", "tok-SECRET", KEY)


class Server:
    """httpx MockTransport recording requests; `codes` are returned in order, then 201."""

    def __init__(self, codes=(), raise_first=0):
        self.codes, self.raise_first, self.requests = list(codes), raise_first, []

    def __call__(self, request):
        if self.raise_first:
            self.raise_first -= 1
            raise httpx.ConnectError("connection refused")
        self.requests.append(request)
        return httpx.Response(self.codes.pop(0) if self.codes else 201)

    def transport(self):
        return httpx.MockTransport(self)

    def digests(self):
        return [json.loads(r.content)["client_digest"] for r in self.requests]


def no_network(request):
    raise AssertionError(f"unexpected network call: {request.method} {request.url}")


def spend_row(tmp_path, monkeypatch, verified=True):
    _, ledger, policy, _ = make(tmp_path, monkeypatch)
    import hashlib
    sid = policy.authorize_inference(route="direct", model=MODEL, miner=MINER, max_flop=CENT,
                                     purpose="contest-digest",
                                     request_sha256=hashlib.sha256(REQ).hexdigest())
    ledger.transition(sid, "sent", channel_id="0x" + "ab" * 32)
    ledger.transition(sid, "settled", settled_flop=CENT // 2, response_sha256=hashlib.sha256(
        RESP).hexdigest(), verification="sdk_verified" if verified else "unverified")
    return ledger, ledger.get(sid)


def turn(spend, **kw):
    args = {"cipher": ts.StreakCipher(KEY), "agent_did": DID, "spend": spend, "turn_index": 0,
            "request_bytes": REQ, "response_bytes": RESP, "token_ids": [1, 2, 3],
            "timings": {"started_at": "2026-11-03T12:00:00.000+00:00", "total_ms": 1234}, "receipt": {"verified_turns": 1},
            "sdk_version": "0.2.0",
            "recorded_at": "2026-11-03T12:00:00.000000+00:00"}
    return ts.build_turn(**{**args, **kw})


# ---- payloads -----------------------------------------------------------------------------------

def test_client_digest_is_touchstones_and_ciphertext_is_v1(tmp_path, monkeypatch):
    import base64
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _, spend = spend_row(tmp_path, monkeypatch)
    a, b = turn(spend), turn(spend)
    assert a["request_enc"] != b["request_enc"]  # fresh nonce each time
    # Touchstone SDK: sha256 hex of route|channel_id|turn_index|sha256hex(request)
    want = hashlib.sha256(("direct|0x" + "ab" * 32 + "|0|" + hashlib.sha256(REQ).hexdigest())
                          .encode()).hexdigest()
    assert a["client_digest"] == b["client_digest"] == want
    assert turn(spend, turn_index=1)["client_digest"] != want
    assert a["enc_scheme"] == "aes-256-gcm/v1"
    # decrypt by hand, as Touchstone's helper would: 0x01 || nonce(12) || ct || tag, AAD did\ndigest
    aad = f"{DID}\n{want}".encode()
    for field, plain in (("request_enc", REQ), ("response_enc", RESP)):
        raw = base64.b64decode(a[field])
        assert raw[0] == 1 and len(raw) == 1 + 12 + len(plain) + 16
        assert AESGCM(KEY).decrypt(raw[1:13], raw[13:], aad) == plain
    with pytest.raises(Exception):  # noqa: B017 - any AEAD failure
        ts.StreakCipher(KEY).decrypt(a["request_enc"], f"{DID}\n{'0' * 64}".encode())
    assert REQ.decode() not in json.dumps(a) and a["sdk_verification"] == {"ok": True}
    assert a["timings"] == {"started_at_ms": 1793707200000, "total_ms": 1234}
    assert a["sdk"] == {"name": "flop-sdk", "version": "0.2.0"}


def test_sdk_verdict_and_timings_mapping():
    assert ts.sdk_verdict("sdk_verified") == {"ok": True}
    assert ts.sdk_verdict("unverified") is None and ts.sdk_verdict(None) is None
    assert ts.sdk_verdict("failed")["ok"] is False
    with pytest.raises(ValueError, match="not an int"):
        ts.int_timings({"total_ms": 1.5})


def test_gateway_turns_get_distinct_digests():
    from flopstar.flop.inference import _turn_index
    assert _turn_index({"route": "direct", "id": 7}) == 0
    assert _turn_index({"route": "gateway", "id": 7}) == 7
    sha = "00" * 32
    assert ts.client_digest("gateway", None, 7, sha) != ts.client_digest("gateway", None, 8, sha)
    assert ts.client_digest("gateway", None, None, sha) == hashlib_hex(f"gateway|||{sha}")


def hashlib_hex(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def test_streak_key_file_formats():
    import base64
    assert ts.parse_streak_key(base64.b64encode(KEY) + b"\n") == KEY
    assert ts.parse_streak_key(KEY.hex().encode()) == KEY
    with pytest.raises(ts.TouchstoneConfigError):
        ts.parse_streak_key(base64.b64encode(KEY[:16]))


def test_build_turn_refuses_bytes_that_do_not_match_the_ledger(tmp_path, monkeypatch):
    _, spend = spend_row(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="request bytes"):
        turn(spend, request_bytes=b"something else")
    with pytest.raises(ValueError, match="response bytes"):
        turn(spend, response_bytes=b"tampered")
    with pytest.raises(ValueError, match="token_ids"):
        turn(spend, token_ids=[1.0])
    with pytest.raises(ValueError, match="channel_id"):
        turn({**spend, "channel_id": None})


def test_validate_rejects_undeclared_shapes():
    with pytest.raises(ValueError):
        ts.validate({"did": DID, "account_ss58": MINER}, ts.AGENT_FIELDS)
    with pytest.raises(ValueError):
        ts.validate({"did": DID, "account_ss58": 5, "label": "flopstar"}, ts.AGENT_FIELDS)


# ---- network path -------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://10.0.0.7:8080", "http://192.168.1.2", "http://100.64.0.1", "http://127.0.0.1:9000",
    "http://[fd7a:115c:a1e0::1]:8080", "http://touchstone.tail1234.ts.net:8080",
    "http://[::ffff:10.1.2.3]",
])
def test_private_plain_http_is_allowed(url):
    assert ts.check_url(url, PRIVATE) == []


@pytest.mark.parametrize("url, table", [
    ("http://203.0.113.9", {}),
    ("http://touchstone.example.com", {"touchstone.example.com": ["93.184.216.34"]}),
    ("http://split.example", {"split.example": ["10.0.0.1", "93.184.216.34"]}),
    ("ftp://10.0.0.1", {}),
    ("http://198.51.100.7", {}),  # documentation range: is_private in Python, public here
    ("http://169.254.169.254", {}),  # link-local / cloud metadata
    ("http://[::ffff:93.184.216.34]", {}),
])
def test_public_plain_http_is_refused(url, table):
    with pytest.raises(ts.UnsafeURL):
        ts.check_url(url, resolver(table))


def test_public_https_works_with_a_warning():
    warnings = ts.check_url("https://ts.example.com", resolver({"ts.example.com": ["93.184.216.34"]}))
    assert warnings and "private network" in warnings[0]


def test_reporter_never_sends_to_public_http(tmp_path):
    settings = ts.TouchstoneSettings("http://93.184.216.34", "tok", KEY)
    rep = ts.TouchstoneReporter(settings, tmp_path / "buf.jsonl",
                                transport=httpx.MockTransport(no_network))
    assert rep.report(1, {"client_digest": "x"}) == "buffered"
    assert "UnsafeURL" in rep.last_error and len(rep.buffered()) == 1


# ---- buffer and replay --------------------------------------------------------------------------

def test_failures_buffer_and_replay_in_order(tmp_path, monkeypatch):
    ledger, spend = spend_row(tmp_path, monkeypatch)
    server = Server(raise_first=3)
    rep = ts.TouchstoneReporter(SETTINGS, tmp_path / "buf.jsonl", ledger=ledger,
                                transport=server.transport(), resolve=PRIVATE)
    payloads = [turn(spend, turn_index=i) for i in range(3)]
    assert [rep.report(spend["id"], p) for p in payloads] == ["buffered"] * 3
    assert ledger.get(spend["id"])["touchstone_reported_at"] is None
    assert [e["payload"]["turn_index"] for e in rep.buffered()] == [0, 1, 2]
    # Touchstone is back: the next report sends the backlog first, oldest first.
    newest = turn(spend, turn_index=3)
    assert rep.report(spend["id"], newest) == "sent"
    assert server.digests() == [p["client_digest"] for p in [*payloads, newest]]
    assert rep.buffered() == [] and ledger.get(spend["id"])["touchstone_reported_at"]
    assert all(r.headers["authorization"] == "Bearer tok-SECRET" for r in server.requests)


def test_replay_stops_at_first_failure_and_new_reports_queue_behind(tmp_path):
    server = Server(codes=[201, 503])
    rep = ts.TouchstoneReporter(SETTINGS, tmp_path / "buf.jsonl",
                                transport=server.transport(), resolve=PRIVATE)
    rep._rewrite([{"spend_id": None, "path": ts.TURNS_PATH, "payload": {"client_digest": d}}
                  for d in "abc"])
    assert rep.report(None, {"client_digest": "d"}) == "buffered"
    assert server.digests() == ["a", "b"]
    assert [e["payload"]["client_digest"] for e in rep.buffered()] == ["b", "c", "d"]
    assert rep.replay() == 0 and server.digests() == ["a", "b", "b", "c", "d"]


def test_non_2xx_is_not_marked_reported(tmp_path, monkeypatch):
    ledger, spend = spend_row(tmp_path, monkeypatch)
    rep = ts.TouchstoneReporter(SETTINGS, tmp_path / "buf.jsonl", ledger=ledger,
                                transport=Server(codes=[422]).transport(), resolve=PRIVATE)
    assert rep.report(spend["id"], turn(spend)) == "buffered"
    assert rep.last_error == "HTTP 422"
    assert ledger.get(spend["id"])["touchstone_reported_at"] is None


def test_unconfigured_reporter_buffers_without_raising(tmp_path):
    rep = ts.TouchstoneReporter(None, tmp_path / "buf.jsonl",
                                transport=httpx.MockTransport(no_network))
    assert rep.report(None, {"client_digest": "z"}) == "buffered"


# ---- settings -----------------------------------------------------------------------------------

def secret(tmp_path, name, data: bytes, mode=0o600):
    path = tmp_path / name
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def env(tmp_path, token_mode=0o600, key=None):
    key = KEY.hex().encode() if key is None else key
    return {"TOUCHSTONE_API_URL": SETTINGS.api_url + "/",
            "TOUCHSTONE_API_TOKEN_FILE": str(secret(tmp_path, "token", b"tok-SECRET\n", token_mode)),
            "TOUCHSTONE_STREAK_KEY_FILE": str(secret(tmp_path, "streak", key))}


def test_settings_from_files(tmp_path):
    s = ts.TouchstoneSettings.from_env(env(tmp_path))
    assert s.token == "tok-SECRET" and s.streak_key == KEY and s.api_url == SETTINGS.api_url
    assert "SECRET" not in repr(s)
    assert ts.TouchstoneSettings.from_env(env(tmp_path, key=KEY)).streak_key == KEY


@pytest.mark.parametrize("change, match", [
    ({"TOUCHSTONE_API_TOKEN": "inline"}, "0600 file"),
    ({"TOUCHSTONE_API_URL": ""}, "TOUCHSTONE_API_URL"),
])
def test_unsafe_or_missing_settings_refused(tmp_path, change, match):
    with pytest.raises(ts.TouchstoneConfigError, match=match):
        ts.TouchstoneSettings.from_env({**env(tmp_path), **change})


def test_loose_file_modes_and_bad_keys_refused(tmp_path):
    with pytest.raises(ts.TouchstoneConfigError, match="0600"):
        ts.TouchstoneSettings.from_env(env(tmp_path, token_mode=0o644))
    with pytest.raises(ts.TouchstoneConfigError, match="32 bytes"):
        ts.TouchstoneSettings.from_env(env(tmp_path, key=b"short"))


# ---- OpenAPI diff -------------------------------------------------------------------------------

def openapi(turn_props, required):
    return {"info": {"version": "1"}, "paths": {ts.TURNS_PATH: {"post": {"requestBody": {"content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/Turn"}}}}}}},
        "components": {"schemas": {"Turn": {"type": "object", "properties": turn_props,
                                            "required": required}}}}


def test_openapi_diff_matches_and_mismatches():
    props = {k: {"type": v} for k, v in ts.TURN_FIELDS.items()}
    assert ts.diff_against_openapi(openapi(props, list(props)), ts.TURNS_PATH, ts.TURN_FIELDS) == []
    props2 = dict(props)
    props2.pop("timings")
    props2["turn_index"] = {"type": "string"}
    props2["streak_id"] = {"type": "string"}
    problems = ts.diff_against_openapi(openapi(props2, ["streak_id"]), ts.TURNS_PATH, ts.TURN_FIELDS)
    text = "\n".join(problems)
    assert "requires 'streak_id'" in text and "'timings'" in text and "'turn_index'" in text
    assert ts.diff_against_openapi({"paths": {}}, ts.TURNS_PATH, ts.TURN_FIELDS)


# ---- dry runs make no network calls -------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["register", "--chain-account", MINER],
    ["replay"],
    ["status"],
])
def test_dry_runs_send_nothing(tmp_path, monkeypatch, capsys, args):
    cfg, _, _, _ = make(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ts, "check_url", lambda url, resolve=None: [])
    run_touchstone(args, transport=httpx.MockTransport(no_network))
    out = capsys.readouterr().out
    assert "tok-SECRET" not in out
    assert args[0] == "status" or "dry run: nothing sent" in out


def test_register_post_sends_did_and_account(tmp_path, monkeypatch, capsys):
    cfg, _, _, _ = make(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ts, "check_url", lambda url, resolve=None: [])
    server = Server()
    run_touchstone(["register", "--chain-account", MINER, "--post"], transport=server.transport())
    body = json.loads(server.requests[0].content)
    assert server.requests[0].url.path == ts.AGENTS_PATH
    assert body == {"did": DID, "account_ss58": MINER, "label": "flopstar"}


def _register_env(tmp_path, monkeypatch):
    cfg, _, _, _ = make(tmp_path, monkeypatch)
    monkeypatch.setenv("FLOPSTAR_FLOP_CONFIG", str(cfg.config_path))
    for k, v in env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ts, "check_url", lambda url, resolve=None: [])


def test_repeat_registration_is_success_other_400s_are_not(tmp_path, monkeypatch, capsys):
    _register_env(tmp_path, monkeypatch)
    dup = httpx.MockTransport(lambda r: httpx.Response(400, json={"detail": "Agent already exists"}))
    run_touchstone(["register", "--chain-account", MINER, "--post"], transport=dup)
    assert "already registered" in capsys.readouterr().out
    bad = httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": "field required"}))
    with pytest.raises(SystemExit):
        run_touchstone(["register", "--chain-account", MINER, "--post"], transport=bad)


def test_check_reads_a_supplied_spec_without_network(tmp_path, monkeypatch, capsys):
    _register_env(tmp_path, monkeypatch)
    props = {k: {"type": v} for k, v in ts.TURN_FIELDS.items()}
    spec = openapi(props, list(props))
    spec["paths"][ts.AGENTS_PATH] = {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"type": "object", "required": ["did", "account_ss58"], "properties": {
            k: {"type": v} for k, v in ts.AGENT_FIELDS.items()}}}}}}}
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(spec))
    run_touchstone(["check", "--spec", str(path)], transport=httpx.MockTransport(no_network))
    assert "0 mismatches" in capsys.readouterr().out


def test_payloads_match_touchstones_published_spec():
    """Touchstone's web/openapi.json at main @ c00367e (production serves none)."""
    from pathlib import Path
    spec = json.loads((Path(__file__).parent / "fixtures/touchstone-openapi-c00367e.json").read_text())
    assert ts.diff_against_openapi(spec, ts.TURNS_PATH, ts.TURN_FIELDS) == []
    assert ts.diff_against_openapi(spec, ts.AGENTS_PATH, ts.AGENT_FIELDS) == []
