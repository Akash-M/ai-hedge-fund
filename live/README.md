# Live eToro Trading Bridge

This package connects the AI Hedge Fund decision engine (`src/`) to a **real
eToro account** via eToro's public trading API. The base project only *generates*
orders ("the system does not actually make any trades"); this bridge **executes**
them, with hard risk controls and a demo-first safety workflow.

> ⚠️ **Risk.** Automated trading can lose money fast. This started as an
> educational proof-of-concept. You trade entirely at your own risk. **Validate on
> the demo (paper) account for weeks before risking a cent of real capital.**

## How it works (one cycle)

```
 eToro portfolio ──▶ scale to YOUR budget ──▶ AI Hedge Fund agents ──▶ decisions
        ▲                                                                   │
        │                                                                   ▼
   execute orders ◀── eToro REST API ◀── deterministic risk guardrails ◀── cap & size
```

1. **Read** the live eToro portfolio (also the credential health-check).
2. **Build** the hedge fund's `portfolio` dict, scaled to your managed budget (so it
   sizes against *your* money, not the demo account's full balance).
3. **Run** the agents (Buffett, Wood, Burry, … + Risk & Portfolio managers).
4. **Guardrails** convert share decisions → capped, cash-denominated orders.
5. **Execute** on eToro (or dry-run), log everything, optionally notify.

Each cycle reconciles against the live portfolio, so a retried/crashed run converges
instead of double-trading.

## Components

| File | Role |
|------|------|
| `config.py` | Env-driven settings + risk-profile presets |
| `etoro_client.py` | eToro REST wrapper (auth, retries, demo/real switch) |
| `portfolio_sync.py` | eToro positions → hedge-fund portfolio dict; budget scaling |
| `risk_guardrails.py` | **Deterministic** hard caps (the final say on every order) |
| `executor.py` | Places/closes orders; one failure never aborts the cycle |
| `run_cycle.py` | Orchestrates one cycle (the entrypoint) |
| `state_store.py` / `report.py` | Append-only cycle log + review tool |
| `notifier.py` | Logs + optional Slack/Discord/webhook summary |

## Safety model

- **Dry-run by default** (`AIHF_DRY_RUN=true`): computes and logs orders, sends nothing.
- **Demo-first** (`ETORO_ENVIRONMENT=demo`): identical API, paper money.
- **Hard guardrails** (even on `aggressive`): max % per position, max # positions,
  max % invested, min order size, confidence floor, stop-loss, **no leverage**, long-only.
- **Live gate**: real-money trading requires BOTH `ETORO_ENVIRONMENT=real`,
  `AIHF_DRY_RUN=false`, AND `AIHF_CONFIRM_LIVE=I_UNDERSTAND`.

## Multi-provider LLM failover

19 agents per ticker hits OpenAI rate limits fast. `live/llm_failover.py` wraps the
app's single `call_llm` chokepoint with an ordered **provider chain** and fails over
on 429 / quota / overloaded errors — installed by monkeypatch *before* the agents
import, so no `src/` file is edited (keeps `main` syncable).

- Set keys for any providers you have; the chain auto-builds from what's present.
- **Best free fallbacks:** **Groq** (genuine free tier, high rate limits, fast) and
  **Gemini** (free tier). Anthropic works but is pay-as-you-go (no perpetual free tier).
- After a provider 429s it's benched for `AIHF_LLM_COOLDOWN_S` (default 90s) so the
  next agents skip straight to a healthy provider instead of re-hitting the limit.
- Override the order explicitly with `AIHF_LLM_FALLBACKS`, e.g.
  `OpenAI:gpt-4.1-mini,Groq:llama-3.3-70b-versatile,Google:gemini-2.5-flash`.

Each cycle log records the active `llm_chain`. Tested in `tests/test_llm_failover.py`.

## Prerequisites

1. **eToro API keys** — generate at <https://www.etoro.com/settings/trade>
   (`x-api-key` and `x-user-key`). Enable **demo** first.
2. **Financial data key** — <https://financialdatasets.ai/> (`FINANCIAL_DATASETS_API_KEY`).
3. **LLM key** — e.g. `OPENAI_API_KEY`. A cheap model (`gpt-4.1-mini`) is the default.

## Local quick start

```bash
poetry install --only main          # from repo root
cp live/.env.example .env           # fill in your keys; keep AIHF_DRY_RUN=true

# Dry-run a real cycle against your DEMO account (no orders sent):
poetry run python -m live.run_cycle --print

# Test the execution path without spending on the LLM (inject decisions):
echo '{"decisions":{"NVDA":{"action":"buy","quantity":10,"confidence":80,"reasoning":"test"}}}' > d.json
AIHF_DECISIONS_FILE=d.json poetry run python -m live.run_cycle --print

# Review recent cycles:
poetry run python -m live.report --n 20
```

Run the offline test suite (no keys needed):

```bash
PYTHONPATH=. python live/tests/test_guardrails.py
PYTHONPATH=. python live/tests/test_cycle_offline.py
```

## Hosting

### Option A — GitHub Actions (recommended: free + simplest)
The repo is already on GitHub. `.github/workflows/trade.yml` runs the cycle on a
schedule for free.
1. Repo → **Settings → Secrets and variables → Actions** → add `ETORO_API_KEY`,
   `ETORO_USER_KEY`, `FINANCIAL_DATASETS_API_KEY`, `OPENAI_API_KEY` (+ optional
   `AIHF_NOTIFY_WEBHOOK`). Keep `ETORO_ENVIRONMENT=demo`.
2. It runs weekdays ~19:30 UTC in **dry-run**. Watch the **Actions** logs.
3. Go live later via **Run workflow → mode: live** (after setting the real-money secrets).

### Option B — Render Cron (managed, cheap)
Use `live/render.yaml` as a Blueprint; set secrets in the dashboard. Billed per run.

### Option C — Docker on any VPS
```bash
docker build -f live/Dockerfile -t ai-hedge-fund-live .
docker run --rm --env-file .env -v "$PWD/live_state:/app/live_state" ai-hedge-fund-live
# then add a crontab entry, e.g.:  30 19 * * 1-5  docker run --rm ...
```

## Run history & progress tracking

Each cycle writes to the state dir (`AIHF_STATE_DIR=runs` in CI):
- `runs/run-<timestamp>.json` — one committed log file per run
- `runs/cycles.jsonl` — consolidated append-only history
- `runs/latest.json` — most recent cycle snapshot
- `runs/index.html` — self-contained equity-curve + trade-log report, regenerated each run

On GitHub Actions each run **commits the new `runs/` files back to the branch**, so the
repo itself is the durable, versioned history — no database, no separate branch. Open
`runs/index.html` locally or serve it via GitHub Pages for an at-a-glance view.
Your **eToro app stays the authoritative portfolio view**; this adds the equity trend
over time plus the agent's per-trade reasoning. Regenerate locally with
`python -m live.report_html --state-dir runs`.

## Demo → live checklist

- [ ] Dry-run cycles produce sane orders & sizes in the logs.
- [ ] Flip `AIHF_DRY_RUN=false` on **demo** — confirm orders appear in the eToro demo account.
- [ ] Let it run on demo for the agreed period; review `report.py` / paper PnL.
- [ ] Confirm guardrails (per-position, max positions, stop-loss) behave as expected.
- [ ] Set your real budget, fund the account, set `ETORO_ENVIRONMENT=real`,
      `AIHF_CONFIRM_LIVE=I_UNDERSTAND`, start with a small allocation, monitor closely.

## Disclaimer
Educational software. Not investment advice. No warranty. You are solely responsible
for any trades placed and any losses incurred. Comply with eToro's API terms.
