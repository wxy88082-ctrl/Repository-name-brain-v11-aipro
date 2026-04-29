# Security Review Notes

This repository should be reviewed as source code only.

## Do Not Execute During Review

- Do not run the Python script.
- Do not download or execute zip files.
- Do not connect to Binance APIs.
- Do not request or use API keys.
- Do not place orders.

## Required Checks

Please inspect `brain_v11_aipro.py` for:

1. Hardcoded API keys, secrets, tokens, or private URLs.
2. Dangerous use of `eval`, `exec`, `subprocess`, shell commands, dynamic imports, or remote code execution.
3. External downloads followed by execution.
4. Live trading gates and whether live mode can be accidentally triggered.
5. Whether paper mode is the default.
6. Whether orders can be placed without stop loss / protection rules.
7. Whether AI decisions can bypass risk controls.
8. Infinite loops, repeated entry bugs, or missing position limits.
9. Excessive file writes or suspicious filesystem access.
10. Network destinations and whether they are limited to intended market/API endpoints.

## Expected Safe Design

- Paper/simulation mode should be the default.
- Live mode should require multiple explicit confirmations.
- No secrets should be present in the repository.
- Risk governor should be final authority.
- AI should rank/choose candidates only; it should not create arbitrary trades.

## Review Output Format

Please output JSON:

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
