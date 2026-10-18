# Meme Narrative Collector

Open-source research collector for Pump.fun migrations, market snapshots, public
Solana activity, X posts, and human-reviewed meme-idea cards. It does **not**
trade, create tokens, verify the truth of X posts, or predict profit.

This repository contains source code and tests, **not** a backup of a running
installation. Databases, API keys, Telegram IDs, private watchlists, the full
keyword lexicon, OAuth sessions, and SSH credentials are deliberately excluded.
The included `x_keyword_terms.txt` is only a four-phrase example.

## ChatGPT Plus sign-in (opt-in)

The bot has two explicit AI modes. `AI_AUTH_MODE=api_key` uses a separately
billed OpenAI API key. `AI_AUTH_MODE=chatgpt_plan` uses the official Sign in with
ChatGPT flow for an eligible ChatGPT account and the account's authorized plan
usage. It is **not** a free or unlimited API key: OpenAI's account and app
limits still apply. Neither a public repository nor an existing Plus
subscription switches a running VPS automatically.

For the plan mode, install the package on the computer where you will complete
the browser sign-in. Run `meme-chatgpt-auth login` there, not in an SSH session;
the callback is on `127.0.0.1` of the machine with the browser. Approve the
app's plan-usage permission in the official OpenAI page. `meme-chatgpt-auth
status` displays the connected account without exposing tokens. Use
`meme-chatgpt-auth models` to list model slugs available to this account, then
set `OPENAI_MODEL` to a listed slug.

For a self-hosted VPS, install the same package there, run
`meme-chatgpt-auth --credentials /PRIVATE/PATH/chatgpt-credentials.json host-init`
on the VPS to create its own persistent host ID, and securely copy only the
protected credential JSON from your local computer to that path over SSH.
Ensure the file is owned by the service account, readable only by it (mode
`0600`), and never copy the laptop's `chatgpt-host-id`. Configure:

```dotenv
AI_AUTH_MODE=chatgpt_plan
CHATGPT_CREDENTIALS_FILE=/PRIVATE/PATH/chatgpt-credentials.json
OPENAI_API_KEY=
OPENAI_MODEL=MODEL_SLUG_FROM_MODELS_COMMAND
```

The VPS then owns access-token refreshes. Keep the AI timer off until `status`
and `models` succeed **as the service account**, and test one bounded analysis
before enabling automatic alerts. Only the X→AI→Telegram analyzer currently
uses the plan mode; the optional migration-learning worker still uses its
separately billed API-key path. Sign-in and inference have not been exercised
against this user's account or VPS in this source release.

Do not paste OAuth tokens, ChatGPT passwords, cookies, or the credential file
into a chat, issue, or repository. The code requests no ChatGPT conversations.
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
