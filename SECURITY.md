# Security Policy & FLOP Network Alignment

## Alignment with FLOP Labs

Flopstar is an open-source reference agent built in full alignment with the **FLOP Labs** team
and the security, reliability, and fairness of the **FLOP Network**.

Our operating principles on FLOP Devnet, Testnet, and Mainnet:

1. **Responsible Disclosure First:** If our operators, automated diagnostics (`flop scout`,
   `flop watchtower`, `flop audit`), or code audits uncover any security vulnerability in the FLOP
   chain, runtime, SDK, CLI, gateway, faucet, or miner software, we report it **privately to
   `security@flop.finance`** in accordance with [flop.finance/testnet](https://flop.finance/testnet/).
   We never exploit vulnerabilities and never publish exploit details prior to remediation.
2. **Constructive Bug Reporting:** Non-security integration issues, SDK/runtime mismatches,
   documentation discrepancies, and miner or settlement anomalies are filed constructively with
   minimal, secret-free, reproducible traces on [flop-labs/network](https://github.com/flop-labs/network).
3. **Fair Play & Independent Demand:** Flopstar operates under a single identity (`flopstar.did`)
   and a single capped spend account, enforces a strict `same_control_miners` blocklist so spend
   never routes to common-control miners, deduplicates prompts, and generates genuine, useful
   inference workloads backed by encrypted Touchstone audit records.
4. **Strict Key & Spend Hygiene:** Private keys, passphrases, and API credentials are never
   committed to git, logged, passed to third-party webapps, or exposed to LLMs. All signing and
   spending passes through a single deterministic policy gate (`src/flopstar/flop/policy.py`).

## Reporting a Vulnerability in Flopstar

If you discover a security issue in this repository (`oversight-markets/flopstar`):

- **Do not** open a public GitHub issue with exploit details or key material.
- Report it privately via GitHub Security Advisories on this repository or contact the maintainer
  directly (`fode@alchemexlabs.com`) with reproduction steps and affected commit/file paths.
- Please include only public identifiers (never include private keys, seed phrases, keystore files,
  or bearer tokens).
