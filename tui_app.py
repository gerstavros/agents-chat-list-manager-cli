from __future__ import annotations

import curses
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from core.export import export_conversation
from core.models import ConversationMeta, Message
from core.service import ConversationService

HEADER_ROWS = 2  # title bar + column header
FOOTER_ROWS = 1


def run_tui(stdscr, service: ConversationService) -> None:
    try:
        curses.set_escdelay(25)  # snappier Escape-to-cancel in text prompts
    except AttributeError:
        pass  # older Python without set_escdelay support
    curses.curs_set(0)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selected row
        curses.init_pair(2, curses.COLOR_WHITE, -1)                  # column header
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)  # status/footer
        curses.init_pair(4, curses.COLOR_GREEN, -1)                  # role: user
        curses.init_pair(5, curses.COLOR_YELLOW, -1)                 # role: assistant
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)                # role: system

    app = TuiApp(stdscr, service)
    app.reload()
    app.loop()


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    text = text[: max(0, max_x - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


class TuiApp:
    def __init__(self, stdscr, service: ConversationService):
        self.stdscr = stdscr
        self.service = service
        self.all_convs: list[ConversationMeta] = []
        self.filtered: list[ConversationMeta] = []
        self.selected = 0
        self.scroll_top = 0
        self.search_query = ""
        self.tool_ids = [a.tool_id for a in service.adapters()]
        self.tool_filter: str | None = None
        self.status = "Ready."
        self.should_quit = False

    def reload(self) -> None:
        self.set_status("Scanning...")
        self.draw()
        self.all_convs = self.service.list_all()
        self._apply_filter()
        self.set_status(f"{len(self.filtered)} conversation(s)")

    def _apply_filter(self) -> None:
        convs = self.all_convs
        if self.tool_filter:
            convs = [c for c in convs if c.tool_id == self.tool_filter]
        if self.search_query:
            q = self.search_query.lower()
            convs = [c for c in convs if q in c.title.lower() or q in c.project_path.lower()]
        min_dt = datetime.min.replace(tzinfo=timezone.utc)
        convs = sorted(convs, key=lambda c: c.updated_at or min_dt, reverse=True)
        self.filtered = convs
        self.selected = min(self.selected, max(0, len(convs) - 1))
        self.scroll_top = 0

    def set_status(self, msg: str) -> None:
        self.status = msg

    def loop(self) -> None:
        while not self.should_quit:
            self.draw()
            key = self.stdscr.getch()
            self.handle_key(key)

    # ---- drawing ----

    def draw(self) -> None:
        self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()
        if max_y < 6 or max_x < 40:
            _safe_addstr(self.stdscr, 0, 0, "Terminal too small.")
            self.stdscr.refresh()
            return

        title = " chatlistctl — j/k or arrows: move  Enter: view  d: delete  /: search  t: tool  e: export  r: refresh  q: quit "
        _safe_addstr(self.stdscr, 0, 0, title.ljust(max_x), curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD)

        header = f"{'TOOL':<13} {'UPDATED':<17} {'MSGS':>5}  {'TITLE':<30} PROJECT"
        _safe_addstr(self.stdscr, 1, 0, header.ljust(max_x), curses.color_pair(2) if curses.has_colors() else curses.A_UNDERLINE)

        list_height = max_y - HEADER_ROWS - FOOTER_ROWS
        if self.selected < self.scroll_top:
            self.scroll_top = self.selected
        elif self.selected >= self.scroll_top + list_height:
            self.scroll_top = self.selected - list_height + 1

        for row in range(list_height):
            idx = self.scroll_top + row
            y = HEADER_ROWS + row
            if idx >= len(self.filtered):
                continue
            conv = self.filtered[idx]
            updated = conv.updated_at.strftime("%Y-%m-%d %H:%M") if conv.updated_at else "-"
            title_txt = conv.title.replace("\n", " ")[:30]
            line = f"{conv.tool_id:<13} {updated:<17} {conv.message_count:>5}  {title_txt:<30} {conv.project_path}"
            attr = curses.color_pair(1) if (idx == self.selected and curses.has_colors()) else (curses.A_REVERSE if idx == self.selected else 0)
            _safe_addstr(self.stdscr, y, 0, line.ljust(max_x), attr)

        filt_bits = []
        if self.tool_filter:
            filt_bits.append(f"tool={self.tool_filter}")
        if self.search_query:
            filt_bits.append(f"search='{self.search_query}'")
        filt_txt = ("  [" + ", ".join(filt_bits) + "]") if filt_bits else ""
        footer = f" {self.status}{filt_txt} "
        _safe_addstr(self.stdscr, max_y - 1, 0, footer.ljust(max_x), curses.color_pair(3) if curses.has_colors() else curses.A_REVERSE)

        self.stdscr.refresh()

    # ---- input ----

    def handle_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(0, len(self.filtered) - 1), self.selected + 1)
        elif key == curses.KEY_NPAGE:
            self.selected = min(max(0, len(self.filtered) - 1), self.selected + 10)
        elif key == curses.KEY_PPAGE:
            self.selected = max(0, self.selected - 10)
        elif key == curses.KEY_HOME:
            self.selected = 0
        elif key == curses.KEY_END:
            self.selected = max(0, len(self.filtered) - 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            self._view_selected()
        elif key in (ord("d"), curses.KEY_DC):
            self._delete_selected()
        elif key == ord("/"):
            self._search_prompt()
        elif key == ord("t"):
            self._cycle_tool_filter()
        elif key == ord("e"):
            self._export_selected()
        elif key == ord("r"):
            self.reload()
        elif key == ord("q"):
            self.should_quit = True

    def _current(self) -> ConversationMeta | None:
        if 0 <= self.selected < len(self.filtered):
            return self.filtered[self.selected]
        return None

    def _cycle_tool_filter(self) -> None:
        options = [None] + self.tool_ids
        current_idx = options.index(self.tool_filter) if self.tool_filter in options else 0
        self.tool_filter = options[(current_idx + 1) % len(options)]
        self._apply_filter()
        self.set_status(f"{len(self.filtered)} conversation(s)")

    def _search_prompt(self) -> None:
        query = self._read_line(prompt="/", initial=self.search_query)
        if query is not None:
            self.search_query = query
            self._apply_filter()
            self.set_status(f"{len(self.filtered)} conversation(s)")

    def _read_line(self, prompt: str, initial: str = "") -> str | None:
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                max_y, max_x = self.stdscr.getmaxyx()
                text = prompt + "".join(buf)
                _safe_addstr(self.stdscr, max_y - 1, 0, text.ljust(max_x), curses.color_pair(3) if curses.has_colors() else curses.A_REVERSE)
                self.stdscr.move(max_y - 1, min(len(text), max_x - 1))
                self.stdscr.refresh()
                ch = self.stdscr.getch()
                if ch in (10, 13):
                    return "".join(buf)
                if ch == 27:
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    buf.append(chr(ch))
        finally:
            curses.curs_set(0)

    def _delete_selected(self) -> None:
        conv = self._current()
        if conv is None:
            return
        answer = self._read_line(prompt=f"Delete '{conv.title[:40]}'? [y/N] ")
        if answer is None or answer.strip().lower() != "y":
            self.set_status("Delete cancelled.")
            return
        try:
            self.service.delete(conv)
        except OSError as exc:
            self.set_status(f"Delete failed: {exc}")
            return
        self.set_status(f"Deleted: {conv.title[:40]}")
        self.reload()

    def _export_selected(self) -> None:
        conv = self._current()
        if conv is None:
            return
        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in conv.title)[:40].strip() or "conversation"
        default_path = f"./{safe_title}.md"
        path_str = self._read_line(prompt="Export to: ", initial=default_path)
        if not path_str:
            self.set_status("Export cancelled.")
            return
        output = Path(path_str)
        fmt = "markdown" if output.suffix.lower() == ".md" else "text"
        messages = self.service.load(conv)
        try:
            export_conversation(conv, messages, output, fmt)
        except OSError as exc:
            self.set_status(f"Export failed: {exc}")
            return
        self.set_status(f"Exported to {output}")

    def _view_selected(self) -> None:
        conv = self._current()
        if conv is None:
            return
        self.set_status("Loading...")
        self.draw()
        messages = self.service.load(conv)
        viewer = TranscriptViewer(self.stdscr, conv, messages)
        viewer.loop()


class TranscriptViewer:
    _ROLE_PAIR = {"user": 4, "assistant": 5, "system": 6}

    def __init__(self, stdscr, meta: ConversationMeta, messages: list[Message]):
        self.stdscr = stdscr
        self.meta = meta
        self.messages = messages
        self.top = 0
        self.raw_mode = False
        self._lines: list[tuple[str, int]] = []  # (text, color_pair or 0)
        self._build_lines()

    def _build_lines(self) -> None:
        _, max_x = self.stdscr.getmaxyx()
        width = max(20, max_x - 1)
        lines: list[tuple[str, int]] = []
        for msg in self.messages:
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S") if msg.timestamp else "--"
            marker = " [tool call]" if msg.has_tool_call else ""
            header = f"[{ts}] {msg.role}{marker}"
            pair = self._ROLE_PAIR.get(msg.role, 0)
            lines.append((header, pair | curses.A_BOLD if pair else curses.A_BOLD))
            for para in (msg.text or "").split("\n"):
                wrapped = textwrap.wrap(para, width) or [""]
                for w in wrapped:
                    lines.append((w, 0))
            lines.append(("", 0))
        self._lines = lines

    def loop(self) -> None:
        while True:
            self.draw()
            key = self.stdscr.getch()
            if key in (ord("q"), curses.KEY_LEFT):
                return
            if key == curses.KEY_RESIZE:
                self._build_lines()
            elif key in (curses.KEY_UP, ord("k")):
                self.top = max(0, self.top - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.top += 1
            elif key == curses.KEY_NPAGE:
                self.top += 15
            elif key == curses.KEY_PPAGE:
                self.top = max(0, self.top - 15)
            elif key == curses.KEY_HOME:
                self.top = 0
            elif key == curses.KEY_END:
                self.top = max(0, len(self._lines) - 1)

    def draw(self) -> None:
        self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()
        if max_y < 4:
            self.stdscr.refresh()
            return

        title = f" {self.meta.title[: max_x - 2]} "
        _safe_addstr(self.stdscr, 0, 0, title.ljust(max_x), curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD)
        info = f"tool={self.meta.tool_id}  project={self.meta.project_path}  messages={self.meta.message_count}"
        _safe_addstr(self.stdscr, 1, 0, info[: max_x - 1])

        body_height = max_y - 3
        self.top = max(0, min(self.top, max(0, len(self._lines) - 1)))
        for row in range(body_height):
            idx = self.top + row
            if idx >= len(self._lines):
                continue
            text, attr = self._lines[idx]
            color = curses.color_pair(attr & 0xFF) if curses.has_colors() and (attr & 0xFF) else 0
            bold = attr & curses.A_BOLD
            _safe_addstr(self.stdscr, 2 + row, 0, text, color | bold)

        footer = " q/Esc: back   j/k, PgUp/PgDn, Home/End: scroll "
        _safe_addstr(self.stdscr, max_y - 1, 0, footer.ljust(max_x), curses.color_pair(3) if curses.has_colors() else curses.A_REVERSE)
        self.stdscr.refresh()
