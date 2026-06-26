# config.py — GPIO pins, I2C constants, settings

# I2C / Display
I2C_ADDRESS = 0x3C
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 64

# GPIO pin assignments (BCM numbering)
PIN_UP     = 17
PIN_DOWN   = 22
PIN_LEFT   = 27
PIN_RIGHT  = 23
PIN_SELECT = 4
PIN_A      = 5   # Back / Cancel
PIN_B      = 6   # Context action

# Button event constants
BTN_UP     = "UP"
BTN_DOWN   = "DOWN"
BTN_LEFT   = "LEFT"
BTN_RIGHT  = "RIGHT"
BTN_SELECT = "SELECT"
BTN_A      = "A"
BTN_B      = "B"
BTN_A_LONG = "A_LONG"  # Button A held for HOLD_TIME seconds

# Debounce / hold
DEBOUNCE_MS = 0.05  # seconds (used as bounce_time in gpiozero)
A_HOLD_TIME = 5.0   # seconds to trigger long-press return-to-main


class ReturnToMainMenu(Exception):
    """Raised anywhere in a flow to jump back to the main menu.
    silent=True suppresses the 'Cancelled' message (used after normal completion)."""
    def __init__(self, silent: bool = False):
        self.silent = silent
        super().__init__()

# Filesystem / mount
MOUNT_BASE = "/mnt/usb_imager"
EXCLUDED_PREFIXES = ("mmcblk", "zram")  # protect Pi SD card and zram swap devices

# UI
MENU_VISIBLE_ROWS = 4   # how many menu items fit on screen
ROW_HEIGHT = 12         # pixels per row
TITLE_HEIGHT = 14       # pixels reserved for title bar
PROGRESS_BAR_Y = 40     # y-position of progress bar
FONT_SIZE_SMALL = 8
FONT_SIZE_MENU = 10
