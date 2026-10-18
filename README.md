# Meme Narrative Collector

Open-source research collector for Pump.fun migrations, market snapshots, public
Solana activity, X posts, and human-reviewed meme-idea cards. It does **not**
trade, create tokens, verify the truth of X posts, or predict profit.

This repository contains source code and tests, **not** a backup of a running
installation. Databases, API keys, Telegram IDs, private watchlists, the full
keyword lexicon, OAuth sessions, and SSH credentials are deliberately excluded.
The included `x_keyword_terms.txt` is only a four-phrase example.

## Current authentication status

The AI analyzer in this version uses an **OpenAI API key with separate API
billing**. ChatGPT Plus does not turn `OPENAI_API_KEY` into a Plus-funded key.
The code does not yet implement Sign in with ChatGPT or its credential refresh
and streaming requirements. Merely making this repository public does not
switch an installed bot to Plus. Leave `AI_BOT_AUTO_ENABLED=false` until you
have consciously configured and tested an authorized inference method.

OpenAI documents ChatGPT plan usage for open-source clients, including a
self-hosted VM flow, with interactive OAuth consent, protected credentials,
account-specific model selection, and streaming Responses requests. A future
integration must implement those requirements and be tested under the chosen
account; do not paste passwords or browser cookies into this project.
See [open-source plan usage](https://developers.openai.com/siwc/token-sharing-open-source),
[self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
and [models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

## Local development

Requires Python 3.11 or newer. From this repository:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

On Windows, use `.venv\\Scripts\\python.exe` in place of `.venv/bin/python`.
Tests use fixtures and mocks; they should not need paid API calls.

To initialize an empty local database, copy `.env.example` to `.env`, set an
absolute `DATABASE_PATH` under a private `data/` directory, and run:

```bash
.venv/bin/meme-collector --env-dir . init-db
.venv/bin/meme-collector --env-dir . status
```

The collector's `run`, X-search, forensics, and AI commands can make metered
external calls. Review their settings, provider terms, and current limits before
enabling any service or timer. The historical `start_*.sh` scripts target an
existing Linux systemd deployment; they are **not** a fresh-server installer.
Do not run them unreviewed or use the sample keyword file for production.

## Privacy and security

- Never commit `.env`, API/OAuth credentials, SQLite databases, exports,
  logs, personal Telegram IDs, or private watchlists.
- Use dedicated least-privilege service accounts and restrictive file permissions
  for credentials and databases. Do not paste secrets into issues or chat.
- Public X posts and on-chain transactions are observations, not proof of who
  controls a wallet, why someone bought a token, or whether a narrative will win.
- The AI's coin name, ticker, and description are creative research drafts,
  not investment advice or endorsements.

License: [MIT](LICENSE).
