"""Finder popup only. Discovery and navigation belong to the caller."""

from __future__ import annotations

from collections.abc import Callable
import curses
from datetime import datetime
import locale
import json
from queue import Empty, SimpleQueue
from threading import Thread
import time
import unicodedata

from . import cockpit, runtime, sessions, tmux, tui
from .finder import FILTER_STATES, SCOPES, Finder, Row
from .names import WindowTarget
from .workspace import Identity, Workspace

FINDER_SCOPE_OPTION = "@letee_finder_scope"
FINDER_FILTERS_OPTION = "@letee_finder_filters"


def _load_filters(value: str) -> tuple[frozenset[str], bool]:
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return frozenset(), False
    if (not isinstance(data, dict) or not isinstance(data.get("states"), list)
            or not isinstance(data.get("has_bell"), bool)
            or any(not isinstance(state, str) or state not in FILTER_STATES for state in data["states"])):
        return frozenset(), False
    return frozenset(data["states"]), data["has_bell"]


def _safe(text: str) -> str:
    return "".join(char if char.isprintable() else " " for char in text)


class _Popup:
    def __init__(self, screen, workspace: Workspace):
        self.screen = screen
        self.finder = Finder(workspace)
        self.cursor = 0
        self.query_start = 0
        self.scroll = 0
        self.error = ""
        self.opening: Identity | None = None
        self.results: SimpleQueue[tuple[Callable[[], None] | None, SystemExit | Exception | None]] = SimpleQueue()
        self.tabs: list[tuple[int, int, str]] = []
        self.disclosures: dict[int, tuple[int, int]] = {}
        self.height = self.width = self.capacity = 0
        self.follow_selection = True
        self.filters_open = False
        self.filter_index = 0
        self.filter_scroll = 0

    def toggle_filter(self) -> None:
        states = set(self.finder.states)
        has_bell = self.finder.has_bell
        if self.filter_index == 0:
            has_bell = not has_bell
        elif self.filter_index == len(FILTER_STATES) + 1:
            states.clear()
            has_bell = False
        else:
            state = FILTER_STATES[self.filter_index - 1]
            states.symmetric_difference_update({state})
        self.finder.set_filters(states, has_bell)
        self.follow_selection = True

    def follow_filter(self) -> None:
        self.filter_scroll = min(self.filter_scroll, self.filter_index)
        self.filter_scroll = max(self.filter_scroll, self.filter_index - max(1, self.capacity) + 1)

    def put(self, y: int, x: int, text: str, attr: int = 0) -> int:
        if not (0 <= y < self.height and 0 <= x < self.width - 1):
            return 0
        text = tui.truncate_cells(_safe(text), self.width - x - 1)
        if not text:
            return 0
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass  # A resize can invalidate coordinates between getmaxyx and addstr.
        return tui.cell_width(text)

    def render(self) -> None:
        self.height, self.width = self.screen.getmaxyx()
        self.capacity = max(0, self.height - 6)
        rows = self.finder.rows
        if self.follow_selection and self.finder.selected_row in rows and self.capacity:
            index = rows.index(self.finder.selected_row)
            self.scroll = min(self.scroll, index)
            self.scroll = max(self.scroll, index - self.capacity + 1)
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - self.capacity)))
        self.follow_selection = False
        self.screen.erase()
        self.put(0, 0, "> ", tui.color("title"))
        query_width = max(0, self.width - 3)
        self.query_start = min(self.query_start, self.cursor)
        while (self.query_start < self.cursor
               and tui.cell_width(self.finder.query[self.query_start:self.cursor]) >= query_width):
            self.query_start += 1
        self.put(0, 2, self.finder.query[self.query_start:])
        self.tabs = []
        x = 0
        for scope in SCOPES:
            label = f"[{scope}] "
            end = x + tui.cell_width(label)
            self.put(1, x, label, curses.A_REVERSE if scope == self.finder.scope else tui.color("hints"))
            self.tabs.append((x, min(end, self.width - 1), scope))
            x = end
        hints = ("Up/Down select  Space toggle  Enter/Esc close  Ctrl-C cancel" if self.filters_open
                 else "Ctrl-T scope  Ctrl-F filters  Tab expand  Enter open  Esc cancel")
        self.put(2, 0, hints, tui.color("hints"))
        filters = (["Has bell"] if self.finder.has_bell else [])
        filters += [state for state in FILTER_STATES if state in self.finder.states]
        self.put(3, 0, "Filters: " + (", ".join(filters) or "none"),
                 curses.A_REVERSE if self.filters_open else tui.color("hints"))
        if self.filters_open:
            labels = [("Has bell", self.finder.has_bell)]
            labels += [(state, state in self.finder.states) for state in FILTER_STATES]
            labels += [("Clear filters", None)]
            self.filter_scroll = max(0, min(self.filter_scroll, max(0, len(labels) - self.capacity)))
            for index in range(self.filter_scroll, min(len(labels), self.filter_scroll + self.capacity)):
                label, checked = labels[index]
                prefix = "" if checked is None else ("[x] " if checked else "[ ] ")
                self.put(4 + index - self.filter_scroll, 0, prefix + label,
                         curses.A_REVERSE if index == self.filter_index else 0)
        now = time.time()
        self.disclosures = {}
        visible_rows = [] if self.filters_open else rows[self.scroll:self.scroll + self.capacity]
        for index, row in enumerate(visible_rows, self.scroll):
            y = 4 + index - self.scroll
            selected = row.key == self.finder.selected
            attr = curses.A_REVERSE if selected else 0
            if not row.available:
                attr |= tui.color("unavailable") | curses.A_DIM
            prefix = "  " * row.depth
            arrow = ("v" if row.expanded else ">") if tui.ascii_mode() else ("▾" if row.expanded else "▸")
            self.put(y, 0, prefix + (arrow if row.has_children else " ") + " ", attr)
            arrow_x = tui.cell_width(prefix)
            if row.has_children:
                self.disclosures[y] = (arrow_x, index)
            x = arrow_x + 2
            x += self.put(y, x, "* " if row.current else "  ", attr | tui.color("active"))
            if row.agent is not None:
                x += self.put(y, x, tui.status_icon(row.agent.state, now) + " ",
                              attr | tui.status_attr(row.agent.state))
            # Group adjacent characters so combining marks stay with their base.
            chunks: list[tuple[str, int]] = []
            for position, char in enumerate(row.label):
                style = attr | (curses.A_BOLD | curses.A_UNDERLINE if position in row.highlights else 0)
                if chunks and (unicodedata.combining(char) or chunks[-1][1] == style):
                    chunks[-1] = (chunks[-1][0] + char, chunks[-1][1])
                else:
                    chunks.append((char, style))
            for text, style in chunks:
                x += self.put(y, x, text, style)
            if row.agent is not None and row.agent.activity_timestamp is not None:
                timestamp = row.agent.activity_timestamp
                elapsed = (datetime.now(timestamp.tzinfo) - timestamp).total_seconds()
                x += self.put(y, x, " " + tui.format_duration(elapsed), attr | curses.A_DIM)
            if (row.kind == "session" and row.session.bell) or (row.agent is not None and row.agent.alert):
                x += self.put(y, x, " BELL" if tui.ascii_mode() else " 🔔", attr | tui.color("danger"))
            if not row.available:
                self.put(y, x, " " + tui.icons()["unavailable"], attr)
        if not self.filters_open and not rows and self.capacity:
            self.put(4, 0, "Loading..." if self.finder.workspace.loading else "No destinations", tui.color("hints"))
        if self.height >= 6:
            self.put(self.height - 2, 0, self.finder.selected_path, tui.color("hints"))
            status = self.error
            if not status:
                if self.opening:
                    status = "Opening " + tui.spinner_frame(now)
                elif self.finder.unavailable:
                    status = "Unavailable"
                elif self.finder.workspace.loading:
                    status = "Loading..."
                elif not rows:
                    status = "No destinations"
            self.put(self.height - 1, 0, status, tui.color("danger") if self.error else tui.color("hints"))
        if self.height and self.width:
            cursor_x = min(self.width - 1, 2 + tui.cell_width(self.finder.query[self.query_start:self.cursor]))
            try:
                self.screen.move(0, cursor_x)
            except curses.error:
                pass
        try:
            self.screen.refresh()
        except curses.error:
            pass

    def open(self, prepare_open: Callable[[Row], Callable[[], None]]) -> None:
        if self.opening is not None:
            return
        row = self.finder.selected_row
        if row is None or not row.available:
            self.error = "Unavailable"
            return
        self.error = ""
        self.opening = row.key

        def worker() -> None:
            try:
                commit = prepare_open(row)
            except (SystemExit, Exception) as exc:
                self.results.put((None, exc))
            else:
                self.results.put((commit, None))

        Thread(target=worker, name="letee-finder-open", daemon=True).start()

    def mouse(self, prepare_open: Callable[[Row], Callable[[], None]]) -> None:
        try:
            _, x, y, _, buttons = curses.getmouse()
        except curses.error:
            return
        if self.filters_open:
            if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
                self.filter_scroll = max(0, self.filter_scroll - 3)
            elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                self.filter_scroll += 3
            elif buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
                if 4 <= y < 4 + self.capacity and 0 <= x < self.width - 1:
                    index = self.filter_scroll + y - 4
                    if index < len(FILTER_STATES) + 2:
                        self.filter_index = index
                        self.toggle_filter()
            return
        if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
            self.scroll = max(0, self.scroll - 3)
        elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
            self.scroll += 3
        elif buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED):
            if y == 1:
                for start, end, scope in self.tabs:
                    if start <= x < end:
                        self.finder.set_scope(scope)
                        self.follow_selection = True
                        break
            elif y == 3 and 0 <= x < self.width - 1:
                self.filters_open = True
                self.follow_filter()
            elif 4 <= y < 4 + self.capacity:
                index = self.scroll + y - 4
                if index < len(self.finder.rows) and 0 <= x < self.width - 1:
                    row = self.finder.rows[index]
                    self.finder.select(row.key)
                    self.follow_selection = True
                    disclosure = self.disclosures.get(y)
                    if disclosure and x == disclosure[0]:
                        self.finder.toggle(row.key)
                    else:
                        self.open(prepare_open)

    def key(self, key, prepare_open: Callable[[Row], Callable[[], None]]) -> bool:
        if key == "\x03":
            return False
        if self.filters_open:
            if key in ("\n", "\r", curses.KEY_ENTER, "\x1b", "\x06"):
                self.filters_open = False
                self.follow_selection = True
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                self.filter_index = max(0, min(len(FILTER_STATES) + 1,
                                             self.filter_index + (-1 if key == curses.KEY_UP else 1)))
                self.follow_filter()
            elif key == " ":
                self.toggle_filter()
            elif key == curses.KEY_MOUSE:
                self.mouse(prepare_open)
            elif key == curses.KEY_RESIZE:
                self.height, self.width = self.screen.getmaxyx()
                self.capacity = max(0, self.height - 6)
                self.follow_filter()
            return True
        if key == "\x1b":
            return False
        if key == "\x06":
            self.filters_open = True
            self.follow_filter()
        elif key == curses.KEY_MOUSE:
            self.mouse(prepare_open)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.open(prepare_open)
        elif key == curses.KEY_RESIZE:
            self.follow_selection = True
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            self.finder.move(-1 if key == curses.KEY_UP else 1)
            self.follow_selection = True
        elif key == "\t":
            self.finder.toggle()
            self.follow_selection = True
        elif key == "\x14":
            self.finder.cycle_scope()
            self.follow_selection = True
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.finder.query), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.finder.query)
        else:
            query = self.finder.query
            if key == "\x15":
                query, self.cursor = "", 0
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if self.cursor:
                    query = query[:self.cursor - 1] + query[self.cursor:]
                    self.cursor -= 1
            elif key == curses.KEY_DC:
                query = query[:self.cursor] + query[self.cursor + 1:]
            elif isinstance(key, str) and key.isprintable():
                query = query[:self.cursor] + key + query[self.cursor:]
                self.cursor += len(key)
            if query != self.finder.query:
                self.finder.set_query(query)
                self.follow_selection = True
        return True


