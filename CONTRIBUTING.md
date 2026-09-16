# Contributing

Issues and pull requests are welcome.

1. Fork and branch from `main` (`feature/…` or `fix/…`).
2. Keep notebooks self-contained; clear outputs before committing.
3. Do not commit `.env`, API keys, `data/tts_*.csv`, or `*.duckdb`.
4. Run `pytest test_solar_economics.py test_integration.py -v` before opening a PR.

Commit messages should say what changed and why, for example:

```
Wire Tracking the Sun sample sizes into live quotes
Fix Nominatim state names so Oregon resolves to OR
```
