"""Reader for ScottFree (TRS-80 format) Scott Adams game files.

Used by the front end to check words against the game's vocabulary and to
build the readable game guide that the narrator gets in its system prompt.

    python3 claudventure/scott.py guide games/adv01.dat      # print the guide
    python3 claudventure/scott.py vocab games/adv01.dat      # print the vocabulary
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CARRIED = 255
DESTROYED = 0
LIGHT_SOURCE = 9
DARKBIT = 15
LIGHTOUTBIT = 16
DIRECTIONS = ["North", "South", "East", "West", "Up", "Down"]

# Titles from games/0readme.txt
TITLES = {
    "adv01": "Adventureland",
    "adv02": "Pirate Adventure",
    "adv03": "Secret Mission",
    "adv04": "Voodoo Castle",
    "adv05": "The Count",
    "adv06": "Strange Odyssey",
    "adv07": "Mystery Fun House",
    "adv08": "Pyramid of Doom",
    "adv09": "Ghost Town",
    "adv10": "Savage Island, Part I",
    "adv11": "Savage Island, Part II",
    "adv12": "The Golden Voyage",
    "adv13": "Sorcerer of Claymorgue Castle",
    "adv14a": "Return to Pirate's Isle",
    "adv14b": "Buckaroo Banzai",
    "quest1": "Questprobe 1: The Hulk",
    "quest2": "Questprobe 2: Spiderman",
    "sampler1": "Adventureland (sampler)",
}


@dataclass
class Action:
    verb: int
    noun: int
    conditions: List[Tuple[int, int]]  # (code, value)
    actions: List[int]
    comment: str = ""


@dataclass
class Room:
    exits: List[int]
    text: str


@dataclass
class Item:
    text: str
    location: int
    autoget: Optional[str]


@dataclass
class Game:
    name: str
    title: str
    num_items: int
    max_carry: int
    start_room: int
    treasures: int
    word_length: int
    light_time: int
    treasure_room: int
    actions: List[Action] = field(default_factory=list)
    verbs: List[str] = field(default_factory=list)
    nouns: List[str] = field(default_factory=list)
    rooms: List[Room] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    version: int = 0
    adventure: int = 0

    @property
    def has_darkness(self) -> bool:
        """Whether any place can be dark: only then does the light source matter."""
        return any(56 in a.actions for a in self.actions)

    # --- vocabulary, mirroring WhichWord() in ScottCurses.c ---------------

    def _match(self, word: str, entry: str) -> bool:
        # strncasecmp(word, entry, WordLength) == 0
        a, b = word.upper(), entry.upper()
        for k in range(self.word_length):
            ca = a[k] if k < len(a) else ""
            cb = b[k] if k < len(b) else ""
            if ca != cb:
                return False
            if not ca:
                return True
        return True

    def which_word(self, word: str, words: List[str]) -> int:
        """Index of the (canonical) word, or -1, exactly as the engine does."""
        canonical = 1
        for i in range(1, len(words)):
            entry = words[i]
            if entry.startswith("*"):
                entry = entry[1:]
            else:
                canonical = i
            if self._match(word, entry):
                return canonical
        return -1

    def groups(self, words: List[str]) -> Dict[int, List[str]]:
        """Canonical index -> [word, synonyms...], skipping empty padding."""
        out: Dict[int, List[str]] = {}
        canonical = 0
        for i, w in enumerate(words):
            if w.startswith("*"):
                if w[1:]:
                    out.setdefault(canonical, []).append(w[1:])
            else:
                canonical = i
                if w:
                    out.setdefault(i, []).append(w)
        return out

    def autoget_words(self) -> List[str]:
        return [it.autoget for it in self.items if it.autoget]

    def parse_command(self, line: str) -> Tuple[Optional[int], Optional[int], str]:
        """Return (verb index, noun index, error) as GetInput() would see it.

        noun index is None when no noun was given, -1 when it is unknown.
        """
        parts = line.split()
        if not parts:
            return None, None, "empty command"
        verb = parts[0][:9]
        noun = parts[1][:9] if len(parts) > 1 else ""
        if not noun and len(verb) == 1:
            verb = {"n": "NORTH", "e": "EAST", "s": "SOUTH", "w": "WEST",
                    "u": "UP", "d": "DOWN", "i": "INVENTORY"}.get(verb.lower(), verb)
        nc = self.which_word(verb, self.nouns)
        if 1 <= nc <= 6:
            return 1, nc, ""
        vc = self.which_word(verb, self.verbs)
        if vc == -1:
            return None, None, f"unknown verb '{parts[0]}'"
        if not noun:
            return vc, None, ""
        nc = self.which_word(noun, self.nouns)
        return vc, nc, ""

    # --- naming helpers for the guide -----------------------------------

    def room_name(self, r: int) -> str:
        if r == CARRIED:
            return "carried"
        if r == DESTROYED:
            return "R0 (out of play)"
        if 0 <= r < len(self.rooms):
            return f"R{r} ({short_room(self.rooms[r].text)})"
        return f"R{r}"

    def item_name(self, i: int) -> str:
        if 0 <= i < len(self.items):
            return f"I{i} ({self.items[i].text})"
        return f"I{i}"

    def word(self, words: List[str], i: int) -> str:
        g = self.groups(words).get(i)
        return g[0] if g else f"#{i}"

    def message(self, m: int) -> str:
        if 0 <= m < len(self.messages):
            return self.messages[m]
        return f"<message {m}>"


def short_room(text: str) -> str:
    t = text[1:] if text.startswith("*") else text
    t = t.split("\n")[0].strip()
    return t if len(t) <= 48 else t[:45] + "..."


def place_name(text: str, maxlen: int = 40) -> str:
    """Short name of a room for the player: 'I'm on the shore of a lake' -> 'shore of a lake'."""
    t = text[1:] if text.startswith("*") else text
    lines = t.split("\n")
    t = lines[0]
    if len(lines) > 1 and re.search(r"\b(in|on|at|of|an?|the|and|to|with|over|by|from|into)$", t.strip(), re.I):
        t += " " + lines[1]  # 'underwater in / thick murky fluid'
    t = " ".join(re.split(r"[.!]\s", t + " ")[0].split())  # the first sentence
    t = re.sub(r"^(I'm |I am |You're |You are )(in |on |at )?(an? |the )?", "", t, flags=re.I).rstrip(".!")
    if len(t) > maxlen:
        t = t[:maxlen].rsplit(" ", 1)[0] + "…"
    return t


