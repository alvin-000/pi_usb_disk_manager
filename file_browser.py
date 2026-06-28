# file_browser.py — Scrollable file browser

import os
import config
import disk_ops


class FileBrowser:
    """
    Interactive file browser that navigates a mounted filesystem.
    Integrates with Display and MenuSystem.
    """

    def __init__(self, display, root_path: str, dev: str = None,
                 pick_exts: tuple | None = None):
        """
        display   : Display instance
        root_path : absolute path to the mount point root
        dev       : block device path (e.g. '/dev/sda1') — used for unmount label
        pick_exts : optional tuple of lowercase extensions (e.g. ('.dd',)).
                    When set, selecting a file with a matching extension stores
                    its absolute path in self.picked_path and signals the event
                    loop to exit. Files without a matching extension can't be
                    picked. When None, file selection does nothing (browse-only).
        """
        self.display = display
        self.root = root_path.rstrip("/")
        self.dev = dev
        self.pick_exts = tuple(e.lower() for e in pick_exts) if pick_exts else None
        self.picked_path = None  # set to the chosen file's absolute path
        self._path_stack = [self.root]  # stack of directory paths
        self.selected_idx = 0
        self.scroll_offset = 0
        self._entries: list[tuple[str, bool]] = []  # (name, is_dir)
        self._refresh()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _refresh(self):
        """Reload directory listing for current path."""
        path = self._current_path
        try:
            raw = os.listdir(path)
        except PermissionError:
            raw = []

        dirs = sorted(
            [n for n in raw if os.path.isdir(os.path.join(path, n))],
            key=str.lower
        )
        files = sorted(
            [n for n in raw if os.path.isfile(os.path.join(path, n))],
            key=str.lower
        )
        self._entries = [(n, True) for n in dirs] + [(n, False) for n in files]
        self.selected_idx = 0
        self.scroll_offset = 0

    @property
    def _current_path(self) -> str:
        return self._path_stack[-1]

    def _visible_rows(self) -> int:
        font_h = 8 + 2  # approximate line height for small font
        return (config.DISPLAY_HEIGHT - config.TITLE_HEIGHT) // font_h

    def _clamp_scroll(self):
        visible = self._visible_rows()
        if self.selected_idx >= self.scroll_offset + visible:
            self.scroll_offset = self.selected_idx - visible + 1
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def move_up(self):
        if self._entries:
            self.selected_idx = (self.selected_idx - 1) % len(self._entries)
            self._clamp_scroll()

    def move_down(self):
        if self._entries:
            self.selected_idx = (self.selected_idx + 1) % len(self._entries)
            self._clamp_scroll()

    def enter(self) -> bool:
        """
        Enter selected directory. Returns True if entered, False if it's a file.
        For a file matching pick_exts, records the selection in self.picked_path.
        """
        if not self._entries:
            return False
        name, is_dir = self._entries[self.selected_idx]
        if is_dir:
            new_path = os.path.join(self._current_path, name)
            self._path_stack.append(new_path)
            self._refresh()
            return True
        # It's a file — record it if it matches the pick filter
        if self.pick_exts and name.lower().endswith(self.pick_exts):
            self.picked_path = os.path.join(self._current_path, name)
        return False

    def go_up(self) -> bool:
        """
        Go up one directory. Returns False if already at root (triggers unmount).
        """
        if len(self._path_stack) > 1:
            self._path_stack.pop()
            self._refresh()
            return True
        return False  # at root — caller should unmount

    def unmount(self):
        """Unmount the device and clean up."""
        disk_ops.unmount(self.root)

    # ------------------------------------------------------------------ #
    # Render
    # ------------------------------------------------------------------ #
    def render(self):
        self.display.draw_file_list(
            self._entries,
            self.selected_idx,
            self._current_path,
            self.scroll_offset,
        )

    # ------------------------------------------------------------------ #
    # Event loop integration — returns False when browser should exit
    # ------------------------------------------------------------------ #
    def handle_event(self, event: str) -> bool:
        """
        Handle a button event.
        Returns True to continue browsing, False to exit (and unmount).
        """
        if event == config.BTN_UP:
            self.move_up()
        elif event == config.BTN_DOWN:
            self.move_down()
        elif event in (config.BTN_SELECT, config.BTN_RIGHT, config.BTN_B):
            self.enter()
            # In pick mode, a successful file selection exits the browser
            if self.picked_path is not None:
                return False
        elif event in (config.BTN_A, config.BTN_LEFT):
            still_inside = self.go_up()
            if not still_inside:
                return False  # exit browser
        return True
