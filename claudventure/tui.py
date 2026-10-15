"""Full-screen interface (Textual): header, story, exits and items on the left, map and game state on the right.

The game itself (engine, Claude, undo, saves) is the same Session used by the
plain mode; it runs in a worker thread and draws through TuiOutput.
"""

from typing import List, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

import scott
from app import BOLD, CYAN, DIM, GREEN, YELLOW, Session

# the narrator's asides (CYAN) in a pastel cyan, like the borders
STYLES = {DIM: "dim", CYAN: "#97D8D8", YELLOW: "yellow", BOLD: "bold", GREEN: "green", "": ""}
ITEM = "#A2CDA2"  # items, here and carried: a soft green

# border colors: the theme's hues, paler and less saturated (variable -> saturation, lightness)
PASTELS = {"primary": (0.45, 0.72), "secondary": (0.30, 0.58), "accent": (0.65, 0.76)}


class TuiOutput:
    """The Printer interface used by Session, drawing into the app (from the worker thread)."""

    def __init__(self, app: "Claudventure"):
        self.app = app
        self.style = ""

    @property
    def width(self) -> int:
        """Text width of the map window (90% of the screen, minus border and padding)."""
        return max(40, int(self.app.size.width * 0.9) - 4)

    def _call(self, fn, *args):
        self.app.call_from_thread(fn, *args)

    def dots(self):
        self._call(self.app.set_busy, True)

    def set_style(self, style: str):
        self.style = style

    def write(self, text: str):
        self._call(self.app.story_write, text, STYLES.get(self.style, ""))

    def end(self):
        self.style = ""  # as Printer.end: a style lasts until the end of its block
        self._call(self.app.story_end)

    def line(self, text: str, style: str = ""):
        self._call(self.app.story_line, text, STYLES.get(style, ""))

    def footer(self, st: dict):
        self._call(self.app.refresh_panels)  # exits and items live in the side panels

    def show_map(self, text: str):
        self._call(self.app.open_map, text)


class CommandInput(Input):
    """Input line with history (Up / Down)."""

    BINDINGS = [Binding("up", "history(-1)", show=False), Binding("down", "history(1)", show=False)]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.history: List[str] = []
        self.pos = 0

    def remember(self, line: str):
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self.pos = len(self.history)

    def action_history(self, step: int):
        if not self.history:
            return
        self.pos = max(0, min(len(self.history), self.pos + step))
        self.value = self.history[self.pos] if self.pos < len(self.history) else ""
        self.cursor_position = len(self.value)


