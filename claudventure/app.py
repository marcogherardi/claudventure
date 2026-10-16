"""Claudventure: play the Scott Adams adventures with Claude as narrator.

Claude (Claude Code in non-interactive mode) sits between the player and the
original ScottFree engine: it turns free-form input into two-word commands,
the front end runs them on the engine, and Claude narrates the outcome.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import List, Optional, Tuple

import scott
from automap import AutoMap
from engine import ROOT, Engine, Turn

HERE = Path(__file__).resolve().parent
GAMES = ROOT / "games"
SAVES = ROOT / "saves"
LOGS = ROOT / "logs"

MAX_ROUNDS = 3        # command batches Claude may send per player message
MAX_COMMANDS = 8      # commands per player message

RESET, DIM, CYAN, BOLD, YELLOW = "\033[0m", "\033[2m", "\033[36m", "\033[1m", "\033[33m"
GREEN = "\033[32m"

HELP = """Type what you want to do, in your own words, or ask the narrator anything.
Plain game commands (n, e, i, get axe, climb tree...) go straight to the game; Claude narrates the result.
  /undo          take back your last action
  /save [name]   save the game (default name: quick)
  /load [name]   load a saved game
  /saves         list saved games
  /new           start over
  /map           map of the places you have visited
  /usage         tokens used and subscription quota
  /quit          leave (the game is saved as 'autosave')
