# Contributing

Thanks for looking at World Cup Pilot. Issues and pull requests are welcome.

## Set up

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python server.py          # server only: http://127.0.0.1:8770
.venv\Scripts\python worldcup.py        # desktop window
```

On macOS use `python3 -m venv .venv`, `.venv/bin/pip` and `.venv/bin/python`, and build with `./build_mac.sh`.
Platform-specific code (notifications, taskbar badge, icons) belongs in `worldcup.py` behind `sys.platform` checks.

## Before you open a pull request

```bash
python -m py_compile server.py worldcup.py backtest.py learn.py
python test_model_consistency.py
```

The prediction math exists in four places (`worldcup.html`, `server.py`, `backtest.py`, `learn.py`) and the test
fails if they drift apart. If you change one, change the others.

## Ground rules

- Cache what cannot change anymore (finished matches, past tournaments); call an external API only when the data can
  actually change. The free services have rate limits.
- Do not commit third-party data (ESPN, TheSportsDB, ... responses), photos or FIFA logos. `dist/cache/` and
  `assets/logo.png` are git-ignored on purpose.
- Keep pull requests small and say how you checked them.