class MapScreen(ModalScreen):
    """The whole map, over the game. Esc closes it."""

    BINDINGS = [Binding("escape,q,m", "dismiss", "close")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        body = Text(no_wrap=True)  # the drawing must not wrap: it scrolls sideways instead
        for line in self.text.split("\n"):
            if line.startswith("Level "):
                body.append(line + "\n", style="bold")
                continue
            start = line.find("<")
            end = line.find(">", start)
            if start >= 0 and end > start:
                body.append(line[:start])
                body.append(line[start:end + 1], style="bold yellow")
                body.append(line[end + 1:] + "\n")
            else:
                body.append(line + "\n")
        with ScrollableContainer(id="fullmap") as box:
            box.border_title = "Map"
            box.border_subtitle = "Esc to close"
            yield Static(body, id="fullmap-body")


class Claudventure(App):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #left { width: 1fr; }
    #header { height: auto; border: round $pastel-primary; padding: 0 1; text-style: bold; }
    #story { height: 1fr; border: round $pastel-primary; padding: 0 1; }
    #story > Static { margin: 0 0 1 0; }
    #exits { height: auto; border: round $pastel-primary; padding: 0 1; }
    #items { height: auto; min-height: 7; max-height: 14; border: round $pastel-primary; padding: 0 1; }
    #side { width: 42; }
    #map { height: 1fr; border: round $pastel-secondary; padding: 0 1; }
    #inv, #score { height: auto; border: round $pastel-secondary; padding: 0 1; }
    #header, #exits, #items, #map, #inv, #score { color: $text-muted; }
    #cmd { border: round $pastel-accent; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    MapScreen { align: center middle; }
    #fullmap { width: 90%; height: 90%; border: round $pastel-accent; padding: 0 1; background: $surface; }
    #fullmap-body { width: auto; }
    #story, #fullmap {
        scrollbar-background: $surface; scrollbar-background-hover: $surface;
        scrollbar-background-active: $surface; scrollbar-color: $pastel-secondary 45%;
        scrollbar-color-hover: $pastel-secondary 65%; scrollbar-color-active: $pastel-secondary 85%;
    }
    """

    BINDINGS = [
        Binding("pageup", "scroll_story(-1)", "scroll up", show=False),
        Binding("pagedown", "scroll_story(1)", "scroll down", show=False),
        Binding("ctrl+q", "quit", "quit", show=False),
    ]

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.session = Session(args, out=TuiOutput(self))
        self.block: Optional[Static] = None
        self.block_text: Optional[Text] = None
        self.busy = False

    # layout -------------------------------------------------------------------

    def get_css_variables(self):
        variables = super().get_css_variables()
        for name, (sat, light) in PASTELS.items():
            hsl = Color.parse(variables[name]).hsl
            variables[f"pastel-{name}"] = Color.from_hsl(hsl.h, sat, light).hex
        return variables

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static(f"Claudventure · {self.session.game.title}", id="header")
                yield VerticalScroll(id="story")
                for wid, title in (("exits", "Exits"), ("items", "Items here")):
                    panel = Static(id=wid)
                    panel.border_title = title
                    yield panel
            with Vertical(id="side"):
                for wid, title in (("map", "Map"), ("inv", "Carrying"), ("score", "Score")):
                    panel = Static(id=wid)
                    panel.border_title = title
                    yield panel
        yield CommandInput(id="cmd", placeholder="What do you want to do?  (/help for commands)")
        yield Static(id="status")

    def on_mount(self):
        self.query_one("#cmd").focus()
        load = None
        if self.args.load:
            load = self.session.save_path(self.args.load)
            if not load.exists():
                self.story_line(f"No saved game called '{self.args.load}'; starting a new game.",
                                "yellow")
                load = None
        self.run_game(lambda: self.session.new_game(load))

    # running the game in a worker thread ------------------------------------------

    @work(thread=True, exclusive=True)
    def run_game(self, job):
        try:
            keep_going = job()
        except RuntimeError as e:
            self.call_from_thread(self.story_line, f"Error: {e}", "yellow")
            keep_going = True
        self.call_from_thread(self.job_done, keep_going is not False)

    def job_done(self, keep_going: bool):
        self.story_end()
        self.set_busy(False)
        self.refresh_panels()
        if not keep_going:
            self.exit()

    def on_input_submitted(self, event: Input.Submitted):
        line = event.value.strip()
        cmd = self.query_one("#cmd", CommandInput)
        cmd.value = ""
        if not line or self.busy:
            return
        cmd.remember(line)
        self.story_line("> " + line, "bold")
        self.set_busy(True)
        self.run_game(lambda: self.session.handle(line))

    # story panel ------------------------------------------------------------------

    def story_write(self, text: str, style: str):
        if self.block is None:
            self.block_text = Text()
            self.block = Static(self.block_text)
            self.query_one("#story").mount(self.block)
        self.block_text.append(text, style=style or None)
        self.block.update(self.block_text)
        self.query_one("#story").scroll_end(animate=False)

    def story_end(self):
        if self.block_text is not None:
            self.block_text.rstrip()
            self.block.update(self.block_text)
        self.block = None
        self.block_text = None

    def story_line(self, text: str, style: str = ""):
        self.story_end()
        self.story_write(text, style)
        self.story_end()

    def action_scroll_story(self, direction: int):
        story = self.query_one("#story")
        story.scroll_page_down() if direction > 0 else story.scroll_page_up()

    # side panels -------------------------------------------------------------------

    def set_busy(self, busy: bool):
        self.busy = busy
        self.refresh_status()

    def refresh_status(self):
        usage = self.session.usage.short()
        state = "the narrator is thinking…" if self.busy else "/help · /map · PgUp/PgDn scroll · Ctrl+Q quit"
        self.query_one("#status", Static).update(Text(f"{usage}   {state}" if usage else state))

    def refresh_panels(self):
        s = self.session
        st = s.engine.state
        self.refresh_status()
        if not st:
            return
        g = s.game

        map_panel = self.query_one("#map", Static)
        w = max(10, map_panel.size.width - 2)
        h = max(3, map_panel.size.height)
        title, lines = s.map.level_view(st["room"], w, h)
        map_panel.border_title = title
        body = Text()
        for line in lines:
            start = line.find("<")
            end = line.find(">", start)
            if start >= 0 and end > start:
                body.append(line[:start])
                body.append(line[start:end + 1], style="bold yellow")
                body.append(line[end + 1:] + "\n")
            else:
                body.append(line + "\n")
        map_panel.update(body)

        story = self.query_one("#story")
        exits, items = Text(), Text()
        if st.get("game_over"):
            story.border_title = "Game over"
            exits.append("/undo, /load or /new", style="yellow")
        elif st["dark"]:
            story.border_title = "Darkness"
            exits.append("It is too dark to see.")
        else:
            story.border_title = scott.place_name(st["room_text"], max(10, story.size.width - 6))
            exits.append(", ".join(st["exits"]) or "none")
            for i, item in enumerate(st["items_here"]):
                items.append(("\n" if i else "") + " ".join(item.split()), style=ITEM)
            if not st["items_here"]:
                items.append("nothing")
        self.query_one("#exits", Static).update(exits)
        self.query_one("#items", Static).update(items)

        inv = Text()
        for i, item in enumerate(st["inventory"]):
            inv.append(("\n" if i else "") + " ".join(item.split()), style=ITEM)
        if not st["inventory"]:
            inv.append("nothing")
        inv_panel = self.query_one("#inv", Static)
        inv_panel.border_title = f"Carrying {len(st['inventory'])}/{g.max_carry}"
        inv_panel.update(inv)

        score = []
        if st["treasures_total"]:
            score.append(f"Treasures {st['treasures_stored']}/{st['treasures_total']}")
        score.append(f"Turn {st['turn']}")
        if g.has_darkness:
            score.append("Light on" if st["lamp_lit"] else "No light")
        self.query_one("#score", Static).update(" · ".join(score))

    def on_resize(self, event):
        self.refresh_panels()

    def open_map(self, text: str):
        self.push_screen(MapScreen(text))


def run(args) -> int:
    app = Claudventure(args)
    try:
        app.run()
    finally:
        saved = app.session.close()
    if saved:
        print("Your game was saved as 'autosave' (./claudventure.sh --load autosave to continue).")
    return 0