Full screen: PgUp/PgDn scroll the story, Up/Down recall what you typed, Ctrl+Q quits."""


# --- Claude ------------------------------------------------------------------

def fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


class Usage:
    """Token counts and quota, summed over every request of the session."""

    def __init__(self):
        self.requests = 0
        self.input = 0
        self.cache_write = 0
        self.cache_read = 0
        self.output = 0
        self.cost = 0.0           # estimate at API list prices
        self.windows: dict = {}   # subscription quota windows from rate_limit_event

    def add_result(self, ev: dict, cost_delta: float):
        u = ev.get("usage") or {}
        self.requests += 1
        self.input += u.get("input_tokens") or 0
        self.cache_write += u.get("cache_creation_input_tokens") or 0
        self.cache_read += u.get("cache_read_input_tokens") or 0
        self.output += u.get("output_tokens") or 0
        self.cost += max(cost_delta, 0.0)

    def add_rate_limit(self, ev: dict):
        windows = (ev.get("rate_limit_info") or {}).get("unifiedWindows")
        if windows:
            self.windows = windows

    @property
    def fresh(self) -> int:
        """Tokens not read from the cache: what really weighs on the quota."""
        return self.input + self.cache_write + self.output

    def _window(self, key: str):
        w = self.windows.get(key) or {}
        return w.get("utilization"), w.get("resetsAt")

    def short(self) -> str:
        if not self.requests:
            return ""
        text = f"tok {fmt_tokens(self.fresh)} + {fmt_tokens(self.cache_read)} cache"
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            util, _ = self._window(key)
            if util is not None:
                text += f" · {label} {util * 100:.0f}%"
        return text

    def report(self, model: str) -> str:
        lines = [f"Claude ({model}): {self.requests} requests this session",
                 f"  input {fmt_tokens(self.input)} · cache write {fmt_tokens(self.cache_write)}"
                 f" · cache read {fmt_tokens(self.cache_read)} · output {fmt_tokens(self.output)}",
                 f"  estimated cost at API list prices: ${self.cost:.2f}"
                 " (with a subscription it is not charged)"]
        quota = []
        for key, label, when in (("five_hour", "5-hour window", "%H:%M"),
                                 ("seven_day", "weekly", "%a %d %b %H:%M")):
            util, reset = self._window(key)
            if util is not None:
                part = f"{label} {util * 100:.0f}% used"
                if reset:
                    part += f" (resets {datetime.datetime.fromtimestamp(reset).strftime(when)})"
                quota.append(part)
        lines.append("Subscription quota: " + (" · ".join(quota) if quota else "not reported"))
        return "\n".join(lines)


class Claude:
    """A persistent Claude Code session in stream-json mode."""

    def __init__(self, system_prompt_file: Path, model: str, effort: str, log, usage: Usage):
        self.log = log
        self.usage = usage
        self.last_cost = 0.0  # total_cost_usd is cumulative for the process
        self.args = [
            "claude", "-p",
            "--model", model, "--effort", effort,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--system-prompt-file", str(system_prompt_file),
            "--tools", "", "--strict-mcp-config", "--setting-sources", "local",
            "--no-session-persistence",
        ]
        # A neutral working directory keeps project files and CLAUDE.md out of the session
        self.cwd = tempfile.mkdtemp(prefix="claudventure-claude-")
        # stderr goes to a file: an unread pipe could fill up and block Claude
        self.errfile = open(Path(self.cwd) / "stderr.txt", "w+")
        self.proc = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errfile, text=True, bufsize=1, cwd=self.cwd)

    def ask(self, text: str):
        """Send one user message; yield text chunks as they stream in."""
        self.log("to-claude", text)
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError(self._dead_message())
        streamed = False
        full = ""
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    streamed = True
                    full += e["delta"]["text"]
                    yield e["delta"]["text"]
            elif kind == "assistant" and not streamed:
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        full += block["text"]
                        yield block["text"]
            elif kind == "rate_limit_event":
                self.usage.add_rate_limit(ev)
            elif kind not in ("user", "assistant"):
                self.log("claude-event", line.strip()[:500])
            if kind == "result":
                cost = ev.get("total_cost_usd") or 0.0
                self.usage.add_result(ev, cost - self.last_cost)
                self.last_cost = cost
                self.log("from-claude", full)
                if ev.get("is_error"):
                    raise RuntimeError(f"Claude error: {ev.get('result') or ev.get('subtype')}")
                return
        raise RuntimeError(self._dead_message())

    def _dead_message(self) -> str:
        self.errfile.seek(0)
        err = self.errfile.read()
        return "the Claude session ended unexpectedly" + (f": {err.strip()[:300]}" if err else "")

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.errfile.close()
        shutil.rmtree(self.cwd, ignore_errors=True)


# --- terminal output ------------------------------------------------------------

class Printer:
    """Word-wrapping writer that works on streamed text."""

    def __init__(self):
        cols = shutil.get_terminal_size((88, 24)).columns
        self.width = min(cols - 2, 96) if cols >= 40 else 86
        self.col = 0
        self.word = ""
        self.style = ""
        self.pending_dots = False

    def dots(self):
        sys.stdout.write(DIM + "…" + RESET)
        sys.stdout.flush()
        self.pending_dots = True

    def _clear_dots(self):
        if self.pending_dots:
            sys.stdout.write("\r\033[K")
            self.pending_dots = False

    def set_style(self, style: str):
        self._flush_word()
        self.style = style

    def write(self, text: str):
        for ch in text:
            if ch == "\n":
                self._flush_word()
                self._emit("\n")
                self.col = 0
            elif ch == " ":
                self._flush_word()
                if 0 < self.col < self.width:
                    self._emit(" ")
                    self.col += 1
            else:
                self.word += ch
        sys.stdout.flush()

    def _flush_word(self):
        if not self.word:
            return
        if self.col and self.col + len(self.word) > self.width:
            self._emit("\n")
            self.col = 0
        self._emit(self.word)
        self.col += len(self.word)
        self.word = ""

    def _emit(self, s: str):
        self._clear_dots()
        sys.stdout.write(f"{self.style}{s}{RESET}" if self.style else s)

    def end(self):
        self._flush_word()
        self._clear_dots()
        if self.col:
            sys.stdout.write("\n")
        self.col = 0
        self.style = ""
        sys.stdout.flush()

    def line(self, text: str, style: str = ""):
        self.end()
        self.set_style(style)
        self.write(text + "\n")
        self.end()

    def footer(self, st: dict):
        """Exits and visible items under the narration."""
        if st.get("game_over"):
            return
        if st["dark"]:
            self.line("It is too dark to see.", DIM)
            return
        self.line("Exits: " + (", ".join(st["exits"]) or "none"), DIM)
        if st["items_here"]:
            self.line("You see:", DIM)
            for item in st["items_here"]:
                wrapped = textwrap.fill(" ".join(item.split()), width=self.width,
                                        initial_indent="   ", subsequent_indent="     ")
                print(f"{GREEN}{wrapped}{RESET}")

    def show_map(self, text: str):
        self.end()
        for line in text.split("\n"):
            if line.startswith("Level "):
                print(f"{BOLD}{line}{RESET}")
            else:
                print(re.sub(r"(<[^>]*>)", f"{YELLOW}{BOLD}\\1{RESET}", line, count=1))


class ReplyParser:
    """Splits Claude's streamed reply into visible text, asides and <cmd> tags."""

    TAGS = ("<cmd>", "</cmd>", "<aside>", "</aside>")

    def __init__(self, out: Printer):
        self.out = out
        self.buf = ""
        self.in_cmd = False
        self.in_aside = False
        self.cmds: List[str] = []
        self.cmd = ""
        self.shown = False
        self.at_line_start = True

    def feed(self, chunk: str):
        self.buf += chunk
        while self.buf:
            if self.buf[0] == "<":
                tag = next((t for t in self.TAGS if self.buf.startswith(t)), None)
                if tag:
                    self.buf = self.buf[len(tag):]
                    self._tag(tag)
                    continue
                if any(t.startswith(self.buf) for t in self.TAGS):
                    return  # might still become a tag: wait for more text
            self._char(self.buf[0])
            self.buf = self.buf[1:]

    def _tag(self, tag: str):
        if tag == "<cmd>":
            self.in_cmd, self.cmd = True, ""
        elif tag == "</cmd>":
            self.in_cmd = False
            if self.cmd.strip():
                self.cmds.append(" ".join(self.cmd.split()).upper())
        elif tag == "<aside>":
            self.in_aside = True
            if self.shown:
                self.out.end()
                self.out.write("\n")
            self.out.set_style(CYAN)
            self.at_line_start = True
        elif tag == "</aside>":
            self.in_aside = False
            self.out.end()

    def _char(self, ch: str):
        if self.in_cmd:
            self.cmd += ch
            return
        if self.cmds:
            return  # nothing is shown once commands have been issued
        if self.at_line_start and ch in " \n":
            return  # leading blank space
        self.at_line_start = False
        self.shown = True
        if self.in_aside and self.out.style != CYAN:
            self.out.set_style(CYAN)
        self.out.write(ch)

    def finish(self):
        if self.buf and not self.in_cmd and not self.cmds:
            self.out.write(self.buf)
        self.buf = ""
        self.out.end()


