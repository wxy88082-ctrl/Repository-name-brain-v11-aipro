# Brain V11.1 Security Patch

## Fixed H-01: ambiguous TRADING_MODE parsing

Previous behavior treated these values as live mode:

- `TRADING_MODE=false`
- `TRADING_MODE=0`

V11.1 changes mode parsing to fail closed and safety-first:

- Only `TRADING_MODE=live` or `TRADING_MODE=real` can select live mode.
- `false`, `0`, `off`, empty, `paper`, `dryrun`, `simulation`, `mock` map to `paper`.
- `demo`, `testnet`, `test`, `sandbox` map to `demo`.
- Unknown values terminate startup with an explicit error.
- Live mode still requires both `LIVE_TRADING_CONFIRM=YES` and `USE_EXCHANGE_PROTECTION=1`.

## Recommended code-review scope

Please review source files only. Do not run code, do not connect APIs, do not request API keys, and do not execute trades.

Required transparent files:

- `brain_v11_1_aipro.py`
- `README.md`
- `requirements.txt`
- `SECURITY_REVIEW.md`
- `SECURITY_PATCH_V11_1.md`
- `brain_v11.env.example`

## Expected recommendation after H-01 fix

If no additional critical issues are found, this version should be considered only for paper/simulation testing, not demo or live trading.
