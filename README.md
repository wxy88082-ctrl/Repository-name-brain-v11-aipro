# Brain V11 Ai Pro - Transparent Review Repository

This repository is for **code review only**. Do not execute this code inside an AI chat agent.

## Files

- `brain_v11_aipro.py` — visible Python source code for review.
- `brain_v11.env.example` — example environment variables only; contains no secrets.
- `requirements.txt` — minimal Python dependency list.
- `SECURITY_REVIEW.md` — security boundaries and review checklist.
- `aipro_code_review_request_v11.md` — prompt for Binance Ai Pro to review the repository without executing anything.

## Safety Defaults

- Default trading mode is paper/simulation.
- Live mode is intended to require explicit environment confirmations.
- No API keys or secrets should be committed to this repository.
- The AI layer must not modify entry price, stop loss, take profit, leverage, quantity, or risk budget.
- Risk controls must have final veto power.

## Important

This project is a research and simulation framework. It does not guarantee profitability. Futures trading is high risk.

## Source Integrity

Expected SHA256 for `brain_v11_aipro.py`:

```text
fc0bf167d836f882e1e80b7b3cfddb069e7db9974f57f16c35cd4a77c583de1a
```
