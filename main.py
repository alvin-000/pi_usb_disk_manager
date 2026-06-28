# main.py — Entry point, event loop, init

import logging
import os
import signal
import time

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")
import disk_ops
from display import Display
from buttons import ButtonHandler
from menu import MenuSystem, MenuState, ActionItem, SubMenuItem
from file_browser import FileBrowser
from progress import ProgressScreen


# ------------------------------------------------------------------ #
# Active mounts tracker (for cleanup on exit)
# ------------------------------------------------------------------ #
_active_mounts: list[str] = []


def _cleanup(display: Display, buttons: ButtonHandler):
    """Unmount all active mounts and clear the display."""
    for mp in list(_active_mounts):
        try:
            disk_ops.unmount(mp)
        except Exception:
            pass
    _active_mounts.clear()
    try:
        display.clear()
        display.show()
    except Exception:
        pass
    buttons.close()


# ------------------------------------------------------------------ #
# Disk selection helper — returns a MenuState listing disks
# ------------------------------------------------------------------ #
def _build_disk_menu(title: str, on_select, include_partitions=False):
    """
    Build a dynamic MenuState that lists available disks (or partitions).
    on_select(dev_path) is called when the user confirms.
    """
    try:
        disks = disk_ops.list_disks()
    except Exception as e:
        return MenuState(title, [ActionItem(f"Error: {e}", lambda: None)])

    if not disks:
        return MenuState(title, [ActionItem("No disks found", lambda: None)])

    items = []
    for disk in disks:
        name = disk.get("name", "?")
        size = disk.get("size", "?")
        label = f"/dev/{name} ({size})"
        dev_path = f"/dev/{name}"
        items.append(ActionItem(label, lambda d=dev_path: on_select(d)))

    return MenuState(title, items)


def _build_partition_menu(title: str, disk_name: str, on_select):
    try:
        parts = disk_ops.list_partitions(disk_name)
    except Exception as e:
        return MenuState(title, [ActionItem(f"Error: {e}", lambda: None)])

    if not parts:
        return MenuState(title, [ActionItem("No partitions", lambda: None)])

    items = []
    for p in parts:
        name = p.get("name", "?")
        size = p.get("size", "?")
        fstype = p.get("fstype") or "?"
        label = f"/dev/{name} {fstype} ({size})"
        dev_path = f"/dev/{name}"
        items.append(ActionItem(label, lambda d=dev_path: on_select(d)))

    return MenuState(title, items)


# ------------------------------------------------------------------ #
# Flow helpers
# ------------------------------------------------------------------ #
def _confirm_screen(display, button_queue, lines: list[str]) -> bool:
    """
    Show a confirm/cancel prompt. Returns True if user pressed SELECT/Right/B.
    Raises ReturnToMainMenu if A is held for 5 s.
    """
    display.draw_message(
        lines + ["[Sel]=Yes  [A]=No"],
        highlight_last=True
    )
    while True:
        event = button_queue.get(timeout=30)
        if event == config.BTN_A_LONG:
            raise config.ReturnToMainMenu()
        if event in (config.BTN_SELECT, config.BTN_RIGHT, config.BTN_B):
            return True
        if event in (config.BTN_A, config.BTN_LEFT):
            return False


# ------------------------------------------------------------------ #
# Menu action builders
# ------------------------------------------------------------------ #

def build_browse_disk_flow(display, menu_system, button_queue):
    """Returns a MenuState for disk selection → partition → browse."""

    # Filesystems that can be raw-mounted (no partition table needed)
    _RAW_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs"}

    def on_disk_selected(disk_dev):
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            part_menu = _build_partition_menu(
                "Select Partition",
                disk_name,
                lambda dev: on_part_selected(dev)
            )
            menu_system.push(part_menu)
        else:
            # No partition table — check if raw disk has a directly mountable filesystem
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _RAW_MOUNTABLE:
                log.info("No partitions on %s; raw-mounting as %s", disk_dev, fstype)
                on_part_selected(disk_dev)  # mount the raw disk device directly
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Detected: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    def on_part_selected(dev):
        mount_point = os.path.join(config.MOUNT_BASE, dev.replace("/dev/", ""))
        try:
            disk_ops.mount_partition(dev, mount_point)
            _active_mounts.append(mount_point)
        except Exception as e:
            display.draw_message(["Mount failed:", str(e)[:22], "", "Press any button"])
            button_queue.get(timeout=15)
            return

        # Pop into file browser loop
        browser = FileBrowser(display, mount_point, dev)
        menu_system.pop()  # remove partition menu
        menu_system.pop()  # remove disk menu

        try:
            running = True
            while running:
                browser.render()
                event = button_queue.get()
                if event == config.BTN_A_LONG:
                    raise config.ReturnToMainMenu()
                running = browser.handle_event(event)
        finally:
            # Always unmount, even on ReturnToMainMenu
            try:
                disk_ops.unmount(mount_point)
                if mount_point in _active_mounts:
                    _active_mounts.remove(mount_point)
            except Exception:
                pass

    return _build_disk_menu("Browse: Select Disk", on_disk_selected)