# --- game session -----------------------------------------------------------------

class Session:
    """The game: engine, Claude, undo, saves and map. Output goes through `out`
    (a Printer for --plain, or the panel interface in tui.py)."""

    def __init__(self, args, out=None):
        self.args = args
        self.game_path = GAMES / f"{args.game}.dat"
        self.game = scott.load(self.game_path)
        self.death_room = len(self.game.rooms) - 1
        self.out = out or Printer()
        self.saves = SAVES / args.game
        self.saves.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.logfile = open(LOGS / f"{args.game}-{stamp}.log", "w")
        self.undo_dir = Path(tempfile.mkdtemp(prefix="claudventure-undo-"))
        self.undo: List[Path] = []
        self.engine = Engine(self.game_path, seed=args.seed)
        self.prompt_file = self._write_system_prompt()
        self.claude: Optional[Claude] = None
        self.usage = Usage()  # survives /new, which restarts Claude
        self.map = AutoMap(self.game)

    # system prompt -------------------------------------------------------------

    def _write_system_prompt(self) -> Path:
        template = (HERE / "narrator.md").read_text()
        walk_path = HERE / "walkthroughs" / f"{self.args.game}.txt"
        if walk_path.exists():
            walkthrough = ("# Solution (secret: only for graded hints)\n\n"
                           "A verified sequence of commands that wins the game. Random events can "
                           "require small detours. Lines starting with # are notes; '? CMD' is only "
                           "needed in some cases; 'if here \"X\": CMD' (or 'if carrying', 'if said', "
                           "'unless ...') is done only when X is in the room, carried, or in the "
                           "last message (or, for unless, when it is not); 'until C: A, B' repeats "
                           "A and B in turn until C holds (for random destinations).\n\n"
                           + walk_path.read_text())
        else:
            walkthrough = ("# Solution\n\nNo verified walkthrough is available for this game: work "
                           "out hints from the game logic above.")
        text = (template.replace("{title}", self.game.title)
                .replace("{word_length}", str(self.game.word_length))
                .replace("{guide}", scott.guide(self.game))
                .replace("{walkthrough}", walkthrough))
        path = self.undo_dir / "narrator-prompt.md"
        path.write_text(text)
        return path

    def log(self, kind: str, text: str):
        self.logfile.write(f"--- {kind} {datetime.datetime.now():%H:%M:%S}\n{text}\n")
        self.logfile.flush()

    def start_claude(self):
        if self.claude:
            self.claude.close()
        self.claude = Claude(self.prompt_file, self.args.model, self.args.effort, self.log,
                             self.usage)

    # state for Claude ----------------------------------------------------------

    def state_text(self, st: dict) -> str:
        g = self.game
        lines = [f"Location: {scott.short_room(st['room_text'])}"
                 + (" (too dark to see anything)" if st["dark"] else "")]
        if not st["dark"]:
            lines.append("Exits: " + (", ".join(st["exits"]) or "none"))
            lines.append("Visible here: " + (", ".join(st["items_here"]) or "nothing"))
        lines.append("Carrying: " + (", ".join(st["inventory"]) or "nothing")
                     + f" ({len(st['inventory'])}/{g.max_carry})")
        light = (f"; light on, {st['light_time']} turns of light left" if st["lamp_lit"]
                 else "; no lit light source") if g.has_lamp else ""
        lines.append(f"Turn {st['turn']}{light}")
        if st["treasures_total"]:
            lines.append(f"Treasures stored: {st['treasures_stored']} of {st['treasures_total']}")
        moved = []
        for i, loc in enumerate(st["item_locations"]):
            start = g.items[i].location
            now = scott.CARRIED if loc == -1 else loc
            if now != start and g.items[i].text:
                moved.append(f"I{i} {g.items[i].text}: {g.room_name(start)} -> {g.room_name(now)}")
        if moved:
            lines.append("Items moved from their starting places (internal, for your reasoning): "
                         + "; ".join(moved))
        lines.append(f"Room R{st['room']}; flags set: {st['flags']}; counter {st['counter']} (internal)")
        return "\n".join(lines)

    # commands ----------------------------------------------------------------------

    def check(self, cmd: str) -> Optional[str]:
        g = self.game
        vb, no, err = g.parse_command(cmd)
        if err:
            return f"{cmd}: {err}; the parser does not know that word"
        words = cmd.split()
        if len(words) > 2:
            return f"{cmd}: the parser reads only two words"
        if no == -1:
            noun = words[1]
            if vb in (10, 18) and (noun.upper() == "ALL" or any(
                    g._match(noun, w) for w in g.autoget_words())):
                return None
            if any(a.verb == vb and a.noun == 0 for a in g.actions):
                return None  # the verb has rules that accept any noun
            return f"{cmd}: the parser does not know the noun '{noun}'"
        return None

    def direct_command(self, line: str) -> Optional[str]:
        """The command itself if the line is already a valid game command, else None.

        Such lines go straight to the engine: Claude only narrates the outcome,
        which saves the request that would interpret the input.
        """
        if self.args.always_interpret:
            return None
        words = line.split()
        if not 1 <= len(words) <= 2 or not all(w.isalpha() for w in words):
            return None
        vb, no, err = self.game.parse_command(line)
        if err or self.check(line):
            return None
        if no is not None and 1 <= no <= 6 and vb != 1:
            return None  # "pick up", "get down": a phrasal verb, not a direction
        return " ".join(words).upper()

    def run_direct(self, cmd: str):
        """Run a command typed in the game's own language, then let Claude narrate it."""
        vb, _, _ = self.game.parse_command(cmd)
        if vb == self.game.which_word("SAVE", self.game.verbs):
            self.out.line("To save, use /save [name].", YELLOW)
            return
        if vb == self.game.which_word("QUIT", self.game.verbs):
            self.out.line("To leave, use /quit (the game is saved). To start over, /new.", YELLOW)
            return
        if not self.engine.alive:
            self.out.line("The game is over: use /undo, /load or /new.", YELLOW)
            return
        self.snapshot()
        state_before = self.state_text(self.engine.state)
        results, changed = self.run_commands([cmd])
        message = (f"<player>{cmd}</player>\n<direct>The player typed this in the game's own words, "
                   "so it was sent straight to the engine. Narrate the outcome.</direct>\n"
                   f"<state_before>\n{state_before}\n</state_before>\n" + results)
        self.turn(message, direct=True, view_changed=changed)

    def run_commands(self, cmds: List[str]) -> Tuple[str, bool]:
        """Run checked commands on the engine. Returns (message for Claude, view changed)."""
        parts, view = [], None
        died = over = False
        for i, cmd in enumerate(cmds):
            before = self.engine.state.get("room")
            t = self.engine.send(cmd)
            self.map.observe(t.state, cmd, before)
            self.log("engine", f"> {cmd}\n{t.text}\n" + "\n".join(t.looks))
            parts.append(f'<result cmd="{cmd}">{t.text or "(no message)"}</result>')
            if t.view:
                view = t.view
            st = t.state
            died = st["room"] == self.death_room and before != self.death_room
            over = st.get("game_over", False)
            if (died or over) and i < len(cmds) - 1:
                parts.append(f"<note>The remaining commands ({', '.join(cmds[i + 1:])}) were not run.</note>")
            if died or over:
                break
        if view:
            parts.append(f"<view>\n{view}\n</view>")
        if died:
            parts.append("<note>The player died and is now in the limbo room.</note>")
        if over:
            parts.append("<note>GAME OVER: the engine has stopped. The player can use /undo, /load or /new.</note>")
        msg = "<results>\n" + "\n".join(parts) + "\n</results>\n<state>\n" + \
            self.state_text(self.engine.state) + "\n</state>"
        return msg, view is not None

    def snapshot(self):
        if not self.engine.alive:
            return
        path = self.undo_dir / f"undo-{len(self.undo):04d}.sav"
        if self.engine.save(path):
            self.undo.append(path)

    # talking to Claude -------------------------------------------------------------------

    def converse(self, message: str) -> List[str]:
        """Send a message to Claude, show what it says, return any commands."""
        parser = ReplyParser(self.out)
        self.out.dots()
        try:
            for chunk in self.claude.ask(message):
                parser.feed(chunk)
        finally:
            parser.finish()
        return parser.cmds

    def turn(self, message: str, direct: bool = False, view_changed: bool = False):
        """One player message: let Claude issue commands until it narrates.

        direct: a command typed in the game's words has already run (and the undo
        snapshot been taken); view_changed tells whether it changed the view.
        """
        rounds = total = 0
        snapshot_taken = direct
        while True:
            cmds = self.converse(message)
            if not cmds:
                break
            rounds += 1
            total += len(cmds)
            if rounds > MAX_ROUNDS or total > MAX_COMMANDS:
                if rounds > MAX_ROUNDS + 1:
                    break
                message = ("<event type=\"limit\">Too many commands for one player message; they were "
                           "not run. Do not send more commands: narrate what happened so far.</event>")
                continue
            errors = [e for e in (self.check(c) for c in cmds) if e]
            if errors:
                message = ("<rejected>\n" + "\n".join(errors) +
                           "\nNothing was executed and no turn was used.\n</rejected>")
                continue
            if not self.engine.alive:
                message = ("<event type=\"stopped\">The game is over and the engine is stopped; "
                           "nothing can be done until the player uses /undo, /load or /new.</event>")
                continue
            if not snapshot_taken:
                self.snapshot()  # /undo goes back to before this player message
                snapshot_taken = True
            message, changed = self.run_commands(cmds)
            view_changed |= changed
        if view_changed:
            self.footer()

    def footer(self):
        self.out.footer(self.engine.state)

    def event(self, kind: str, turn: Turn, note: str):
        self.map.observe(turn.state)
        parts = [f'<event type="{kind}">{note}</event>']
        if turn.text:
            parts.append(f"<engine>\n{turn.text}\n</engine>")
        if turn.view:
            parts.append(f"<view>\n{turn.view}\n</view>")
        parts.append("<state>\n" + self.state_text(turn.state) + "\n</state>")
        self.converse("\n".join(parts))
        self.footer()

    # app commands ----------------------------------------------------------------------

    def new_game(self, load: Optional[Path] = None):
        self.undo.clear()
        self.map = self.load_map(load) if load else AutoMap(self.game)
        t = self.engine.start(str(load) if load else None)
        self.start_claude()
        if load:
            self.event("resume", t, "The player resumed a saved game. Set the scene where they are "
                                    "in a few sentences.")
        else:
            self.event("start", t, f"A new game of {self.game.title} begins. Narrate the opening, "
                                   "including what the game says.")

    def save_path(self, name: str) -> Path:
        return self.saves / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.sav"

    def do_save(self, name: str):
        if not self.engine.alive:
            self.out.line("The game is over: there is nothing to save.", YELLOW)
            return
        path = self.save_path(name)
        if self.engine.save(path):
            self.save_map(path)
            self.out.line(f"Saved as '{name}'.", DIM)
        else:
            self.out.line("Could not save.", YELLOW)

    def do_load(self, name: str):
        path = self.save_path(name)
        if not path.exists():
            self.out.line(f"No saved game called '{name}'. /saves lists them.", YELLOW)
            return
        self.map = self.load_map(path)
        self.restore(path, "load", f"The player loaded the saved game '{name}'. "
                                   "Briefly set the scene where they are now.")

    def do_undo(self):
        if not self.undo:
            self.out.line("Nothing to undo.", YELLOW)
            return
        path = self.undo.pop()
        self.restore(path, "undo", "The player took back their last action: the game is back to how "
                                   "it was before it. Acknowledge in one short sentence and remind "
                                   "them where they are.")

    def restore(self, path: Path, kind: str, note: str):
        if self.engine.alive:
            t = self.engine.load(path)
        else:
            t = self.engine.start(str(path))
        self.event(kind, t, note)

    # the map is kept next to each saved game, since the engine's save knows nothing of it
    def save_map(self, save: Path):
        save.with_suffix(".map.json").write_text(json.dumps(self.map.to_json()))

    def load_map(self, save: Path) -> AutoMap:
        try:
            return AutoMap.from_json(self.game, json.loads(save.with_suffix(".map.json").read_text()))
        except (OSError, ValueError):
            return AutoMap(self.game)  # an older save without a map: start a new one

    def show_map(self):
        self.out.show_map(self.map.render(self.engine.state.get("room", -1), self.out.width))

    def list_saves(self):
        names = sorted(p.stem for p in self.saves.glob("*.sav"))
        self.out.line("Saved games: " + (", ".join(names) if names else "none"), DIM)

    def status(self) -> str:
        st = self.engine.state
        if not st:
            return ""
        if st.get("game_over"):
            where = "game over"
        elif st["dark"]:
            where = "darkness"
        else:
            where = scott.place_name(st["room_text"], 30)
        extra = (f" · {st['treasures_stored']}/{st['treasures_total']}" if st["treasures_total"]
                 else f" · turn {st['turn']}")
        usage = self.usage.short()
        return f"[{where}{extra}]" + (f"  {usage}" if usage else "")

    def handle(self, line: str) -> bool:
        """Handle one line of input. Returns False to quit."""
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("quit", "exit", "q"):
                return False
            elif cmd == "help":
                self.out.line(HELP, DIM)
            elif cmd == "undo":
                self.do_undo()
            elif cmd == "save":
                self.do_save(arg or "quick")
            elif cmd == "load":
                self.do_load(arg or "quick")
            elif cmd == "saves":
                self.list_saves()
            elif cmd == "new":
                self.new_game()
            elif cmd == "map":
                self.show_map()
            elif cmd == "usage":
                self.out.line(self.usage.report(self.args.model), DIM)
            else:
                self.out.line(f"Unknown command /{cmd}. /help lists the commands.", YELLOW)
            return True
        direct = self.direct_command(line)
        if direct:
            self.run_direct(direct)
            return True
        message = (f"<player>{line}</player>\n<state>\n{self.state_text(self.engine.state)}\n</state>")
        self.turn(message)
        return True

    def close(self) -> bool:
        saved = self.engine.alive and self.engine.save(self.save_path("autosave"))
        if saved:
            self.save_map(self.save_path("autosave"))
        self.engine.close()
        if self.claude:
            self.claude.close()
        shutil.rmtree(self.undo_dir, ignore_errors=True)
        self.logfile.close()
        return saved


