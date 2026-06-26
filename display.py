# display.py — OLED wrapper (SSD1306 + Pillow)

import board
import adafruit_ssd1306
from PIL import Image, ImageDraw, ImageFont
import config


class Display:
    def __init__(self):
        i2c = board.I2C()
        self.oled = adafruit_ssd1306.SSD1306_I2C(
            config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT, i2c,
            addr=config.I2C_ADDRESS
        )
        self.width = config.DISPLAY_WIDTH
        self.height = config.DISPLAY_HEIGHT
        self._image = Image.new("1", (self.width, self.height))
        self._draw = ImageDraw.Draw(self._image)

        # Fonts — load_default() is always available (8px bitmap)
        self.font_small = ImageFont.load_default()
        self.font_menu = ImageFont.load_default()

    # ------------------------------------------------------------------ #
    # Primitives
    # ------------------------------------------------------------------ #
    def clear(self):
        self._draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)

    def show(self):
        self.oled.image(self._image)
        self.oled.show()

    def _text_h(self, font):
        """Return pixel height of a line of text."""
        bbox = font.getbbox("A")
        return bbox[3] - bbox[1]

    # ------------------------------------------------------------------ #
    # Menu renderer — scrolling list with title bar
    # ------------------------------------------------------------------ #
    def draw_menu(self, items, selected_idx, title, scroll_offset=0):
        """
        Render a scrolling menu.

        items        : list of strings
        selected_idx : absolute index of the currently highlighted item
        title        : string shown in title bar
        scroll_offset: first visible item index
        """
        self.clear()
        d = self._draw
        font = self.font_menu

        # Title bar — omitted when title is empty, freeing the full height for items
        if title:
            d.rectangle((0, 0, self.width, config.TITLE_HEIGHT - 1), fill=1)
            d.text((2, 1), title[:20], font=font, fill=0)
            content_y = config.TITLE_HEIGHT
        else:
            content_y = 0

        visible = (self.height - content_y) // config.ROW_HEIGHT

        # Item rows
        for row in range(visible):
            idx = scroll_offset + row
            if idx >= len(items):
                break
            y = content_y + row * config.ROW_HEIGHT
            highlighted = (idx == selected_idx)
            if highlighted:
                d.rectangle((0, y, self.width, y + config.ROW_HEIGHT - 1), fill=1)
            label = str(items[idx])
            # Truncate to fit width
            while font.getlength(label) > self.width - 4 and len(label) > 1:
                label = label[:-1]
            d.text((2, y + 1), label, font=font, fill=0 if highlighted else 1)

        # Scroll indicators
        if scroll_offset > 0:
            d.text((self.width - 8, content_y), "^", font=font, fill=1)
        if scroll_offset + visible < len(items):
            d.text((self.width - 8, self.height - 10), "v", font=font, fill=1)

        self.show()

    # ------------------------------------------------------------------ #
    # Progress bar
    # ------------------------------------------------------------------ #
    def draw_progress(self, label, percent, speed=""):
        """
        Render a label + horizontal progress bar + percentage + optional speed.

        label   : string description of the operation
        percent : float 0.0 – 100.0
        speed   : optional speed string, e.g. '12.3 MB/s'
        """
        self.clear()
        d = self._draw
        font = self.font_menu

        # Label — top of screen
        d.text((2, 2), label[:22], font=font, fill=1)

        # Bar — middle
        bar_x, bar_y = 2, 22
        bar_w = self.width - 4
        bar_h = 14
        d.rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), outline=1, fill=0)

        fill_w = int(bar_w * max(0.0, min(1.0, percent / 100.0)))
        if fill_w > 0:
            d.rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), fill=1)

        # Percentage centred inside bar
        pct_str = f"{percent:.0f}%"
        txt_w = int(font.getlength(pct_str))
        txt_x = bar_x + (bar_w - txt_w) // 2
        d.text((txt_x, bar_y + 2), pct_str, font=font, fill=0 if fill_w > txt_x else 1)

        # Speed — below bar, right-aligned
        if speed:
            spd_w = int(font.getlength(speed))
            d.text((self.width - spd_w - 2, 42), speed, font=font, fill=1)

        self.show()

    # ------------------------------------------------------------------ #
    # Message / confirm screen
    # ------------------------------------------------------------------ #
    def draw_message(self, lines, highlight_last=False):
        """
        Display up to ~5 centred text lines.

        lines          : list of strings
        highlight_last : draw a solid rectangle behind the last line (as a button cue)
        """
        self.clear()
        d = self._draw
        font = self.font_small

        line_h = self._text_h(font) + 2
        total_h = len(lines) * line_h
        start_y = max(0, (self.height - total_h) // 2)

        for i, line in enumerate(lines):
            y = start_y + i * line_h
            txt_w = int(font.getlength(str(line)))
            x = (self.width - txt_w) // 2
            if highlight_last and i == len(lines) - 1:
                d.rectangle((0, y - 1, self.width, y + line_h), fill=1)
                d.text((x, y), str(line), font=font, fill=0)
            else:
                d.text((x, y), str(line), font=font, fill=1)

        self.show()

    # ------------------------------------------------------------------ #
    # File list browser
    # ------------------------------------------------------------------ #
    def draw_file_list(self, files, selected_idx, path, scroll_offset=0):
        """
        Render a scrollable file/directory listing.

        files        : list of (name, is_dir) tuples
        selected_idx : absolute highlighted index
        path         : current path string (shown as truncated title)
        scroll_offset: first visible item
        """
        self.clear()
        d = self._draw
        font = self.font_small

        # Path in title bar
        d.rectangle((0, 0, self.width, config.TITLE_HEIGHT - 1), fill=1)
        # Show last two path components
        short_path = "/".join(path.rstrip("/").split("/")[-2:]) or "/"
        d.text((1, 1), short_path[:24], font=font, fill=0)

        line_h = self._text_h(font) + 2
        visible = (self.height - config.TITLE_HEIGHT) // line_h

        for row in range(visible):
            idx = scroll_offset + row
            if idx >= len(files):
                break
            name, is_dir = files[idx]
            y = config.TITLE_HEIGHT + row * line_h
            highlighted = (idx == selected_idx)
            prefix = "[D] " if is_dir else "[F] "
            label = prefix + name
            # Truncate
            max_w = self.width - 4
            while font.getlength(label) > max_w and len(label) > 1:
                label = label[:-1]
            if highlighted:
                d.rectangle((0, y, self.width, y + line_h - 1), fill=1)
            d.text((2, y), label, font=font, fill=0 if highlighted else 1)

        # Scroll indicators
        if scroll_offset > 0:
            d.text((self.width - 8, config.TITLE_HEIGHT), "^", font=font, fill=1)
        if scroll_offset + visible < len(files):
            d.text((self.width - 8, self.height - 10), "v", font=font, fill=1)

        self.show()
