# menu.py — Hierarchical menu engine

import config


class ActionItem:
    """A leaf menu item that runs a callable when selected."""
    def __init__(self, label: str, action):
        self.label = label
        self.action = action  # callable()

    def __str__(self):
        return self.label


class SubMenuItem:
    """A menu item that pushes a child MenuState onto the navigation stack."""
    def __init__(self, label: str, build_fn):
        self.label = label
        self.build_fn = build_fn  # callable() -> MenuState

    def __str__(self):
        return self.label


class MenuState:
    """Represents one level of the navigation stack."""
    def __init__(self, title: str, items: list, visible_rows: int = config.MENU_VISIBLE_ROWS):
        self.title = title
        self.items = items
        self.visible_rows = visible_rows
        self.selected_idx = 0
        self.scroll_offset = 0

    def _clamp_scroll(self):
        visible = self.visible_rows
        # Scroll down
        if self.selected_idx >= self.scroll_offset + visible:
            self.scroll_offset = self.selected_idx - visible + 1
        # Scroll up
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx

    def move_up(self):
        if self.items:
            self.selected_idx = (self.selected_idx - 1) % len(self.items)
            self._clamp_scroll()

    def move_down(self):
        if self.items:
            self.selected_idx = (self.selected_idx + 1) % len(self.items)
            self._clamp_scroll()

    @property
    def current_item(self):
        if not self.items:
            return None
        return self.items[self.selected_idx]


class MenuSystem:
    """
    Navigation stack of MenuState objects.
    The top of the stack is the currently displayed menu.
    """

    def __init__(self, display):
        self.display = display
        self._stack: list[MenuState] = []
        self._dirty = True  # redraw needed

    # ------------------------------------------------------------------ #
    # Stack management
    # ------------------------------------------------------------------ #
    def push(self, state: MenuState):
        self._stack.append(state)
        self._dirty = True

    def pop(self):
        if len(self._stack) > 1:
            self._stack.pop()
            self._dirty = True

    def pop_to_root(self):
        """Discard every menu except the root (main menu)."""
        while len(self._stack) > 1:
            self._stack.pop()
        self._dirty = True

    @property
    def current(self) -> MenuState | None:
        return self._stack[-1] if self._stack else None

    # ------------------------------------------------------------------ #
    # Event dispatch
    # ------------------------------------------------------------------ #
    def handle_event(self, event: str) -> bool:
        """
        Process one button event.
        Returns True if the display should be refreshed.
        """
        state = self.current
        if state is None:
            return False

        if event == config.BTN_UP:
            state.move_up()
            self._dirty = True

        elif event == config.BTN_DOWN:
            state.move_down()
            self._dirty = True

        elif event in (config.BTN_SELECT, config.BTN_RIGHT, config.BTN_B):
            self._activate(state)

        elif event in (config.BTN_A, config.BTN_LEFT):
            self.pop()

        elif event == config.BTN_A_LONG:
            self.pop_to_root()

        dirty = self._dirty
        self._dirty = False
        return dirty

    def _activate(self, state: MenuState):
        item = state.current_item
        if item is None:
            return
        if isinstance(item, SubMenuItem):
            child = item.build_fn()
            if child is not None:
                self.push(child)
                self._dirty = True
        elif isinstance(item, ActionItem):
            item.action()
            self._dirty = True

    # ------------------------------------------------------------------ #
    # Render
    # ------------------------------------------------------------------ #
    def render(self):
        state = self.current
        if state is None:
            self.display.draw_message(["No menu"])
            return
        self.display.draw_menu(
            state.items,
            state.selected_idx,
            state.title,
            state.scroll_offset,
        )