def main():
    games = sorted(p.stem for p in GAMES.glob("*.dat"))
    ap = argparse.ArgumentParser(description="Play Scott Adams adventures with Claude as narrator.")
    ap.add_argument("game", nargs="?", default="adv01", help="game file name (default adv01)")
    ap.add_argument("--model", default=os.environ.get("CLAUDVENTURE_MODEL", "sonnet"),
                    help="Claude model: sonnet (default), haiku, opus...")
    ap.add_argument("--effort", default=os.environ.get("CLAUDVENTURE_EFFORT", "low"))
    ap.add_argument("--load", metavar="NAME", help="resume a saved game (e.g. autosave)")
    ap.add_argument("--seed", type=int, help="fix the engine's random numbers (testing)")
    ap.add_argument("--script", metavar="FILE", help="read player input from a file (testing)")
    ap.add_argument("--always-interpret", action="store_true",
                    help="let Claude interpret every input, even plain game commands like 'n' or 'get axe'")
    ap.add_argument("--plain", action="store_true",
                    help="scrolling text instead of the full-screen panels")
    ap.add_argument("--list", action="store_true", help="list the available games")
    args = ap.parse_args()

    if args.list:
        for g in games:
            print(f"  {g:10} {scott.TITLES.get(g, '')}")
        return 0
    if args.game not in games:
        print(f"Unknown game '{args.game}'. Available: {', '.join(games)}")
        return 1
    if not (args.plain or args.script):
        try:
            import tui
        except ImportError:
            print("The full-screen interface needs Textual: python3 -m pip install textual\n"
                  "(or use --plain). Starting in plain mode.\n")
        else:
            return tui.run(args)
    return run_plain(args)


