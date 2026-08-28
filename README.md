# simcoach

AI sim-racing coach for Le Mans Ultimate: captures laps live and answers
between-laps setup and driving questions in one conversation.

See `docs/superpowers/specs/2026-08-27-lmu-ai-coach-design.md` for the design.

## Dev setup

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```
