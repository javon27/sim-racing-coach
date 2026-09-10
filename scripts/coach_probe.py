"""Ask the coach a question and record EVERY tool it calls, plus the answer.

The chat UI shows the current tool inline ("reading your lap (get_lap_analysis)…")
but overwrites it as each one fires, so a 9-tool turn is unreadable live. This
records the full sequence, which is what #2 and #1 actually need:

  #2  does a race/session-wide question route to the SESSION-WIDE tools
      (list_recent_laps, get_session_status) rather than the selected lap?
  #1  does the coach LEAD with the race story and weight racecraft over pace?

Needs the sidecar running (`python -m simcoach.server`, or just have the desktop
app open) and a configured API key. EVERY run spends real API credit -- one
agentic turn re-sends the tool stack up to 9 times, so don't loop it.

Usage:
    .venv/Scripts/python.exe scripts/coach_probe.py "how's my race going?"
    .venv/Scripts/python.exe scripts/coach_probe.py --lap 7114 "what did I lose in T3?"

Writes nothing. Prints a transcript to paste back.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import websockets
except ImportError:
    print("needs `websockets`: .venv/Scripts/python.exe -m pip install websockets")
    raise SystemExit(2) from None

PORT = 8765


async def ask(question: str, lap_id: int | None, session_id: str | None) -> int:
    url = f"ws://127.0.0.1:{PORT}/coach"
    # The ws route enforces the same Host/Origin guard as the HTTP middleware
    # (app.py:1281), so send an allowed Origin or it closes with 1008 before accept.
    headers = {"Origin": f"http://127.0.0.1:{PORT}"}
    tools: list[dict] = []
    answer: list[str] = []
    usage: list[dict] = []
    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"question": question,
                                      "lap_id": lap_id,
                                      "session_id": session_id}))
            async for raw in ws:
                ev = json.loads(raw)
                kind = ev.get("type")
                if kind == "tool":
                    tools.append(ev)
                    print(f"  [tool {len(tools):>2}] {ev.get('name')}  "
                          f"{json.dumps(ev.get('input') or {}, ensure_ascii=False)[:160]}")
                elif kind == "text":
                    answer.append(ev.get("text") or "")
                elif kind == "usage":
                    usage.append(ev)
                elif kind == "error":
                    print(f"  [error] {ev.get('message')}")
                    return 1
                elif kind == "done":
                    break
    except OSError as exc:
        print(f"cannot reach the sidecar on {url} -- is it running? ({exc})")
        return 1

    print("\n" + "=" * 78)
    print(f"QUESTION: {question}" + (f"   (lap_id={lap_id})" if lap_id else ""))
    print("=" * 78)
    print(f"\nTOOLS CALLED, in order ({len(tools)}):")
    for i, t in enumerate(tools, 1):
        print(f"  {i:>2}. {t.get('name')}")
    if not tools:
        print("  (none -- the coach answered without reading anything)")

    names = [t.get("name") for t in tools]
    session_wide = {"list_recent_laps", "get_session_status", "get_race_result",
                    "get_driver_memories", "get_last_setup"}
    lap_scoped = {"get_lap_analysis", "get_corner", "get_input_traces"}
    hit_session = sorted(session_wide.intersection(names))
    hit_lap = sorted(lap_scoped.intersection(names))
    print(f"\n  session-wide tools used: {hit_session or 'NONE'}")
    print(f"  lap-scoped tools used:   {hit_lap or 'NONE'}")
    print("  READ (#2): a race/session-wide question should reach for the session-wide")
    print("             set. Lap-scoped tools alongside them are fine; lap-scoped ONLY")
    print("             means the routing fix did not take.")

    print("\nANSWER:\n")
    print("".join(answer).strip() or "(empty)")
    if usage:
        u = usage[-1]
        print(f"\n[usage] {json.dumps({k: v for k, v in u.items() if k != 'type'})}")
    print("\n" + "=" * 78)
    print("Paste from QUESTION down.")
    return 0


def main(argv: list[str]) -> int:
    lap_id = None
    session_id = None
    args = []
    i = 0
    while i < len(argv):
        if argv[i] == "--lap" and i + 1 < len(argv):
            lap_id = int(argv[i + 1]); i += 2
        elif argv[i] == "--session" and i + 1 < len(argv):
            session_id = argv[i + 1]; i += 2
        else:
            args.append(argv[i]); i += 1
    if not args:
        print(__doc__)
        return 2
    return asyncio.run(ask(" ".join(args), lap_id, session_id))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