def run_plain(args) -> int:
    script = None
    if args.script:
        script = [l.rstrip("\n") for l in open(args.script) if l.strip() and not l.startswith("#")]
    else:
        try:
            import readline  # noqa: F401  (line editing and history for input())
            hist = SAVES / ".history"
            SAVES.mkdir(exist_ok=True)
            if hist.exists():
                readline.read_history_file(hist)
        except Exception:
            readline = None

    s = Session(args)
    out = s.out
    out.line(f"Claudventure · {s.game.title}  (/help for commands)", BOLD)
    try:
        load = s.save_path(args.load) if args.load else None
        if load and not load.exists():
            out.line(f"No saved game called '{args.load}'; starting a new game.", YELLOW)
            load = None
        s.new_game(load)
        while True:
            if script is not None:
                if not script:
                    break
                line = script.pop(0)
                print(f"\n{s.status()}\n> {line}")
            else:
                # Plain prompt: libedit (macOS) mishandles colour codes inside it
                print(f"\n{DIM}{s.status()}{RESET}")
                try:
                    line = input("> ").strip()
                except EOFError:
                    break
            if not line:
                continue
            if not s.handle(line):
                break
    except KeyboardInterrupt:
        print()
    except RuntimeError as e:
        out.line(f"Error: {e}", YELLOW)
    finally:
        saved = s.close()
        if script is None and 'readline' in sys.modules:
            try:
                sys.modules["readline"].write_history_file(SAVES / ".history")
            except Exception:
                pass
    if saved:
        out.line("Your game was saved as 'autosave' (./claudventure.sh --load autosave to continue).", DIM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
