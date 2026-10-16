"""Play a walkthrough file through the real engine and report how it went.

    python3 claudventure/walkthrough.py games/adv01.dat claudventure/walkthroughs/adv01.txt [--seeds N] [--seed FIRST] [-v] [--map]

The game is won when the engine prints "Well done" (all treasures stored), or
the text given in the walkthrough by a line like '# win: You have saved the
world'. --map draws the map of the places visited, as the player would see it.

Line formats: 'CMD' must work; '? CMD' is only needed in some cases (its
warnings are ignored); 'if here "X": CMD' runs CMD only when an item whose name
contains X is in the room ('carrying "X"': carried, 'said "X"': in the last
command's output). Conditions can be negated with 'not' and joined with 'and'
('if not carrying "Postcard" and not said "DING-DONG": LOOK'); 'unless C' is
'if not C'. 'until C: CMD, CMD' repeats the commands in turn until C holds
(checked before each one), for random destinations. These handle the games'
random events.
"""

import re
import sys
from pathlib import Path

import scott
from automap import AutoMap
from engine import Engine

WARN = ("dead", "can't do", "can't go", "sorry i can't", "beyond my power", "don't understand",
        "too much", "don't know", "nothing to light", "what ?", "dangerous", "not here",
        "don't see", "won't let me", "won't fit", "something is missing")


COND = re.compile(r'^(if|unless|until) (.+?):\s*([A-Z0-9].*)$', re.I)
TEST = re.compile(r'^(not )?(here|carrying|said) "([^"]+)"$', re.I)
MAX_ROUNDS = 100  # commands an 'until' line may send


def commands(path):
    """(command, optional, conditions, loop) for each line; conditions: [(negated, kind,
    text)]; loop is the list of commands of an 'until' line (its conditions say when to stop)."""
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = COND.match(line)
        if m:
            tests = [TEST.match(t.strip()) for t in m.group(2).split(" and ")]
            if not all(tests):
                raise ValueError(f"bad condition: {line}")
            kind = m.group(1).lower()
            conds = [(bool(t.group(1)) != (kind == "unless"), t.group(2).lower(), t.group(3).lower())
                     for t in tests]
            if kind == "until":
                loop = [c.strip() for c in m.group(3).split(",")]
                yield loop[0], True, conds, loop
            else:
                yield m.group(3).strip(), True, conds, None
        else:
            yield line.lstrip("? ").strip(), line.startswith("?"), None, None


def holds(conds, state, last):
    for negated, kind, text in conds:
        if kind == "said":
            found = text in last.lower()
        else:
            items = state.get("items_here" if kind == "here" else "inventory", [])
            found = any(text in i.lower() for i in items)
        if found == negated:
            return False
    return True


def win_text(path):
    for line in open(path):
        if line.lower().startswith("# win:"):
            return line.split(":", 1)[1].strip().lower()
    return "well done"


def run(game, walk, seed, verbose):
    eng = Engine(game, seed=seed)
    t = eng.start()
    amap = AutoMap(scott.load(game))
    amap.observe(t.state)
    problems = []
    won = False
    goal = win_text(walk)
    last = t.text

    def send(n, cmd, optional):
        nonlocal t, last, won
        before = t.state.get("room")
        t = eng.send(cmd)
        amap.observe(t.state, cmd, before)
        if not t.state.get("game_over"):
            amap.level_view(t.state["room"], 38, 20)  # what the side panel draws
        last = t.text + "\n" + "\n".join(t.looks)
        out = t.text.lower()
        won |= goal in out
        if verbose:
            print(f"> {cmd}\n{t.text}\n  [room {t.state['room']} carry {len(t.state['inventory'])} "
                  f"light {t.state['light_time']} stored {t.state['treasures_stored']}]")
        hits = [w for w in WARN if w in out]
        if (hits and not optional) or t.state.get("unknown_word"):
            problems.append(f"{n}:{cmd}: {t.text.strip()[:80]!r}")
        if t.state.get("game_over") and not won:
            problems.append(f"{n}:{cmd}: game over: {' '.join(t.text.split())[-90:]!r}")

    for n, (cmd, optional, cond, loop) in enumerate(commands(walk), 1):
        if loop:
            sent = 0
            while not holds(cond, t.state, last) and sent < MAX_ROUNDS and not t.state.get("game_over"):
                send(n, loop[sent % len(loop)], True)
                sent += 1
            if sent == MAX_ROUNDS:
                problems.append(f"{n}:until: not reached after {sent} commands")
        elif not cond or holds(cond, t.state, last):
            send(n, cmd, optional)
        if t.state.get("game_over"):
            break
    eng.close()
    return won, t.state, problems, amap


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in argv
    seeds = int(argv[argv.index("--seeds") + 1]) if "--seeds" in argv else 1
    first = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 1
    for opt in ("--seeds", "--seed"):
        if opt in argv:
            args.remove(argv[argv.index(opt) + 1])
    game, walk = args
    game = str(Path(game).resolve())  # the engine runs from the project root
    wins = 0
    for seed in range(first, first + seeds):
        won, state, problems, amap = run(game, walk, seed, verbose and seeds == 1)
        wins += won
        print(f"seed {seed}: {'WON' if won else 'not won'} turn {state.get('turn')} "
              f"stored {state.get('treasures_stored')}/{state.get('treasures_total')} light {state.get('light_time')}")
        for p in problems:
            print("   ", p)
    if "--map" in argv:
        print()
        print(amap.render(state.get("room"), width=110))
    print(f"{wins}/{seeds} won")
    return 0 if wins == seeds else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