def room_display(text: str) -> str:
    """Room text as Look() prints it in 'I am' mode."""
    return text[1:] if text.startswith("*") else f"I'm in a {text}"


# --- file reader --------------------------------------------------------------

class _Reader:
    def __init__(self, data: str):
        self.d = data
        self.p = 0

    def _skip(self):
        while self.p < len(self.d) and self.d[self.p].isspace():
            self.p += 1

    def num(self) -> int:
        self._skip()
        m = re.compile(r"-?\d+").match(self.d, self.p)
        if not m:
            raise ValueError(f"number expected at offset {self.p}")
        self.p = m.end()
        return int(m.group())

    def string(self) -> str:
        self._skip()
        if self.d[self.p] != '"':
            raise ValueError(f"quote expected at offset {self.p}")
        self.p += 1
        out = []
        while True:
            c = self.d[self.p]
            self.p += 1
            if c == '"':
                if self.p < len(self.d) and self.d[self.p] == '"':
                    self.p += 1
                else:
                    break
            out.append('"' if c == "`" else c)
        return "".join(out)


def load(path) -> Game:
    path = Path(path)
    r = _Reader(path.read_text(encoding="latin-1"))
    r.num()  # unknown
    ni, na, nw, nr, mc, pr, tr, wl, lt, mn, trm = (r.num() for _ in range(11))
    g = Game(name=path.stem.lower(), title=TITLES.get(path.stem.lower(), path.stem),
             num_items=ni, max_carry=mc, start_room=pr, treasures=tr,
             word_length=wl, light_time=lt, treasure_room=trm)
    for _ in range(na + 1):
        vals = [r.num() for _ in range(8)]
        conds = [(c % 20, c // 20) for c in vals[1:6]]
        acts = [vals[6] // 150, vals[6] % 150, vals[7] // 150, vals[7] % 150]
        g.actions.append(Action(vals[0] // 150, vals[0] % 150, conds, acts))
    for _ in range(nw + 1):
        g.verbs.append(r.string())
        g.nouns.append(r.string())
    for _ in range(nr + 1):
        exits = [r.num() for _ in range(6)]
        g.rooms.append(Room(exits, r.string()))
    for _ in range(mn + 1):
        g.messages.append(r.string())
    for _ in range(ni + 1):
        text = r.string()
        loc = r.num() & 0xFF
        autoget = None
        slash = text.find("/")
        if slash >= 0 and text[slash:] not in ("//", "/*"):
            text, autoget = text[:slash], text[slash + 1:].split("/")[0] or None
        g.items.append(Item(text, loc, autoget))
    for a in g.actions:
        try:
            a.comment = r.string()
        except (ValueError, IndexError):
            break
    try:
        g.version = r.num()
        g.adventure = r.num()
    except (ValueError, IndexError):
        pass
    return g


# --- guide ----------------------------------------------------------------

COND = {
    1: "carrying {i}",
    2: "{i} is here",
    3: "{i} carried or here",
    4: "player in {r}",
    5: "{i} NOT here",
    6: "NOT carrying {i}",
    7: "player NOT in {r}",
    8: "flag {n} set",
    9: "flag {n} clear",
    10: "carrying something",
    11: "carrying nothing",
    12: "{i} neither carried nor here",
    13: "{i} in play",
    14: "{i} out of play",
    15: "counter <= {n}",
    16: "counter > {n}",
    17: "{i} still in its starting place",
    18: "{i} moved from its starting place",
    19: "counter == {n}",
}

# action code -> (description, number of parameters, parameter kinds)
ACT = {
    52: ("get {0}", "i"),
    53: ("drop {0} here", "i"),
    54: ("move player to {0}", "r"),
    55: ("remove {0} from play", "i"),
    56: ("darkness on", ""),
    57: ("darkness off", ""),
    58: ("set flag {0}", "n"),
    59: ("remove {0} from play", "i"),
    60: ("clear flag {0}", "n"),
    61: ("PLAYER DIES (moved to limbo)", ""),
    62: ("put {0} in {1}", "ir"),
    63: ("GAME OVER", ""),
    64: ("describe room", ""),
    65: ("show score (wins if all treasures stored)", ""),
    66: ("show inventory", ""),
    67: ("set flag 0", ""),
    68: ("clear flag 0", ""),
    69: ("refill lamp", ""),
    70: ("clear screen", ""),
    71: ("save game", ""),
    72: ("swap locations of {0} and {1}", "ii"),
    73: ("continue with the following unconditional lines", ""),
    74: ("get {0} (no weight check)", "i"),
    75: ("put {0} where {1} is", "ii"),
    76: ("describe room", ""),
    77: ("counter -= 1", ""),
    78: ("print counter", ""),
    79: ("counter = {0}", "n"),
    80: ("swap player location with saved room", ""),
    81: ("swap counter with backup counter {0}", "n"),
    82: ("counter += {0}", "n"),
    83: ("counter -= {0}", "n"),
    84: ("echo typed noun", ""),
    85: ("echo typed noun + newline", ""),
    86: ("newline", ""),
    87: ("swap player location with saved room {0}", "n"),
    88: ("pause", ""),
    89: ("(picture {0})", "n"),
}


def describe_line(g: Game, a: Action) -> Tuple[str, str]:
    params = [v for c, v in a.conditions if c == 0]
    conds = []
    for c, v in a.conditions:
        if c == 0:
            continue
        conds.append(COND.get(c, f"cond{c}({v})").format(
            i=g.item_name(v), r=g.room_name(v), n=v))
    acts = []
    pp = 0
    for x in a.actions:
        if 1 <= x <= 51:
            acts.append(f'say "{g.message(x)}"')
        elif x >= 102:
            acts.append(f'say "{g.message(x - 50)}"')
        elif x == 0:
            continue
        elif x in ACT:
            text, kinds = ACT[x]
            args = []
            for k in kinds:
                v = params[pp] if pp < len(params) else -1
                pp += 1
                args.append(g.item_name(v) if k == "i" else g.room_name(v) if k == "r" else str(v))
            acts.append(text.format(*args))
        else:
            acts.append(f"<action {x}>")
    return (" AND ".join(conds) or "always"), "; ".join(acts) or "nothing"


def guide(g: Game) -> str:
    out = []
    w = out.append
    w(f"# {g.title} ({g.name}.dat), version {g.version // 100}.{g.version % 100:02d}")
    w("")
    w("## Parser facts")
    w(f"- Commands are one or two words: VERB or VERB NOUN. Only the first {g.word_length} "
      "letters of each word count, and a word matches only if it is at least that long "
      f"(or exactly as long as the dictionary entry). Words below are stored truncated to {g.word_length} letters.")
    w("- A direction alone (NORTH, N, ...) moves. Single letters n/s/e/w/u/d/i are accepted.")
    w("- Unknown verbs are rejected without using a turn. Any other command uses a turn.")
    w(f"- Carry limit: {g.max_carry} items. GET ALL / DROP ALL exist.")
    if g.has_darkness and 0 < g.light_time < 5000:
        w(f"- The light source (item I{LIGHT_SOURCE}) lasts {g.light_time} turns once it is in play.")
    if g.treasures:
        w(f"- {g.treasures} treasures (items whose name starts with '*'). Store them in "
          f"{g.room_name(g.treasure_room)} and say SCORE; all stored + SCORE wins.")
    w("")

    w("## Vocabulary (first word of each group is canonical, others are synonyms)")
    vg = g.groups(g.verbs)
    ng = g.groups(g.nouns)
    w("Verbs: " + " | ".join("=".join(ws) for i, ws in sorted(vg.items()) if i > 0))
    w("Nouns: " + " | ".join("=".join(ws) for i, ws in sorted(ng.items()) if i > 0))
    gets = sorted(set(g.autoget_words()))
    if gets:
        w("Words GET/DROP accept for portable items: " + ", ".join(gets))
    w("")

    w("## Rooms (exits: N S E W U D)")
    for i, room in enumerate(g.rooms):
        if i == 0:
            continue
        ex = ", ".join(f"{DIRECTIONS[d][0]}->R{t}" for d, t in enumerate(room.exits) if t)
        w(f"- R{i}: {room_display(room.text)!r}" + (f" | exits {ex}" if ex else " | no exits"))
    w(f"(R{len(g.rooms) - 1} is where the player goes on death.)")
    w("")

    w("## Items (I = index, start location)")
    for i, it in enumerate(g.items):
        tag = " [treasure]" if it.text.startswith("*") else ""
        ag = f" | GET/DROP word: {it.autoget}" if it.autoget else ""
        w(f"- I{i}: {it.text!r}{tag} | starts {g.room_name(it.location)}{ag}")
    w("")

    w("## Game logic")
    w("Each turn, before input, every AUTO line runs with its % chance. A typed "
      "command runs the first VERB NOUN line whose conditions all hold (NOUN 'ANY' "
      "matches any noun). Lines marked '+' continue the line above them.")
    cont = False
    for idx, a in enumerate(g.actions):
        cond, acts = describe_line(g, a)
        if a.verb == 0 and a.noun == 0 and cont:
            head = "  +"
        elif a.verb == 0:
            if a.noun == 0:
                cont = False
                continue
            head = f"A{idx} AUTO {a.noun}%"
        else:
            head = f"A{idx} {g.word(g.verbs, a.verb)} {g.word(g.nouns, a.noun) if a.noun else 'ANY'}"
        if not (a.verb == 0 and a.noun == 0):
            cont = 73 in a.actions
        elif 73 in a.actions:
            cont = True
        note = f"  // {a.comment}" if a.comment.strip() else ""
        w(f"{head}: IF {cond} THEN {acts}{note}")
    w("")
    w("Messages not referenced above are unused.")
    return "\n".join(out)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("guide", "vocab"):
        print(__doc__)
        return 1
    g = load(argv[2])
    if argv[1] == "guide":
        print(guide(g))
    else:
        for name, words in (("verbs", g.verbs), ("nouns", g.nouns)):
            print(name + ": " + " | ".join("=".join(ws) for ws in g.groups(words).values()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
