# Ai Pro Code Review Request for Brain V11

Please review this GitHub repository as source code only.

Do **not** execute anything.
Do **not** download and run any zip file.
Do **not** connect to Binance API.
Do **not** request API keys.
Do **not** place paper, demo, or live trades.

Please inspect these files:

1. `brain_v11_aipro.py`
2. `brain_v11.env.example`
3. `README.md`
4. `SECURITY_REVIEW.md`
5. `requirements.txt`

Review goals:

1. Confirm whether `TRADING_MODE=paper` is the default.
2. Confirm whether live trading requires explicit confirmations.
3. Check for hardcoded secrets or API keys.
4. Check for dangerous dynamic execution such as `eval`, `exec`, `subprocess`, shell commands, or remote code execution.
5. Check whether AI decisions can bypass risk controls.
6. Check whether any order can be placed without stop loss / risk limit.
7. Check for repeated opening, infinite loop, or excessive position risk.
8. Check whether this is suitable for paper/simulation testing only.

Return JSON only:

```json
{
  "can_review": true,
  "can_execute": false,
  "safety_score": 0,
  "critical_issues": [],
  "high_risk_issues": [],
  "medium_risk_issues": [],
  "paper_mode_ok": false,
  "live_trading_risk": "",
  "recommendation": "APPROVE_FOR_PAPER / NEEDS_FIX / REJECT",
  "notes": []
}
```
