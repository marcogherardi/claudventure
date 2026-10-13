"""Map of the places the player has visited, drawn as simple ASCII, one level per height.

Rooms are laid out on a grid by following the exits (North = up the page,
East = right, Up/Down = another level). Old adventures are not always
geometric: passages that do not fit the grid, loops and magic transports are
listed under the map instead of drawn.
"""

from collections import deque
from typing import Dict, List, Optional, Tuple

import scott

DELTA = [(0, -1, 0), (0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)]  # N S E W U D
SHORT = "NSEWUD"
OPPOSITE = [1, 0, 3, 2, 5, 4]
CELL = 7  # characters per grid column: box of up to 5 + connector


class AutoMap:
    def __init__(self, game: scott.Game):
        self.game = game
        self.order: List[int] = []                 # rooms in order of first visit
        self.seen: Dict[int, bool] = {}            # room -> seen in the light
        self.items: Dict[int, List[str]] = {}      # room -> items seen there last time
        self.links: List[Tuple[int, str, int]] = []  # moves not explained by exits

    # recording -----------------------------------------------------------------

    def observe(self, state: dict, cmd: Optional[str] = None, before: Optional[int] = None):
        """Note the room in this engine state; cmd/before describe the move that led here."""
        room = state.get("room")
        if room is None or state.get("game_over"):
            return
        if room not in self.seen:
            self.order.append(room)
            self.seen[room] = False
        if not state.get("dark"):
            self.seen[room] = True
            self.items[room] = list(state.get("items_here", []))
        if cmd and before is not None and before != room and room != len(self.game.rooms) - 1:
            vb, no, _ = self.game.parse_command(cmd)
            regular = vb == 1 and no is not None and 1 <= no <= 6 and \
                self.game.rooms[before].exits[no - 1] == room
            link = (before, cmd, room)
            if not regular and link not in self.links:
                self.links.append(link)

    def to_json(self) -> dict:
        return {"order": self.order, "seen": {str(k): v for k, v in self.seen.items()},
                "items": {str(k): v for k, v in self.items.items()},
                "links": [list(l) for l in self.links]}

    @classmethod
    def from_json(cls, game: scott.Game, data: dict) -> "AutoMap":
        m = cls(game)
        m.order = list(data.get("order", []))
        m.seen = {int(k): v for k, v in data.get("seen", {}).items()}
        m.items = {int(k): v for k, v in data.get("items", {}).items()}
        m.links = [tuple(l) for l in data.get("links", [])]
        return m

    # layout ----------------------------------------------------------------------

    def _exits(self, room: int) -> List[int]:
        """Exits the player knows about (they are only shown in the light)."""
        return self.game.rooms[room].exits if self.seen.get(room) else [0] * 6

    def _layout(self) -> Dict[int, Tuple[int, int, int]]:
        pos: Dict[int, Tuple[int, int, int]] = {}
        taken: Dict[Tuple[int, int, int], int] = {}
        visited = set(self.order)

        def place(room, want):
            cell = want
            radius = 0
            while cell in taken:  # spiral outwards on the same level
                radius += 1
                ring = [(want[0] + dx, want[1] + dy, want[2])
                        for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)
                        if max(abs(dx), abs(dy)) == radius]
                free = [c for c in ring if c not in taken]
                if free:
                    cell = free[0]
            pos[room] = cell
            taken[cell] = room

        for start in self.order:
            if start in pos:
                continue
            # a new area, not joined by exits: next to the place it was reached from, if known
            src = next((a for a, _, b in self.links if b == start and a in pos), None)
            if src is not None:
                x, y, z = pos[src]
                place(start, (x + 1, y, z))
            else:
                right = max((p[0] for p in pos.values()), default=-2) + 2
                place(start, (right, 0, 0))
            queue = deque([start])
            while queue:
                r = queue.popleft()
                x, y, z = pos[r]
                for d in range(6):
                    targets = []
                    t = self._exits(r)[d]
                    if t in visited and t not in pos:
                        targets.append((t, DELTA[d]))
                    for other in visited:  # rooms whose exits lead here
                        if other not in pos and self._exits(other)[OPPOSITE[d]] == r:
                            targets.append((other, DELTA[d]))
                    for room, (dx, dy, dz) in targets:
                        if room not in pos:
                            place(room, (x + dx, y + dy, z + dz))
                            queue.append(room)
        return pos

    # drawing ---------------------------------------------------------------------

    def _draw(self, current: int):
        """Grid drawings of every level: ([(z, title, lines)], drawn exits, positions, numbers)."""
        pos = self._layout()
        num = {room: i + 1 for i, room in enumerate(self.order)}
        nw = len(str(len(self.order)))
        min_x = min(p[0] for p in pos.values())
        min_y = min(p[1] for p in pos.values())

        at = {p: r for r, p in pos.items()}

        def neighbour(r, d):
            x, y, z = pos[r]
            dx, dy, dz = DELTA[d]
            return at.get((x + dx, y + dy, z + dz))

        drawn = set()   # (room, direction): exits shown on the map as a line or level mark
        drawings = []
        levels = sorted({p[2] for p in pos.values()}, reverse=True)
        for z in levels:
            rooms = [r for r in self.order if pos[r][2] == z]
            cols = max(pos[r][0] for r in rooms) - min_x + 1
            rows = max(pos[r][1] for r in rooms) - min_y + 1
            grid = [[" "] * (cols * CELL) for _ in range(rows * 2)]
            for r in rooms:
                x, y = pos[r][0] - min_x, pos[r][1] - min_y
                ex = self._exits(r)
                mark = "↕" if ex[4] and ex[5] else "^" if ex[4] else "v" if ex[5] else ""
                left, right = ("<", ">") if r == current else ("[", "]")
                box = f"{left}{num[r]:>{nw}}{mark}{right}"
                for i, ch in enumerate(box):
                    grid[y * 2][x * CELL + i] = ch
                for d in (4, 5):  # up/down to the room right above/below: the mark says it
                    if ex[d] and neighbour(r, d) == ex[d]:
                        drawn.add((r, d))
                for d in (2, 1):  # East and South: draw a line to the neighbour
                    o = neighbour(r, d)
                    if o is None:
                        continue
                    there, back = ex[d] == o, self._exits(o)[OPPOSITE[d]] == r
                    if not (there or back):
                        continue
                    if there:
                        drawn.add((r, d))
                    if back:
                        drawn.add((o, OPPOSITE[d]))
                    if d == 2:
                        for i in range(len(box), CELL):
                            grid[y * 2][x * CELL + i] = "─"
                    else:
                        grid[y * 2 + 1][x * CELL + 1 + (nw - 1) // 2] = "│"
            title = "Level 0 (where you started)" if z == 0 else f"Level {z:+d}" + (
                " (up)" if z > 0 else " (down)")
            drawings.append((z, title, ["".join(row).rstrip() for row in grid
                                        if "".join(row).strip()]))
        return drawings, drawn, pos, num

    def render(self, current: int, width: int = 86) -> str:
        """The whole map: every level, then the list of places."""
        if not self.order:
            return "You have not been anywhere yet."
        drawings, drawn, pos, num = self._draw(current)
        nw = len(str(len(self.order)))
        out: List[str] = []
        for _, title, lines in drawings:
            out.append(title)
            out.extend("  " + line for line in lines)
            out.append("")
        out.append("Places (you are at <" + str(num.get(current, "?")) + ">;"
                   " ^ v ↕ = a way up / down):")
        for r in self.order:
            name = scott.place_name(self.game.rooms[r].text) if self.seen[r] else "(seen only in the dark)"
            line = f" {num[r]:>{nw}}  {name}"
            if self.items.get(r):
                line += "  —  " + ", ".join(" ".join(i.split()) for i in self.items[r])
            notes = []
            ex = self._exits(r)
            unexplored = [SHORT[d] for d in range(6) if ex[d] and ex[d] not in pos]
            loops = [SHORT[d] for d in range(6) if ex[d] == r]
            odd = [f"{SHORT[d]}→{num[ex[d]]}" for d in range(6)
                   if ex[d] and ex[d] != r and ex[d] in pos and (r, d) not in drawn]
            if unexplored:
                notes.append("unexplored " + ",".join(unexplored))
            if loops:
                notes.append(",".join(loops) + " lead back here")
            if odd:
                notes.append("also " + " ".join(odd))
            if notes:
                line += f"  ({'; '.join(notes)})"
            out.append(_clip(line, width))
        if self.links:
            out.append("")
            out.append("Other ways you found:")
            for a, cmd, b in self.links:
                if a in num and b in num:
                    out.append(f"  {num[a]} → {num[b]}  by {cmd}")
        if any(self.items.values()):
            out.append("")
            out.append("(Items are as you last saw them.)")
        return "\n".join(out)


    def level_view(self, current: int, width: int, height: int) -> Tuple[str, List[str]]:
        """The level the player is on, cropped around them, plus the names of its places."""
        if current not in self.seen:
            return "Map", []
        drawings, _, pos, num = self._draw(current)
        z = pos[current][2]
        title, lines = next((t, l) for zz, t, l in drawings if zz == z)
        names = [f"{num[r]} {scott.place_name(self.game.rooms[r].text, 22) if self.seen[r] else '(dark)'}"
                 for r in self.order if pos[r][2] == z]
        room_rows = max(1, height - min(len(names), height // 2))
        row = next(i for i, l in enumerate(lines) if "<" in l)
        col = lines[row].index("<")
        top = max(0, min(row - room_rows // 2, len(lines) - room_rows))
        left = max(0, min(col - width // 2, max(len(l) for l in lines) - width))
        view = [l[left:left + width] for l in lines[top:top + room_rows]]
        legend = [_clip(n, width) for n in names][:max(0, height - len(view) - 1)]
        return title, view + ([""] + legend if legend else [])


def _clip(line: str, width: int) -> str:
    return line if len(line) <= width else line[:width - 1] + "…"
