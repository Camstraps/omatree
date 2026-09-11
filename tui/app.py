"""Curses application for the OmaTree terminal frontend."""

from __future__ import annotations

import argparse
import curses
import queue
import sys
import threading
from typing import Any

from helper.omatree_core import protocol
from tui.controller import (
    BrowserController,
    ScanController,
    ScanOutcome,
    TargetError,
    make_scan_plan,
)


def format_bytes(value: int | float) -> str:
    size = float(value or 0)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    index = 0
    while abs(size) >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    digits = 0 if index == 0 or size >= 100 else 1
    return f"{size:.{digits}f} {units[index]}"


def clipped(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[:width - 1] + "…"


class CursesApp:
    def __init__(self, screen: Any, scan_controller: ScanController) -> None:
        self.screen = screen
        self.scan_controller = scan_controller
        self.browser = BrowserController(scan_controller.active_snapshot)
        self.messages: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self.worker: threading.Thread | None = None
        self.scanning = False
        self.quit_after_cancel = False
        self.progress_entries = 0
        self.progress_bytes = 0
        self.warning_count = 0
        self.status = ""

    def start_scan(self) -> bool:
        if self.scanning:
            return False
        self.scanning = True
        self.progress_entries = 0
        self.progress_bytes = 0
        self.warning_count = 0
        self.status = "Scanning…"

        def emit(event: dict[str, Any]) -> None:
            self.messages.put(("event", event))

        def work() -> None:
            self.messages.put(("outcome", self.scan_controller.scan(emit)))

        self.worker = threading.Thread(target=work, name="omatree-scan", daemon=True)
        self.worker.start()
        return True

    def process_messages(self) -> None:
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                return
            if kind == "event":
                event = value
                if event.get("type") == protocol.PROGRESS:
                    self.progress_entries = int(event.get("entries") or 0)
                    self.progress_bytes = int(event.get("bytes") or 0)
                elif event.get("type") == protocol.WARNING:
                    self.warning_count += 1
            else:
                self.scanning = False
                outcome: ScanOutcome = value
                if outcome.snapshot is not None:
                    self.browser.set_snapshot(outcome.snapshot)
                    self.status = "Scan complete"
                elif outcome.cancelled:
                    self.status = "Scan cancelled"
                else:
                    self.status = "Scan failed: " + (outcome.error or "unknown error")

    def add(self, row: int, column: int, text: str, attributes: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or column >= width:
            return
        try:
            self.screen.addstr(row, column, clipped(text, width - column), attributes)
        except curses.error:
            pass

    def render(self) -> int:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        snapshot = self.browser.snapshot
        self.add(0, 0, "OmaTree", curses.A_BOLD)
        self.add(1, 0, "Path: " + self.scan_controller.plan.path)
        if snapshot is not None:
            summary = (
                f"Size: {format_bytes(snapshot.root.bytes)}  "
                f"Directories: {len(snapshot.directories)}"
            )
        else:
            summary = "No completed snapshot"
        self.add(2, 0, summary)
        if self.scanning:
            progress = (
                f"Scanning: {self.progress_entries:,} entries, "
                f"{format_bytes(self.progress_bytes)} processed"
            )
            if self.warning_count:
                progress += f", {self.warning_count} warnings"
            self.add(3, 0, progress)
        else:
            self.add(3, 0, self.status)

        header_row = 5
        footer_row = max(header_row + 1, height - 2)
        body_rows = max(0, footer_row - header_row - 1)
        size_width = 11
        percent_width = 9
        name_width = max(8, width - size_width - percent_width - 2)
        self.add(
            header_row,
            0,
            f"{'Name':<{name_width}} {'Size':>{size_width}} {'% Parent':>{percent_width}}",
            curses.A_BOLD,
        )

        if snapshot is not None and body_rows:
            self.browser.keep_cursor_visible(body_rows)
            visible = self.browser.visible_paths
            for screen_index, path in enumerate(
                visible[self.browser.scroll:self.browser.scroll + body_rows]
            ):
                visible_index = self.browser.scroll + screen_index
                node = snapshot.directories[path]
                marker = "▾" if path in self.browser.expanded and node.children else (
                    "▸" if node.children else " "
                )
                indent = "  " * node.depth
                name = clipped(indent + marker + " " + node.name, name_width)
                percentage = snapshot.percentage_of_parent(path)
                line = (
                    f"{name:<{name_width}} {format_bytes(node.bytes):>{size_width}} "
                    f"{percentage:>{percent_width - 1}.1f}%"
                )
                attributes = curses.A_REVERSE if visible_index == self.browser.cursor else 0
                self.add(header_row + 1 + screen_index, 0, line, attributes)

        self.add(
            footer_row,
            0,
            "↑/k ↓/j move  Enter/→/l expand  ←/h collapse  Backspace parent",
        )
        self.add(footer_row + 1, 0, "Home/End  PgUp/PgDn  r rescan  q quit")
        self.screen.refresh()
        return body_rows

    def run(self) -> None:
        self.screen.keypad(True)
        self.screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.start_scan()
        while True:
            self.process_messages()
            if self.quit_after_cancel and not self.scanning:
                return
            page_size = self.render()
            try:
                key = self.screen.getch()
            except KeyboardInterrupt:
                if self.scanning:
                    self.scan_controller.cancel()
                    self.status = "Cancelling…"
                    continue
                return
            if key == -1 or key == curses.KEY_RESIZE:
                continue
            if key in (ord("q"), ord("Q")):
                if self.scanning:
                    self.quit_after_cancel = True
                    self.status = "Cancelling…"
                    self.scan_controller.cancel()
                else:
                    return
            elif self.scanning:
                continue
            elif key in (curses.KEY_UP, ord("k")):
                self.browser.move(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.browser.move(1)
            elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
                self.browser.expand_selected()
            elif key in (curses.KEY_LEFT, ord("h")):
                self.browser.collapse_or_parent()
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                self.browser.select_parent()
            elif key == curses.KEY_HOME:
                self.browser.first()
            elif key == curses.KEY_END:
                self.browser.last()
            elif key == curses.KEY_PPAGE:
                self.browser.page(-1, page_size)
            elif key == curses.KEY_NPAGE:
                self.browser.page(1, page_size)
            elif key in (ord("r"), ord("R")):
                self.start_scan()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omatree", description="Browse directory disk usage in a terminal"
    )
    parser.add_argument("path", nargs="?", help="directory to scan (default: HOME)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = make_scan_plan(args.path)
    except (OSError, TargetError) as error:
        print(f"omatree: {error}", file=sys.stderr)
        return 2
    try:
        curses.wrapper(lambda screen: CursesApp(screen, ScanController(plan)).run())
    except KeyboardInterrupt:
        return 130
    except curses.error as error:
        print(f"omatree: terminal error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