def build_copy_files_flow(display, menu_system, button_queue):
    """
    Copy Files flow:
      1. Select source disk → auto-detect partition or raw-mount
      2. Select dest disk   → auto-detect partition or raw-mount
      3. Pre-check dest has enough free space — error screen if not
      4. Confirm screen showing used / free sizes
      5. rsync with live progress bar + MB/s speed
      6. Unmount both disks on exit (success, cancel, or error)
    """

    _RAW_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs"}

    state = {}  # src_mount, dst_mount tracked here for cleanup

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _cleanup_mounts():
        """Unmount src and dst — safe to call multiple times."""
        for key in ("src_mount", "dst_mount"):
            mp = state.get(key)
            if mp:
                try:
                    disk_ops.unmount(mp)
                    if mp in _active_mounts:
                        _active_mounts.remove(mp)
                except Exception:
                    pass
                state.pop(key, None)

    def _mount_dev(dev: str, role: str) -> str | None:
        """
        Mount dev at MOUNT_BASE/{role}_{dev_name}.
        Returns the mount point on success, None on failure (shows error screen).
        """
        mp = os.path.join(config.MOUNT_BASE, f"{role}_{dev.replace('/dev/', '')}")
        try:
            disk_ops.mount_partition(dev, mp)
            _active_mounts.append(mp)
            return mp
        except Exception as e:
            display.draw_message([
                f"Mount {role} failed:",
                str(e)[:22],
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return None

    def _resolve_dev(disk_dev: str, title: str, on_resolved):
        """
        If disk has partitions → push a partition menu.
        If disk has a raw mountable filesystem → call on_resolved(disk_dev) directly.
        Otherwise show an error.
        """
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            menu_system.push(_build_partition_menu(title, disk_name, on_resolved))
        else:
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _RAW_MOUNTABLE:
                log.info("Copy: no partitions on %s, raw-mounting as %s", disk_dev, fstype)
                on_resolved(disk_dev)
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Found: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    # ------------------------------------------------------------------ #
    # Step 1 — source disk selected
    # ------------------------------------------------------------------ #
    def on_src_disk(disk_dev):
        state["src_disk"] = disk_dev
        _resolve_dev(disk_dev, "Source Partition", on_src_dev)

    def on_src_dev(dev):
        """Mount source, then present destination disk list."""
        mp = _mount_dev(dev, "src")
        if mp is None:
            return  # error already shown; src not mounted, nothing to clean up
        state["src_mount"] = mp
        menu_system.push(_build_disk_menu("Dest Disk", on_dst_disk))

    # ------------------------------------------------------------------ #
    # Step 2 — destination disk selected
    # ------------------------------------------------------------------ #
    def on_dst_disk(disk_dev):
        state["dst_disk"] = disk_dev
        _resolve_dev(disk_dev, "Dest Partition", on_dst_dev)

    def on_dst_dev(dev):
        """Mount destination, validate space, confirm, copy, unmount."""
        mp = _mount_dev(dev, "dst")
        if mp is None:
            # dst mount failed — also clean up src
            _cleanup_mounts()
            return
        state["dst_mount"] = mp

        try:
            _run_copy()
        finally:
            _cleanup_mounts()

    # ------------------------------------------------------------------ #
    # Step 3–5 — space check → confirm → rsync → done
    # ------------------------------------------------------------------ #
    def _run_copy():
        src_used = disk_ops.get_used_space(state["src_mount"])
        dst_free = disk_ops.get_free_space(state["dst_mount"])

        # Pre-check space before asking the user to confirm
        if src_used > dst_free:
            display.draw_message([
                "Not enough space!",
                f"Need:  {disk_ops._fmt_bytes(src_used)}",
                f"Free:  {disk_ops._fmt_bytes(dst_free)}",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "Copy Files?",
            f"Used: {disk_ops._fmt_bytes(src_used)}",
            f"Free: {disk_ops._fmt_bytes(dst_free)}",
        ])
        if not ok:
            return

        progress = {
            "label": "Copying files...",
            "percent": 0.0,
            "done": False,
            "error": "",
            "speed": "",
        }
        disk_ops.copy_files(state["src_mount"], state["dst_mount"], progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()  # raises ReturnToMainMenu on button dismiss — caught above in finally

    return _build_disk_menu("Copy: Source Disk", on_src_disk)


def build_clone_disk_flow(display, menu_system, button_queue):
    """Source disk → dest disk → confirm → dd."""

    state = {}

    def on_src(disk_dev):
        state["src_dev"] = disk_dev
        dst_menu = _build_disk_menu("Clone: Dest Disk", on_dst)
        menu_system.push(dst_menu)

    def on_dst(disk_dev):
        state["dst_dev"] = disk_dev
        try:
            src_size = disk_ops.get_disk_size(state["src_dev"])
            dst_size = disk_ops.get_disk_size(disk_dev)
        except Exception as e:
            display.draw_message(["Size check failed:", str(e)[:22], "", "Press button"])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "Clone Disk?",
            f"Src: {disk_ops._fmt_bytes(src_size)}",
            f"Dst: {disk_ops._fmt_bytes(dst_size)}",
            "DATA WILL BE LOST",
        ])
        if not ok:
            return

        progress = {"label": "Cloning...", "percent": 0.0, "done": False, "error": ""}
        disk_ops.dd_clone(state["src_dev"], disk_dev, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("Clone: Source Disk", on_src)


def build_format_disk_flow(display, menu_system, button_queue):
    """Disk → format type → double-confirm → mkfs on whole disk."""

    def on_disk(disk_dev):
        fmt_menu = MenuState("Select Format", [
            ActionItem("FAT32", lambda: on_fmt(disk_dev, "fat32")),
            ActionItem("exFAT", lambda: on_fmt(disk_dev, "exfat")),
            ActionItem("NTFS",  lambda: on_fmt(disk_dev, "ntfs")),
        ])
        menu_system.push(fmt_menu)

    def on_fmt(dev, fs_type):
        # Double confirm
        ok1 = _confirm_screen(display, button_queue, [
            "Format disk?",
            dev,
            f"as {fs_type.upper()}",
            "ALL DATA LOST!",
        ])
        if not ok1:
            return
        ok2 = _confirm_screen(display, button_queue, [
            "ARE YOU SURE?",
            "This cannot be",
            "undone.",
        ])
        if not ok2:
            return

        progress = {"label": "Formatting...", "percent": 0.0, "done": False, "error": ""}
        disk_ops.format_disk(dev, fs_type, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("Format: Select Disk", on_disk)


def build_wipe_disk_flow(display, menu_system, button_queue):
    """Disk → double-confirm → dd zero."""

    def on_disk(disk_dev):
        try:
            size = disk_ops.get_disk_size(disk_dev)
        except Exception:
            size = 0

        ok1 = _confirm_screen(display, button_queue, [
            "WIPE DISK?",
            disk_dev,
            disk_ops._fmt_bytes(size),
            "ALL DATA LOST!",
        ])
        if not ok1:
            return
        ok2 = _confirm_screen(display, button_queue, [
            "FINAL WARNING!",
            "Write zeros to",
            disk_dev + "?",
        ])
        if not ok2:
            return

        progress = {"label": "Wiping...", "percent": 0.0, "done": False, "error": ""}
        disk_ops.wipe_disk(disk_dev, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("Wipe: Select Disk", on_disk)


# ------------------------------------------------------------------ #
# Forensic Image flow
# ------------------------------------------------------------------ #
def build_forensic_image_flow(display, menu_system, button_queue):
    """
    Forensic Image flow:
      1. Select source disk (whole disk)
      2. Read serial number from source disk hardware
      3. Select destination disk → partition or raw-mount (writable FS)
      4. Space check: dst free >= src disk size
      5. Confirm screen
      6. dc3dd: SERIAL.dd + SERIAL.sha256.log + SERIAL.log
      7. Unmount destination on exit
    """

    # Writable filesystems accepted as imaging destinations (includes ext family)
    _DST_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs",
                      "ext4", "ext3", "ext2"}

    state = {}

    def _cleanup():
        mp = state.get("dst_mount")
        if mp:
            try:
                disk_ops.unmount(mp)
                if mp in _active_mounts:
                    _active_mounts.remove(mp)
            except Exception:
                pass
            state.pop("dst_mount", None)

    # ---- Step 1: source disk ----------------------------------------
    def on_src_disk(src_dev):
        state["src_dev"] = src_dev

        # Read serial before pushing dest menu so any delay is obvious
        display.draw_message(["Reading serial...", "", src_dev])
        serial = disk_ops.get_serial(src_dev)
        state["serial"] = serial
        log.info("Forensic source: %s  serial: %s", src_dev, serial)

        menu_system.push(_build_disk_menu("Dest Disk", on_dst_disk))

    # ---- Step 2: destination disk -----------------------------------
    def on_dst_disk(disk_dev):
        state["dst_disk"] = disk_dev
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            menu_system.push(
                _build_partition_menu("Dest Partition", disk_name, on_dst_dev)
            )
        else:
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _DST_MOUNTABLE:
                log.info("Forensic dst: no partitions on %s, raw-mounting as %s",
                         disk_dev, fstype)
                on_dst_dev(disk_dev)
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Found: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    def on_dst_dev(dev):
        mp = os.path.join(config.MOUNT_BASE,
                          "forensic_" + dev.replace("/dev/", ""))
        try:
            disk_ops.mount_partition(dev, mp)
            _active_mounts.append(mp)
            state["dst_mount"] = mp
        except Exception as e:
            display.draw_message(["Mount dst failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        try:
            _run_imaging()
        finally:
            _cleanup()

    # ---- Step 3: space check → confirm → dc3dd ----------------------
    def _run_imaging():
        src_dev   = state["src_dev"]
        serial    = state["serial"]
        dst_mount = state["dst_mount"]

        try:
            src_size = disk_ops.get_disk_size(src_dev)
            dst_free = disk_ops.get_free_space(dst_mount)
        except Exception as e:
            display.draw_message(["Size check failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        if src_size > dst_free:
            display.draw_message([
                "Not enough space!",
                f"Need: {disk_ops._fmt_bytes(src_size)}",
                f"Free: {disk_ops._fmt_bytes(dst_free)}",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "Forensic Image?",
            f"Src: {src_dev}",
            f"ID: {serial[:18]}",
            f"Sz: {disk_ops._fmt_bytes(src_size)}",
        ])
        if not ok:
            return

        progress = {
            "label": f"Imaging {serial[:14]}...",
            "percent": 0.0,
            "done":    False,
            "error":   "",
            "speed":   "",
        }
        disk_ops.forensic_image(src_dev, dst_mount, serial, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()  # raises ReturnToMainMenu(silent=True) on dismiss

    return _build_disk_menu("Forensic: Src Disk", on_src_disk)


# ------------------------------------------------------------------ #
# Forensic Image (E01) flow
# ------------------------------------------------------------------ #
def build_forensic_e01_flow(display, menu_system, button_queue):
    """
    Forensic Image (E01) flow — mirrors the dcfldd flow but uses ewfacquire:
      1. Select source disk (whole disk)
      2. Read serial number from source disk hardware
      3. Select destination disk → partition or raw-mount (writable FS)
      4. Space check: dst free >= src disk size
      5. Confirm screen
      6. ewfacquire: SERIAL.E01 (with embedded SHA-256)
      7. Unmount destination on exit
    """

    _DST_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs",
                      "ext4", "ext3", "ext2"}

    state = {}

    def _cleanup():
        mp = state.get("dst_mount")
        if mp:
            try:
                disk_ops.unmount(mp)
                if mp in _active_mounts:
                    _active_mounts.remove(mp)
            except Exception:
                pass
            state.pop("dst_mount", None)

    def on_src_disk(src_dev):
        state["src_dev"] = src_dev
        display.draw_message(["Reading serial...", "", src_dev])
        serial = disk_ops.get_serial(src_dev)
        state["serial"] = serial
        log.info("E01 source: %s  serial: %s", src_dev, serial)
        menu_system.push(_build_disk_menu("Dest Disk", on_dst_disk))

    def on_dst_disk(disk_dev):
        state["dst_disk"] = disk_dev
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            menu_system.push(
                _build_partition_menu("Dest Partition", disk_name, on_dst_dev)
            )
        else:
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _DST_MOUNTABLE:
                log.info("E01 dst: no partitions on %s, raw-mounting as %s",
                         disk_dev, fstype)
                on_dst_dev(disk_dev)
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Found: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    def on_dst_dev(dev):
        mp = os.path.join(config.MOUNT_BASE,
                          "e01_" + dev.replace("/dev/", ""))
        try:
            disk_ops.mount_partition(dev, mp)
            _active_mounts.append(mp)
            state["dst_mount"] = mp
        except Exception as e:
            display.draw_message(["Mount dst failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        try:
            _run_imaging()
        finally:
            _cleanup()

    def _run_imaging():
        src_dev   = state["src_dev"]
        serial    = state["serial"]
        dst_mount = state["dst_mount"]

        try:
            src_size = disk_ops.get_disk_size(src_dev)
            dst_free = disk_ops.get_free_space(dst_mount)
        except Exception as e:
            display.draw_message(["Size check failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        if src_size > dst_free:
            display.draw_message([
                "Not enough space!",
                f"Need: {disk_ops._fmt_bytes(src_size)}",
                f"Free: {disk_ops._fmt_bytes(dst_free)}",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "Forensic E01?",
            f"Src: {src_dev}",
            f"ID: {serial[:18]}",
            f"Sz: {disk_ops._fmt_bytes(src_size)}",
        ])
        if not ok:
            return

        progress = {
            "label": f"E01 {serial[:16]}...",
            "percent": 0.0,
            "done":    False,
            "error":   "",
            "speed":   "",
        }
        disk_ops.ewfacquire_image(src_dev, dst_mount, serial, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("E01: Src Disk", on_src_disk)


# ------------------------------------------------------------------ #
# VHD Image flow
# ------------------------------------------------------------------ #
def build_vhd_image_flow(display, menu_system, button_queue):
    """
    VHDX Image flow — mirrors the forensic image flows but uses qemu-img:
      1. Select source disk (whole disk)
      2. Read serial number from source disk hardware
      3. Select destination disk → partition or raw-mount (writable FS)
      4. Space check: dst free >= src disk size (worst-case VHDX size)
      5. Confirm screen
      6. qemu-img: SERIAL.vhdx (dynamic/sparse) + SERIAL.vhdx.log
      7. Unmount destination on exit
    """

    _DST_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs",
                      "ext4", "ext3", "ext2"}

    state = {}

    def _cleanup():
        mp = state.get("dst_mount")
        if mp:
            try:
                disk_ops.unmount(mp)
                if mp in _active_mounts:
                    _active_mounts.remove(mp)
            except Exception:
                pass
            state.pop("dst_mount", None)

    def on_src_disk(src_dev):
        state["src_dev"] = src_dev
        display.draw_message(["Reading serial...", "", src_dev])
        serial = disk_ops.get_serial(src_dev)
        state["serial"] = serial
        log.info("VHD source: %s  serial: %s", src_dev, serial)
        menu_system.push(_build_disk_menu("Dest Disk", on_dst_disk))

    def on_dst_disk(disk_dev):
        state["dst_disk"] = disk_dev
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            menu_system.push(
                _build_partition_menu("Dest Partition", disk_name, on_dst_dev)
            )
        else:
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _DST_MOUNTABLE:
                log.info("VHD dst: no partitions on %s, raw-mounting as %s",
                         disk_dev, fstype)
                on_dst_dev(disk_dev)
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Found: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    def on_dst_dev(dev):
        mp = os.path.join(config.MOUNT_BASE,
                          "vhd_" + dev.replace("/dev/", ""))
        try:
            disk_ops.mount_partition(dev, mp)
            _active_mounts.append(mp)
            state["dst_mount"] = mp
        except Exception as e:
            display.draw_message(["Mount dst failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        try:
            _run_imaging()
        finally:
            _cleanup()

    def _run_imaging():
        src_dev   = state["src_dev"]
        serial    = state["serial"]
        dst_mount = state["dst_mount"]

        try:
            src_size = disk_ops.get_disk_size(src_dev)
            dst_free = disk_ops.get_free_space(dst_mount)
        except Exception as e:
            display.draw_message(["Size check failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        if src_size > dst_free:
            display.draw_message([
                "Not enough space!",
                f"Need: {disk_ops._fmt_bytes(src_size)}",
                f"Free: {disk_ops._fmt_bytes(dst_free)}",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "VHDX Image?",
            f"Src: {src_dev}",
            f"ID: {serial[:18]}",
            f"Sz: {disk_ops._fmt_bytes(src_size)}",
        ])
        if not ok:
            return

        progress = {
            "label": f"VHD {serial[:15]}...",
            "percent": 0.0,
            "done":    False,
            "error":   "",
            "speed":   "",
        }
        disk_ops.vhd_image(src_dev, dst_mount, serial, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("VHD: Src Disk", on_src_disk)


# ------------------------------------------------------------------ #
# NVMe Secure Erase flow
# ------------------------------------------------------------------ #
def build_nvme_secure_erase_flow(display, menu_system, button_queue):
    """
    NVMe Secure Erase flow:
      1. List NVMe controller devices (/dev/nvme0 etc.)
      2. Query device capabilities via nvme id-ctrl (sanicap, fna)
      3. Present only the operations the device actually supports
      4. Double-confirm (single-confirm for Exit Failure)
      5. Execute with live progress screen
         - Sanitize ops: poll nvme sanitize-log (SPROG / SSTAT)
         - Format ops:   blocking command, 0 % → 100 % on completion
    """

    def on_device(dev_path, ns_path):
        # dev_path = controller  e.g. /dev/nvme0   (sanitize, id-ctrl)
        # ns_path  = namespace   e.g. /dev/nvme0n1 (format)
        if not dev_path.startswith("/dev/nvme"):
            display.draw_message([
                "Not an NVMe device:",
                dev_path,
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        # Query capabilities — show brief message while nvme id-ctrl runs
        display.draw_message(["Querying device...", "", dev_path, "Please wait..."])

        caps = disk_ops.nvme_get_capabilities(dev_path)

        if caps["error"]:
            display.draw_message([
                "id-ctrl failed:",
                caps["error"][:22],
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        # Build option list from capabilities
        options = []
        if caps["crypto_sanitize"]:
            options.append(ActionItem(
                "Cryptographic",
                lambda d=dev_path, n=ns_path: on_method(d, n, "sanitize_crypto"),
            ))
        if caps["block_sanitize"]:
            options.append(ActionItem(
                "Block",
                lambda d=dev_path, n=ns_path: on_method(d, n, "sanitize_block"),
            ))
        if caps["exit_failure"]:
            options.append(ActionItem(
                "Exit Failure",
                lambda d=dev_path, n=ns_path: on_method(d, n, "exit_failure"),
            ))
        if caps["format_crypto"]:
            options.append(ActionItem(
                "Crypto (Legacy)",
                lambda d=dev_path, n=ns_path: on_method(d, n, "format_crypto"),
            ))
        if caps["format_user"]:
            options.append(ActionItem(
                "User Data (Legacy)",
                lambda d=dev_path, n=ns_path: on_method(d, n, "format_user"),
            ))

        if not options:
            display.draw_message([
                "No erase methods",
                "found for",
                dev_path,
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        menu_system.push(MenuState("Erase Method", options))

    def on_method(dev, ns_dev, method):
        # dev    = controller path  (/dev/nvme0)   — sanitize ops
        # ns_dev = namespace path   (/dev/nvme0n1) — format ops
        method_labels = {
            "sanitize_crypto": "Crypto Sanitize",
            "sanitize_block":  "Block Sanitize",
            "exit_failure":    "Exit Failure",
            "format_crypto":   "Crypto (Legacy)",
            "format_user":     "User Data (Lgcy)",
        }
        label = method_labels.get(method, method)

        # First confirm
        ok1 = _confirm_screen(display, button_queue, [
            "Secure Erase?",
            dev,
            label,
            "ALL DATA LOST!",
        ])
        if not ok1:
            return

        # Second confirm (skip for Exit Failure — no data destruction)
        if method != "exit_failure":
            ok2 = _confirm_screen(display, button_queue, [
                "ARE YOU SURE?",
                "This cannot be",
                "undone.",
            ])
            if not ok2:
                return

        progress = {
            "label":   "...",
            "percent": 0.0,
            "done":    False,
            "error":   "",
            "speed":   "",
        }

        if method == "sanitize_crypto":
            disk_ops.nvme_sanitize_op(dev, "start-crypto-erase", progress)
        elif method == "sanitize_block":
            disk_ops.nvme_sanitize_op(dev, "start-block-erase", progress)
        elif method == "exit_failure":
            disk_ops.nvme_sanitize_op(dev, "exit-failure", progress)
        elif method == "format_crypto":
            disk_ops.nvme_format_op(ns_dev, 2, progress)
        elif method == "format_user":
            disk_ops.nvme_format_op(ns_dev, 1, progress)

        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    # ---- Build initial device list ----
    try:
        nvme_disks = disk_ops.list_nvme_disks()
    except Exception as e:
        return MenuState("NVMe Secure Erase", [
            ActionItem(f"nvme error: {e}", lambda: None)
        ])

    # Only include devices whose path starts with /dev/nvme
    nvme_disks = [d for d in nvme_disks if d.get("path", "").startswith("/dev/nvme")]

    if not nvme_disks:
        return MenuState("NVMe Secure Erase", [
            ActionItem("No compatible disks", lambda: None)
        ])

    items = []
    for d in nvme_disks:
        path    = d["path"]
        ns_path = d.get("ns_path", path + "n1")
        model   = d.get("model", "")
        # Truncate model so the label fits the OLED (max ~20 chars total)
        label = f"{path} {model[:11]}" if model else path
        items.append(ActionItem(
            label,
            lambda p=path, n=ns_path: on_device(p, n),
        ))

    return MenuState("NVMe: Select Dev", items)


# ------------------------------------------------------------------ #
# PiShrink (dd ONLY) flow
# ------------------------------------------------------------------ #
def build_pishrink_flow(display, menu_system, button_queue):
    """
    PiShrink (dd ONLY) flow:
      1. Select disk → partition (or raw-mount a known filesystem)
      2. Browse the mounted filesystem and pick a .dd image file
      3. Confirm (shows source size + target .img name + free space)
      4. pishrink.sh copies foo.dd -> foo.img and shrinks it in place
      5. Unmount the partition on exit (success, cancel, or error)
    """

    _RAW_MOUNTABLE = {"vfat", "fat", "fat32", "fat16", "exfat", "ntfs",
                      "ext4", "ext3", "ext2"}

    state = {}  # mount tracked here for cleanup

    def _cleanup():
        mp = state.get("mount")
        if mp:
            try:
                disk_ops.unmount(mp)
                if mp in _active_mounts:
                    _active_mounts.remove(mp)
            except Exception:
                pass
            state.pop("mount", None)

    def on_disk(disk_dev):
        disk_name = disk_dev.replace("/dev/", "")
        try:
            parts = disk_ops.list_partitions(disk_name)
        except Exception:
            parts = []

        if parts:
            menu_system.push(
                _build_partition_menu("Select Partition", disk_name, on_part)
            )
        else:
            fstype = disk_ops.detect_fstype(disk_dev)
            if fstype in _RAW_MOUNTABLE:
                log.info("PiShrink: no partitions on %s, raw-mounting as %s",
                         disk_dev, fstype)
                on_part(disk_dev)
            else:
                display.draw_message([
                    "No partitions &",
                    "no known FS.",
                    f"Found: {fstype or 'none'}",
                    "",
                    "Press any button",
                ])
                button_queue.get(timeout=15)

    def on_part(dev):
        mp = os.path.join(config.MOUNT_BASE, "pishrink_" + dev.replace("/dev/", ""))
        try:
            disk_ops.mount_partition(dev, mp)
            _active_mounts.append(mp)
            state["mount"] = mp
        except Exception as e:
            display.draw_message(["Mount failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        try:
            _pick_and_run(mp, dev)
        finally:
            _cleanup()

    def _pick_and_run(mount_point, dev):
        browser = FileBrowser(display, mount_point, dev, pick_exts=(".dd",))

        running = True
        while running:
            browser.render()
            event = button_queue.get()
            if event == config.BTN_A_LONG:
                raise config.ReturnToMainMenu()
            running = browser.handle_event(event)

        src_dd = browser.picked_path
        if not src_dd:
            return  # user backed out at root without picking

        # PiShrink can only shrink images whose last partition is ext2/3/4.
        # Check up front so we don't do a full-size copy that's doomed to fail.
        display.draw_message(["Checking image...", "", os.path.basename(src_dd)[:20]])
        last_fs = disk_ops.get_image_last_fstype(src_dd)
        if last_fs not in disk_ops.PISHRINK_OK_FS:
            display.draw_message([
                "Cannot shrink:",
                "last partition is",
                f"{last_fs or 'unknown'}, not ext.",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        stem = os.path.basename(src_dd)
        stem = stem[:-3] if stem.lower().endswith(".dd") else os.path.splitext(stem)[0]
        out_name = stem + ".img"

        try:
            src_size = os.path.getsize(src_dd)
            free = disk_ops.get_free_space(mount_point)
        except Exception as e:
            display.draw_message(["Size check failed:", str(e)[:22], "",
                                  "Press any button"])
            button_queue.get(timeout=15)
            return

        if src_size > free:
            display.draw_message([
                "Not enough space!",
                f"Need: {disk_ops._fmt_bytes(src_size)}",
                f"Free: {disk_ops._fmt_bytes(free)}",
                "",
                "Press any button",
            ])
            button_queue.get(timeout=15)
            return

        ok = _confirm_screen(display, button_queue, [
            "PiShrink?",
            f"Src: {os.path.basename(src_dd)[:16]}",
            f"Out: {out_name[:16]}",
            f"Sz: {disk_ops._fmt_bytes(src_size)}",
        ])
        if not ok:
            return

        progress = {
            "label":   "PiShrink...",
            "percent": 0.0,
            "done":    False,
            "error":   "",
        }
        disk_ops.pishrink_image(src_dd, progress)
        ps = ProgressScreen(display, button_queue, progress)
        ps.run()

    return _build_disk_menu("PiShrink: Disk", on_disk)


# ------------------------------------------------------------------ #
# Main menu construction
# ------------------------------------------------------------------ #
def build_shutdown_flow(display, button_queue):
    def do_shutdown():
        ok = _confirm_screen(display, button_queue, [
            "Shutdown?",
            "System will power",
            "off.",
        ])
        if ok:
            display.draw_message(["Shutting down...", "", "Safe to unplug", "after LED off."])
            disk_ops.shutdown_os()

    return ActionItem("Shutdown", do_shutdown)


def build_main_menu(display, menu_system, button_queue) -> MenuState:
    return MenuState("", [
        SubMenuItem(
            "Browse Disk",
            lambda: build_browse_disk_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Format Disk",
            lambda: build_format_disk_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Wipe Disk (dd)",
            lambda: build_wipe_disk_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Secure Erase (NVMe)",
            lambda: build_nvme_secure_erase_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Copy Files",
            lambda: build_copy_files_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Clone Disk (dd)",
            lambda: build_clone_disk_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "RAW/dd Image (dcfldd)",
            lambda: build_forensic_image_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "Forensic Image (E01)",
            lambda: build_forensic_e01_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "VHDX Image",
            lambda: build_vhd_image_flow(display, menu_system, button_queue)
        ),
        SubMenuItem(
            "PiShrink (dd ONLY)",
            lambda: build_pishrink_flow(display, menu_system, button_queue)
        ),
        build_shutdown_flow(display, button_queue),
    ], visible_rows=5)


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #
def main():
    log.info("USB Imager starting")
    display = Display()
    buttons = ButtonHandler()
    menu_system = MenuSystem(display)

    # Show startup message
    display.draw_message(["USB Imager", "Starting..."])
    time.sleep(1)

    # Build and push main menu
    main_menu = build_main_menu(display, menu_system, buttons.queue)
    menu_system.push(main_menu)
    menu_system.render()

    # SIGTERM handler
    def _sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    # ---- Event loop ----
    try:
        while True:
            event = buttons.queue.get()  # blocks until button press
            try:
                dirty = menu_system.handle_event(event)
            except config.ReturnToMainMenu as e:
                menu_system.pop_to_root()
                if not e.silent:
                    display.draw_message(["Cancelled.", "Returned to", "main menu."])
                    time.sleep(1)
                dirty = True
            if dirty:
                menu_system.render()
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt / SIGTERM)")
    finally:
        _cleanup(display, buttons)
        log.info("USB Imager stopped")


if __name__ == "__main__":
    main()