def run(
    stdscr,
    workspace: Workspace,
    *,
    read_workspace: Callable[[], Workspace | None] | None = None,
    prepare_open: Callable[[Row], Callable[[], None]],
) -> bool:
    """Run without discovery I/O. read_workspace must consume updates nonblockingly.

    prepare_open runs in the background and must not navigate, focus, or use
    curses. It returns a quick local commit, called here only while the original
    destination is still available. Cancellation abandons preparation without
    invoking its commit.
    """
    scope = tmux.out("show-options", "-qv", "-t", tmux.SESSION, FINDER_SCOPE_OPTION)
    filters = tmux.out("show-options", "-qv", "-t", tmux.SESSION, FINDER_FILTERS_OPTION)
    popup = _Popup(stdscr, workspace)
    popup.finder.set_filters(*_load_filters(filters))
    if scope in SCOPES:
        popup.finder.set_scope(scope)
    tui.init_colors()
    stdscr.keypad(True)
    stdscr.timeout(50)
    old_cursor = None
    old_mouse = None
    pending = None
    old_escdelay = curses.get_escdelay()
    curses.set_escdelay(25)
    try:
        try:
            old_cursor = curses.curs_set(1)
            _, old_mouse = curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        while True:
            if read_workspace is not None:
                update = read_workspace()
                if update is not None:
                    popup.finder.update(update)
            if pending is None:
                try:
                    pending = popup.results.get_nowait()
                except Empty:
                    pass
            popup.render()
            stdscr.timeout(0 if pending is not None else 50)
            try:
                key = stdscr.get_wch()
            except curses.error:
                key = -1
            except KeyboardInterrupt:
                return False
            if key != -1:
                if not popup.key(key, prepare_open):
                    return False
                continue
            if pending is None:
                continue
            # A ready result must not overtake queued edits or cancellation.
            commit, error = pending
            pending = None
            destination = popup.finder.destination(popup.opening)
            popup.opening = None
            if error is None and (destination is None or not destination.available):
                popup.error = "Unavailable"
            else:
                if error is None:
                    try:
                        commit()
                    except (SystemExit, Exception) as exc:
                        error = exc
                    else:
                        return True
                popup.error = "Open failed: " + (str(error) or type(error).__name__)
    finally:
        curses.set_escdelay(old_escdelay)
        stdscr.timeout(-1)
        stdscr.keypad(False)
        try:
            if old_cursor is not None:
                curses.curs_set(old_cursor)
            if old_mouse is not None:
                curses.mousemask(old_mouse)
        except curses.error:
            pass
        tmux.tmux("set-option", "-t", tmux.SESSION, FINDER_SCOPE_OPTION, popup.finder.scope, capture=True)
        filters = json.dumps({"states": sorted(popup.finder.states), "has_bell": popup.finder.has_bell})
        tmux.tmux("set-option", "-t", tmux.SESSION, FINDER_FILTERS_OPTION, filters, capture=True)


