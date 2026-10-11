"""Play a walkthrough file through the real engine and report how it went.

    python3 claudventure/walkthrough.py games/adv01.dat claudventure/walkthroughs/adv01.txt [--seeds N] [-v]
"""

import sys

from engine import Engine

WARN = ("dead", "can't", "beyond my power", "don't understand", "too much",
        "don't know", "nothing to light", "what ?", "dangerous", "not here")


def commands(path):
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#"):
            optional = line.startswith("?")
            yield line.lstrip("? ").strip(), optional


def run(game, walk, seed, verbose):
    eng = Engine(game, seed=seed)
    t = eng.start()
    problems = []
    for n, (cmd, optional) in enumerate(commands(walk), 1):
        t = eng.send(cmd)
        out = t.text.lower()
        if verbose:
            print(f"> {cmd}\n{t.text}\n  [room {t.state['room']} carry {len(t.state['inventory'])} "
                  f"light {t.state['light_time']} stored {t.state['treasures_stored']}]")
        hits = [w for w in WARN if w in out]
        if (hits and not optional) or t.state.get("unknown_word"):
            problems.append(f"{n}:{cmd}: {t.text.strip()[:80]!r}")
        if t.state.get("game_over"):
            break
    eng.close()
    won = "well done" in t.text.lower()
    return won, t.state, problems


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in argv
    seeds = int(argv[argv.index("--seeds") + 1]) if "--seeds" in argv else 1
    if "--seeds" in argv:
        args.remove(str(seeds))
    game, walk = args
    wins = 0
    for seed in range(1, seeds + 1):
        won, state, problems = run(game, walk, seed, verbose and seeds == 1)
        wins += won
        print(f"seed {seed}: {'WON' if won else 'not won'} turn {state.get('turn')} "
              f"stored {state.get('treasures_stored')}/{state.get('treasures_total')} light {state.get('light_time')}")
        for p in problems:
            print("   ", p)
    print(f"{wins}/{seeds} won")
    return 0 if wins == seeds else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
