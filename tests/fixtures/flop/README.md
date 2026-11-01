# `flop session run` fixtures — SYNTHETIC

These are **synthetic**: shaped from the `@flop-labs/cli` 0.4.0 source (`dist/cli.js`
5571-5580 for success output, 5045-5063 for `commandError`, and `@flop-labs/sdk`
`compute-channel/direct-session.js` 10-16 / 188-205 for `DirectSessionError` messages), not
captured from a real run. No devnet session has been possible from the droplet yet (docs/FLOP.md §4).
Replace each with real captured output (redacting nothing secret: the CLI prints no secrets) the
first time a devnet session runs, and drop `SYNTHETIC` from the file name.