def _window_target(row: Row) -> WindowTarget:
    return WindowTarget(row.session.target, row.session.session_id,
                        row.window.window_id, row.session.socket_path)


def _prepare_destination(row: Row, current_workspace: Callable[[], Workspace]) -> Callable[[], None]:
    """Validate an existing destination without changing either workspace's focus."""
    if row.agent is not None:
        command = sessions.existing_pane_attach_command(row.agent.pane_target)
    elif row.window is not None:
        command = sessions.window_attach_command(_window_target(row))
    else:
        command = sessions.existing_attach_command(row.session.target, row.session.session_id, row.session.socket_path)

    def commit() -> None:
        # Refresh display metadata too, so a rename during preparation does not
        # install an obsolete current-target marker in the existing sidebar.
        latest = Finder(current_workspace()).destination(row.key)
        if latest is None or not latest.available:
            raise SystemExit("Destination is unavailable")
        if latest.agent is not None:
            cockpit.open_agent(latest.agent.pane_target, latest.agent.agent_id, command)
        elif latest.window is not None:
            cockpit.open_window(_window_target(latest), command)
        else:
            cockpit.open_session(latest.session.target, command)

    return commit


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    reader = runtime.WorkspaceReader()
    try:
        curses.wrapper(run, reader.current, read_workspace=reader.read,
                       prepare_open=lambda row: _prepare_destination(row, lambda: reader.current))
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
    return 0
