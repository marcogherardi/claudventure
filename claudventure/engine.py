"""Drives the ScottFree interpreter in headless mode (-j) over pipes."""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "src" / "ScottCurses"

LOOK_START, LOOK_END, STATE_MARK = "\x02", "\x03", "\x1e"


@dataclass
class Turn:
    text: str                      # engine output, room descriptions removed
    looks: List[str] = field(default_factory=list)  # room descriptions printed
    state: dict = field(default_factory=dict)

    @property
    def view(self) -> Optional[str]:
        """The last room description shown this turn, if any."""
        return self.looks[-1] if self.looks else None


class Engine:
    def __init__(self, game_path, seed: Optional[int] = None):
        self.game_path = str(game_path)
        self.seed = seed
        self.proc: Optional[subprocess.Popen] = None
        self.state: dict = {}

    def start(self, savefile: Optional[str] = None) -> Turn:
        self.close()
        env = dict(os.environ)
        if self.seed is not None:
            env["SCOTTFREE_SEED"] = str(self.seed)
        args = [str(BINARY), "-j", self.game_path] + ([savefile] if savefile else [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=env, cwd=str(ROOT))
        return self._read_turn()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, line: str) -> Turn:
        """Send one input line and return what the engine printed for it."""
        if not self.alive:
            raise RuntimeError("the game is not running")
        self.proc.stdin.write((line.strip() + "\n").encode("latin-1", "replace"))
        self.proc.stdin.flush()
        return self._read_turn()

    def save(self, path) -> bool:
        return "Saved." in self.send(f"#save {path}").text

    def load(self, path) -> Turn:
        return self.send(f"#load {path}")

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def _read_turn(self) -> Turn:
        buf = bytearray()
        state = None
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:  # engine exited without a final state
                break
            if ch == STATE_MARK.encode():
                line = self.proc.stdout.readline()
                state = json.loads(line.decode("latin-1"))
                break
            buf += ch
        if state is None:
            state = dict(self.state, game_over=True)
        self.state = state
        if state.get("game_over"):
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return _split(buf.decode("latin-1"), state)


def _split(raw: str, state: dict) -> Turn:
    text, looks = [], []
    rest = raw
    while LOOK_START in rest:
        before, _, after = rest.partition(LOOK_START)
        look, _, rest = after.partition(LOOK_END)
        text.append(before)
        looks.append(look.strip())
    text.append(rest)
    joined = "".join(text)
    lines = [l.rstrip() for l in joined.split("\n")]
    cleaned = "\n".join(lines).strip()
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return Turn(cleaned, looks, state)
