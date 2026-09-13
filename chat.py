"""Terminal version of the ADK stock chatbot.

    python chat.py                       interactive
    python chat.py "stock of diesel"     one question, then exit
"""
from __future__ import annotations

import sys

from agent import Chat

try:  # Windows consoles default to cp1252; the model writes UTF-8 punctuation
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def _printer():
    state = {"in_text": False}

    def emit(ev: dict) -> None:
        t = ev["type"]
        if t == "tool":
            if state["in_text"]:
                print()
                state["in_text"] = False
            label = ev.get("purpose") or ev["name"]
            print(f"  > {label}")
            if ev.get("sql"):
                for line in ev["sql"].strip().splitlines():
                    print("      " + line)
        elif t == "result":
            print(f"    {ev['rowcount']} rows{' (capped)' if ev['truncated'] else ''} in {ev['elapsed']}s")
        elif t == "sql_error":
            print(f"    ! {ev['text']}")
        elif t == "text_delta":
            if not state["in_text"]:
                print()
                state["in_text"] = True
            print(ev["text"], end="", flush=True)
        elif t == "text_break":
            print()
            state["in_text"] = False
        elif t == "error":
            print(f"\n!! {ev['text']}")
        elif t == "final":
            print(
                f"\n\n[${ev['cost']:.4f} this question, ${ev['session_cost']:.4f} this session, "
                f"${ev['spent_today']:.2f} today of ${ev['budget']:.2f}, {ev['elapsed']}s]"
            )
            state["in_text"] = False

    return emit


def main() -> None:
    chat = Chat()
    st = chat.status()
    print(f"ADK stock chatbot - {st['model']} ({st['effort']}) - {st['db']}")
    emit = _printer()
    if len(sys.argv) > 1:
        chat.ask(" ".join(sys.argv[1:]), emit)
        return
    print("Type a question (blank line or 'exit' to quit).\n")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q or q.lower() in ("exit", "quit", "q"):
            break
        chat.ask(q, emit)
        print()


if __name__ == "__main__":
    main()
