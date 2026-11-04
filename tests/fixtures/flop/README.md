# `flop session run` fixtures — SYNTHETIC

These are **synthetic**: shaped from the `@flop-labs/cli` 0.4.0 source (`dist/cli.js`
5571-5580 for success output, 5045-5063 for `commandError`, and `@flop-labs/sdk`
`compute-channel/direct-session.js` 10-16 / 188-205 for `DirectSessionError` messages), not
captured from a real run. No devnet session has been possible from the droplet yet (docs/FLOP.md §4).
Replace each with real captured output (redacting nothing secret: the CLI prints no secrets) the
first time a devnet session runs, and drop `SYNTHETIC` from the file name.

## `qwen2.5/` — real

`tokenizer.json.gz` is `tokenizer.json` from `Qwen/Qwen2.5-0.5B-Instruct` on Hugging Face
(downloaded 4 Nov 2026, sha256 of the uncompressed file
`c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539`), gzipped. `LICENSE` is that
repo's Apache-2.0 license. `tests/test_flop_estimate.py` uses it to check that
`estimate.TEMPLATE_TOKENS` covers the chat template the miner wraps around one user message.
