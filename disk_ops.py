# disk_ops.py — All disk operations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import config

log = logging.getLogger("disk_ops")


# ------------------------------------------------------------------ #
# Exceptions
# ------------------------------------------------------------------ #
class DiskOpsError(Exception):
    pass


class InsufficientSpaceError(DiskOpsError):
    def __init__(self, needed: int, available: int):
        self.needed = needed
        self.available = available
        super().__init__(
            f"Need {_fmt_bytes(needed)}, only {_fmt_bytes(available)} available"
        )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _get_mount_fstype(mount_point: str) -> str:
    """Return the filesystem type for a mounted path, from /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == mount_point:
                    return parts[2].lower()
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ #
# Disk / partition discovery
# ------------------------------------------------------------------ #
def list_disks() -> list[dict]:
    """
    Return a list of block devices, excluding mmcblk* (Pi SD card).
    Each entry: {name, size, type, mountpoint, label, fstype, path}
    type == 'disk' for whole disks.
    """
    log.info("Listing disks (excluding %s)", ", ".join(f"{p}*" for p in config.EXCLUDED_PREFIXES))
    result = _run([
        "lsblk", "-J",
        "-o", "NAME,SIZE,TYPE,MOUNTPOINT,LABEL,FSTYPE"
    ])
    data = json.loads(result.stdout)
    disks = []
    for dev in data.get("blockdevices", []):
        name = dev.get("name", "")
        if any(name.startswith(p) for p in config.EXCLUDED_PREFIXES):
            log.debug("  Skipping %s (protected)", name)
            continue
        if dev.get("type") != "disk":
            continue
        if dev.get("size", "") in ("0B", "0", "", None):
            log.debug("  Skipping %s (0B size)", name)
            continue
        dev["path"] = f"/dev/{name}"
        disks.append(dev)
    log.info("  Found %d disk(s): %s", len(disks),
             ", ".join(f"/dev/{d['name']} ({d.get('size','?')})" for d in disks))
    return disks


def list_partitions(disk_name: str) -> list[dict]:
    """
    Return partitions for a given disk name (e.g. 'sda').
    Excludes the disk entry itself; returns children.
    """
    log.info("Listing partitions on /dev/%s", disk_name)
    result = _run([
        "lsblk", "-J",
        "-o", "NAME,SIZE,TYPE,MOUNTPOINT,LABEL,FSTYPE",
        f"/dev/{disk_name}"
    ])
    data = json.loads(result.stdout)
    partitions = []
    for dev in data.get("blockdevices", []):
        for child in dev.get("children", []):
            if child.get("type") in ("part", "lvm"):
                child["path"] = f"/dev/{child['name']}"
                partitions.append(child)
    log.info("  Found %d partition(s): %s", len(partitions),
             ", ".join(f"/dev/{p['name']} {p.get('fstype','?')} ({p.get('size','?')})"
                       for p in partitions))
    return partitions


# ------------------------------------------------------------------ #
# Mount / unmount
# ------------------------------------------------------------------ #
_FSTYPE_MAP = {
    "ntfs":  "ntfs3",   # kernel driver (Trixie 6.x)
    "vfat":  "vfat",
    "fat32": "vfat",
    "exfat": "exfat",
    "ext4":  "ext4",
    "ext3":  "ext3",
    "ext2":  "ext2",
}


def mount_partition(dev: str, mount_point: str, fstype: str | None = None) -> str:
    """
    Mount `dev` at `mount_point`.
    Auto-detects fstype from lsblk if not provided.
    Returns the mount point path.
    """
    os.makedirs(mount_point, exist_ok=True)

    if fstype is None:
        r = _run(["lsblk", "-no", "FSTYPE", dev])
        fstype = r.stdout.strip().lower()
        log.info("Auto-detected fstype for %s: %s", dev, fstype or "(none)")

    fs_arg = _FSTYPE_MAP.get(fstype.lower(), fstype.lower())
    log.info("Mounting %s (%s) -> %s", dev, fs_arg, mount_point)

    # Try preferred fstype; fall back to ntfs-3g for NTFS if ntfs3 fails
    try:
        _run(["sudo", "mount", "-t", fs_arg, dev, mount_point])
        log.info("  Mount OK")
    except subprocess.CalledProcessError as e:
        if fs_arg == "ntfs3":
            log.warning("  ntfs3 failed (%s), retrying with ntfs-3g", e)
            _run(["sudo", "mount", "-t", "ntfs-3g", dev, mount_point])
            log.info("  Mount OK (ntfs-3g fallback)")
        else:
            log.error("  Mount FAILED: %s", e)
            raise

    return mount_point


def unmount(mount_point: str):
    """Unmount; ignore if already unmounted."""
    log.info("Unmounting %s", mount_point)
    try:
        _run(["sudo", "umount", mount_point])
        log.info("  Unmount OK")
    except subprocess.CalledProcessError:
        log.debug("  Unmount skipped (already unmounted or not mounted)")
        pass


# ------------------------------------------------------------------ #
# Space checks
# ------------------------------------------------------------------ #
def get_disk_size(dev: str) -> int:
    """Return size of block device in bytes."""
    result = _run(["sudo", "blockdev", "--getsize64", dev])
    size = int(result.stdout.strip())
    log.debug("Size of %s: %s", dev, _fmt_bytes(size))
    return size


def get_free_space(mount_point: str) -> int:
    """Return free bytes at mount_point."""
    return shutil.disk_usage(mount_point).free


def get_used_space(mount_point: str) -> int:
    """Return used bytes at mount_point."""
    usage = shutil.disk_usage(mount_point)
    return usage.used


def check_space_copy(src_mount: str, dst_mount: str):
    """Raise InsufficientSpaceError if dst free < src used."""
    needed = get_used_space(src_mount)
    available = get_free_space(dst_mount)
    log.info("Space check (copy): need %s, dst free %s",
             _fmt_bytes(needed), _fmt_bytes(available))
    if needed > available:
        log.error("  Insufficient space: need %s but only %s free",
                  _fmt_bytes(needed), _fmt_bytes(available))
        raise InsufficientSpaceError(needed, available)
    log.info("  Space check OK")


def check_space_clone(src_dev: str, dst_dev: str):
    """Raise InsufficientSpaceError if dst disk size < src disk size."""
    needed = get_disk_size(src_dev)
    available = get_disk_size(dst_dev)
    log.info("Space check (clone): src %s, dst %s",
             _fmt_bytes(needed), _fmt_bytes(available))
    if needed > available:
        log.error("  Insufficient space: src %s > dst %s",
                  _fmt_bytes(needed), _fmt_bytes(available))
        raise InsufficientSpaceError(needed, available)
    log.info("  Space check OK")


# ------------------------------------------------------------------ #
# Long-running operations — run in a background thread
# ------------------------------------------------------------------ #
def _parse_dd_line(line: str) -> tuple[int | None, str]:
    """
    Parse a dd status=progress stderr line.
    Example: '1073741824 bytes (1.1 GB) copied, 15.4 s, 69.7 MB/s'
    Returns (bytes_copied_or_None, speed_string).
    """
    bytes_copied = None
    speed = ""
    m = re.search(r"(\d+)\s+bytes", line)
    if m:
        bytes_copied = int(m.group(1))
    m2 = re.search(r"([\d.]+)\s*MB/s", line)
    if m2:
        speed = f"{float(m2.group(1)):.1f} MB/s"
    return bytes_copied, speed


def _parse_rsync_line(line: str) -> tuple[float | None, str]:
    """
    Parse an rsync --info=progress2 output line.
    Example: '    1073741824  45%   68.66MB/s    0:00:05'
    Returns (percent_or_None, speed_string).
    """
    pct = None
    speed = ""
    m = re.search(r"\s+(\d+)%", line)
    if m:
        pct = float(m.group(1))
    m2 = re.search(r"([\d.]+)MB/s", line)
    if m2:
        speed = f"{float(m2.group(1)):.1f} MB/s"
    return pct, speed


def detect_fstype(dev: str) -> str:
    """Return the filesystem type on dev as reported by lsblk, or '' if none/unknown."""
    try:
        r = _run(["lsblk", "-no", "FSTYPE", dev])
        return r.stdout.strip().lower()
    except Exception:
        return ""


def _size_label(dev: str) -> str:
    """Return a short, uppercase size string suitable for a volume label (e.g. '16GB')."""
    try:
        n = get_disk_size(dev)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{round(n)}{unit}"
            n /= 1024
        return f"{round(n)}PB"
    except Exception:
        return "DISK"


def copy_files(src_mount: str, dst_mount: str, progress: dict):
    """
    Copy files from src_mount to dst_mount using rsync.
    progress dict keys: label, percent, done, error
    Pre-checks space before starting.
    """
    log.info("copy_files: %s -> %s", src_mount, dst_mount)
    try:
        check_space_copy(src_mount, dst_mount)
    except InsufficientSpaceError as e:
        log.error("copy_files aborted: %s", e)
        progress["error"] = str(e)
        progress["done"] = True
        return

    progress["label"] = "Copying files..."
    progress["percent"] = 0.0

    def _run_rsync():
        try:
            dst_fstype = _get_mount_fstype(dst_mount)
            fat_like  = dst_fstype in {"vfat", "fat", "fat32", "fat16", "exfat"}
            ntfs_like = dst_fstype in {"ntfs", "ntfs3", "fuseblk"}
            log.info("  dst fstype: %s (fat_like=%s ntfs_like=%s)",
                     dst_fstype or "unknown", fat_like, ntfs_like)

            extra: list[str] = []
            if fat_like or ntfs_like:
                # FAT/exFAT/NTFS don't support Unix permission bits or ownership;
                # rsync fails on every file if it tries to apply them.
                extra += ["--no-perms", "--no-owner", "--no-group"]
            if fat_like:
                # Symlinks don't exist on FAT/exFAT — dereference them to their targets.
                # FAT timestamps have 2-second granularity; widen the comparison window.
                extra += ["--copy-links", "--modify-window=2"]

            # -v: verbose — rsync logs each file transferred and any per-file errors
            # stderr kept separate so error lines don't corrupt progress parsing
            cmd = ["rsync", "-av", "--info=progress2"] + extra + [src_mount + "/", dst_mount + "/"]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Collect stderr (per-file errors, permission denied, etc.) in background
            stderr_lines: list[str] = []

            def _read_stderr():
                for line in proc.stderr:
                    line = line.rstrip()
                    if line:
                        log.warning("  rsync stderr: %s", line)
                        stderr_lines.append(line)

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # Parse stdout for progress + per-file verbose lines
            _last_logged = -10.0
            for line in proc.stdout:
                pct, speed = _parse_rsync_line(line)
                if pct is not None:
                    progress["percent"] = pct
                    if speed:
                        progress["speed"] = speed
                    if pct - _last_logged >= 10.0:
                        log.info("  rsync progress: %.0f%% %s", pct, speed)
                        _last_logged = pct
                else:
                    stripped = line.strip()
                    if stripped:
                        log.debug("  rsync: %s", stripped)

            proc.wait()
            stderr_thread.join(timeout=2)

            if proc.returncode != 0:
                # rsync exit 23 = partial transfer; stderr contains the failing paths
                log.error("  rsync FAILED (exit %d), %d stderr line(s)",
                          proc.returncode, len(stderr_lines))
                for l in stderr_lines:
                    log.error("    %s", l)
                # Build a concise on-screen error: code + count + last failing path
                failed_files = [l for l in stderr_lines if "failed" in l.lower() or "error" in l.lower()]
                if failed_files:
                    summary = f"{len(failed_files)} file(s) failed"
                    # Extract just the filename from the last failed line for display
                    last = failed_files[-1]
                    # rsync lines look like: 'rsync: open "/path/file" failed: ...'
                    m = re.search(r'"([^"]+)"', last)
                    if m:
                        fname = m.group(1).split("/")[-1]  # basename only
                        summary += f": {fname}"
                else:
                    summary = f"exit {proc.returncode}"
                progress["error"] = f"rsync err {proc.returncode}: {summary}"
            else:
                progress["percent"] = 100.0
                log.info("  rsync complete")
        except Exception as e:
            progress["error"] = str(e)
            log.exception("  rsync exception: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_rsync, daemon=True)
    t.start()


def dd_clone(src_dev: str, dst_dev: str, progress: dict):
    """
    Clone src_dev to dst_dev using dd.
    progress dict: label, percent, done, error
    Pre-checks that dst_dev >= src_dev in size.
    """
    log.info("dd_clone: %s -> %s", src_dev, dst_dev)
    try:
        check_space_clone(src_dev, dst_dev)
        total_bytes = get_disk_size(src_dev)
    except InsufficientSpaceError as e:
        log.error("dd_clone aborted: %s", e)
        progress["error"] = str(e)
        progress["done"] = True
        return
    except Exception as e:
        log.error("dd_clone aborted (size query failed): %s", e)
        progress["error"] = str(e)
        progress["done"] = True
        return

    log.info("  Source size: %s", _fmt_bytes(total_bytes))
    progress["label"] = f"Cloning {src_dev}..."
    progress["percent"] = 0.0

    def _run_dd():
        try:
            cmd = [
                "sudo", "dd",
                f"if={src_dev}", f"of={dst_dev}",
                "bs=4M", "status=progress"
            ]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )

            _last_logged = -10.0

            def read_stderr():
                nonlocal _last_logged
                for line in proc.stderr:
                    copied, speed = _parse_dd_line(line)
                    if copied is not None and total_bytes > 0:
                        pct = min(100.0, copied / total_bytes * 100)
                        progress["percent"] = pct
                        if speed:
                            progress["speed"] = speed
                        if pct - _last_logged >= 10.0:
                            log.info("  dd progress: %s / %s (%.0f%%) %s",
                                     _fmt_bytes(copied), _fmt_bytes(total_bytes), pct, speed)
                            _last_logged = pct

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            proc.wait()
            stderr_thread.join(timeout=2)

            if proc.returncode != 0:
                progress["error"] = f"dd exited {proc.returncode}"
                log.error("  dd FAILED (exit %d)", proc.returncode)
            else:
                progress["percent"] = 100.0
                log.info("  dd clone complete")
        except Exception as e:
            progress["error"] = str(e)
            log.exception("  dd exception: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_dd, daemon=True)
    t.start()


def _partition_name(dev: str, n: int = 1) -> str:
    """
    Return the name of the Nth partition on a block device.
    /dev/sda  -> /dev/sda1
    /dev/nvme0n1 -> /dev/nvme0n1p1  (device name ends with a digit)
    """
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def _run_cmd_logged(cmd: list, label: str) -> tuple[int, str]:
    """Run a command, log its output, return (returncode, combined output)."""
    log.info("  Running: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    out, _ = proc.communicate()
    for line in (out or "").strip().splitlines():
        log.debug("  %s: %s", label, line)
    return proc.returncode, out or ""


def format_disk(dev: str, fs_type: str, progress: dict):
    """
    Write an MBR partition table, create one primary partition spanning
    the whole disk, then format it as fat32 or exfat.
    progress dict: label, percent, done, error
    """
    log.info("format_disk: %s as %s (with MBR partition table)", dev, fs_type)
    progress["label"] = f"Partitioning {dev}..."
    progress["percent"] = 0.0

    def _run_fmt():
        try:
            # ---- Step 1: write MBR partition table ----
            rc, out = _run_cmd_logged(
                ["sudo", "parted", "-s", dev, "mklabel", "msdos"],
                "parted"
            )
            if rc != 0:
                raise DiskOpsError(f"parted mklabel failed ({rc}): {out[:80]}")
            progress["percent"] = 25.0
            log.info("  MBR partition table written")

            # ---- Step 2: create single primary partition ----
            rc, out = _run_cmd_logged(
                ["sudo", "parted", "-s", dev, "mkpart", "primary", "0%", "100%"],
                "parted"
            )
            if rc != 0:
                raise DiskOpsError(f"parted mkpart failed ({rc}): {out[:80]}")
            progress["percent"] = 50.0
            log.info("  Primary partition created")

            # ---- Step 3: let the kernel see the new partition ----
            _run_cmd_logged(["sudo", "udevadm", "settle"], "udevadm")
            time.sleep(0.5)  # belt-and-suspenders for slow USB controllers

            part = _partition_name(dev)
            vol_label = _size_label(dev)
            progress["label"] = f"Formatting {part}..."
            progress["percent"] = 60.0
            log.info("  Formatting partition %s as %s, label=%s", part, fs_type, vol_label)

            # ---- Step 4: format the partition with volume label = disk size ----
            if fs_type == "fat32":
                cmd = ["sudo", "mkfs.fat", "-F", "32", "-n", vol_label[:11].upper(), part]
            elif fs_type == "exfat":
                cmd = ["sudo", "mkfs.exfat", "-L", vol_label[:15], part]
            elif fs_type == "ntfs":
                # -f = fast format (skip zeroing), -L = volume label (max 32 chars)
                cmd = ["sudo", "mkfs.ntfs", "-f", "-L", vol_label[:32], part]
            else:
                raise DiskOpsError(f"Unsupported fs_type: {fs_type}")

            rc, out = _run_cmd_logged(cmd, "mkfs")
            if rc != 0:
                raise DiskOpsError(f"mkfs failed ({rc}): {out[:80]}")

            progress["percent"] = 100.0
            log.info("  format_disk complete — partition: %s", part)

        except Exception as e:
            progress["error"] = str(e)
            log.error("  format_disk FAILED: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_fmt, daemon=True)
    t.start()


def wipe_disk(dev: str, progress: dict):
    """
    Wipe dev by writing zeros using dd.
    progress dict: label, percent, done, error
    """
    log.info("wipe_disk: %s", dev)
    try:
        total_bytes = get_disk_size(dev)
    except Exception as e:
        log.error("wipe_disk aborted (size query failed): %s", e)
        progress["error"] = str(e)
        progress["done"] = True
        return

    log.info("  Disk size: %s", _fmt_bytes(total_bytes))
    progress["label"] = f"Wiping {dev}..."
    progress["percent"] = 0.0

    def _run_wipe():
        try:
            cmd = [
                "sudo", "dd",
                "if=/dev/zero", f"of={dev}",
                "bs=4M", "status=progress"
            ]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )

            _last_logged = -10.0

            def read_stderr():
                nonlocal _last_logged
                for line in proc.stderr:
                    copied, speed = _parse_dd_line(line)
                    if copied is not None and total_bytes > 0:
                        pct = min(100.0, copied / total_bytes * 100)
                        progress["percent"] = pct
                        if speed:
                            progress["speed"] = speed
                        if pct - _last_logged >= 10.0:
                            log.info("  wipe progress: %s / %s (%.0f%%) %s",
                                     _fmt_bytes(copied), _fmt_bytes(total_bytes), pct, speed)
                            _last_logged = pct

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            proc.wait()
            stderr_thread.join(timeout=2)

            # dd writing to a block device exits 1 when it hits end-of-device — that's normal
            progress["percent"] = 100.0
            log.info("  wipe complete (dd exit %d — 1 is normal for block devices)",
                     proc.returncode)
        except Exception as e:
            progress["error"] = str(e)
            log.exception("  wipe exception: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_wipe, daemon=True)
    t.start()


def shutdown_os():
    """Immediately shut down the OS. Does not return."""
    log.info("Shutting down OS via 'sudo shutdown -h now'")
    subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)


# ------------------------------------------------------------------ #
# Forensic imaging (dcfldd)
# ------------------------------------------------------------------ #
def get_serial(dev: str) -> str:
    """
    Return the hardware serial number of a block device, sanitised for use
    as a filename.  Prefers ID_SERIAL_SHORT (bare serial), falls back to
    ID_SERIAL (vendor+model+serial), then the device name.
    """
    serial = ""
    try:
        r = _run(["udevadm", "info", "--query=property", f"--name={dev}"])
        for line in r.stdout.splitlines():
            if line.startswith("ID_SERIAL_SHORT="):
                serial = line.split("=", 1)[1].strip()
                break
            if line.startswith("ID_SERIAL=") and not serial:
                serial = line.split("=", 1)[1].strip()
    except Exception as e:
        log.warning("get_serial: udevadm failed for %s: %s", dev, e)

    if not serial:
        serial = dev.replace("/dev/", "")
        log.warning("get_serial: no serial found for %s, using '%s'", dev, serial)
    else:
        log.info("get_serial: %s -> '%s'", dev, serial)

    # Keep only alphanumeric, hyphen, underscore — safe for any filesystem
    serial = re.sub(r"[^\w\-]", "_", serial)
    return serial


def _parse_dcfldd_line(line: str, total_bytes: int,
                        last_state: dict) -> tuple[float | None, str]:
    """
    Parse a dcfldd status line.  dcfldd writes two kinds of lines to stderr:

        "N blocks (XMb) written."      — emitted every statusinterval blocks
        "X% done, H:MM remaining."     — emitted when sizeprobe=if is set

    Returns (percent_or_None, speed_string).
    last_state dict carries {"bytes": int, "time": float} across calls so
    speed can be computed from delta bytes / delta time.
    """
    pct = None
    speed = ""

    # Percentage line  e.g. "45% done, 0:30 remaining."
    m = re.search(r"(\d+)%\s+done", line)
    if m:
        pct = float(m.group(1))

    # Blocks-written line  e.g. "128 blocks (512Mb) written."
    m2 = re.search(r"(\d+)\s+blocks\s+\(([\d.]+)(Mb|Gb)\)\s+written", line,
                   re.IGNORECASE)
    if m2:
        val  = float(m2.group(2))
        unit = m2.group(3).upper()
        written_bytes = int(val * (1024 ** 3 if unit == "GB" else 1024 ** 2))

        now = time.monotonic()
        prev_bytes = last_state.get("bytes", 0)
        prev_time  = last_state.get("time", now)
        dt = now - prev_time

        if dt > 0 and written_bytes > prev_bytes:
            mb_per_s = (written_bytes - prev_bytes) / dt / (1024 ** 2)
            speed = f"{mb_per_s:.1f} MB/s"

        last_state["bytes"] = written_bytes
        last_state["time"]  = now

        # Derive percent from bytes when sizeprobe line hasn't arrived yet
        if pct is None and total_bytes > 0:
            pct = min(100.0, written_bytes / total_bytes * 100)

    return pct, speed


def _parse_ewf_line(line: str) -> tuple[float | None, str]:
    """
    Parse an ewfacquire status line.
    Example: 'Status: at 12%'
             '        completion in 5 minutes with 8.5 MiB/s'
    Returns (percent_or_None, speed_string).
    """
    pct = None
    speed = ""
    m = re.search(r"(\d+)%", line)
    if m:
        pct = float(m.group(1))
    m2 = re.search(r"([\d.]+)\s*MiB/s", line)
    if m2:
        speed = f"{float(m2.group(1)):.1f} MB/s"
    return pct, speed


def _parse_qemu_line(line: str) -> tuple[float | None, str]:
    """
    Parse qemu-img convert -p progress output.
    qemu-img writes lines like '    (10.00/100%)' to stderr, terminated with \\r.
    Python universal-newlines mode converts \\r to \\n so each update is a separate line.
    Returns (percent_or_None, "") — qemu-img does not emit transfer speed.
    """
    m = re.search(r'\((\d+(?:\.\d+)?)/100%\)', line)
    if m:
        return float(m.group(1)), ""
    return None, ""


def ewfacquire_image(src_dev: str, dst_mount: str, serial: str, progress: dict):
    """
    Create a forensic E01 image of src_dev using ewfacquire (ewf-tools).

    Output written to dst_mount:
        {serial}.E01            — E01 image with embedded MD5 + SHA-256
        {serial}.ewfacquire.log — acquisition log (-l)

    Chunk size is 4096 sectors per read (-b 4096).
    Runs unattended via -u (no interactive prompts).
    progress dict: label, percent, done, error, speed
    """
    target  = os.path.join(dst_mount, serial)      # ewfacquire appends .E01 automatically
    log_path = os.path.join(dst_mount, f"{serial}.ewfacquire.log")
    log.info("ewfacquire_image: %s -> %s.E01", src_dev, target)
    log.info("  log: %s", log_path)
    log.info("  chunk size: 4096 sectors")

    progress["label"] = f"E01 {src_dev}..."
    progress["percent"] = 0.0

    def _run_ewf():
        try:
            total_bytes = get_disk_size(src_dev)
            log.info("  Source size: %s", _fmt_bytes(total_bytes))
        except Exception as e:
            log.warning("  Could not get source size: %s", e)

        try:
            cmd = [
                "sudo", "ewfacquire",
                "-t", target,        # base output path (ewfacquire appends .E01)
                "-b", "4096",        # sectors to read per chunk
                "-f", "encase6",     # E01 format (EnCase 6 compatible)
                "-d", "sha256",      # additional digest — MD5 always included
                "-e", "Pi_USB_Imager",    # examiner name
                "-D", serial,        # description = device serial
                "-l", log_path,      # write acquisition log to destination
                "-u",                # unattended — no interactive prompts
                src_dev,
            ]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
                bufsize=1,
            )

            _last_logged = -10.0
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                log.debug("  ewfacquire: %s", line)
                pct, speed = _parse_ewf_line(line)
                if pct is not None:
                    progress["percent"] = min(100.0, pct)
                    if pct - _last_logged >= 10.0:
                        log.info("  ewf progress: %.0f%% %s", pct, speed)
                        _last_logged = pct
                if speed:
                    progress["speed"] = speed

            proc.wait()

            if proc.returncode != 0:
                log.error("  ewfacquire FAILED (exit %d)", proc.returncode)
                progress["error"] = f"ewfacquire exit {proc.returncode}"
            else:
                progress["percent"] = 100.0
                log.info("  E01 image complete: %s.E01", target)

        except Exception as e:
            progress["error"] = str(e)
            log.exception("  ewfacquire exception: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_ewf, daemon=True)
    t.start()


def vhd_image(src_dev: str, dst_mount: str, serial: str, progress: dict):
    """
    Create a sparse (dynamic) VHDX image of src_dev using qemu-img.

    Output files written to dst_mount:
        {serial}.vhdx     — dynamic VHDX (subformat=dynamic, up to 64 TB)
        {serial}.vhdx.log — operation log

    qemu-img streams directly from the block device to VHDX format in one pass;
    no intermediate raw file is created.
    progress dict: label, percent, done, error, speed
    """
    image_path = os.path.join(dst_mount, f"{serial}.vhdx")
    op_log     = os.path.join(dst_mount, f"{serial}.vhdx.log")

    log.info("vhd_image: %s -> %s", src_dev, image_path)

    progress["label"] = f"VHD {src_dev}..."
    progress["percent"] = 0.0

    def _run_qemu():
        log_lines: list[str] = []

        try:
            total_bytes = get_disk_size(src_dev)
            log.info("  Source size: %s", _fmt_bytes(total_bytes))
        except Exception as e:
            log.warning("  Could not get source size: %s", e)

        try:
            cmd = [
                "sudo", "qemu-img", "convert",
                "-p",                       # write progress to stderr (\\r-terminated)
                "-f", "raw",                # source is a raw block device
                "-O", "vhdx",               # VHDX (Hyper-V, mountable on Windows 8+, up to 64 TB)
                "-o", "subformat=dynamic",  # sparse — only allocates sectors with data
                src_dev,
                image_path,
            ]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,   # universal newlines converts \\r to \\n for progress lines
                bufsize=1,
            )

            _last_logged = -10.0

            def _read_stdout():
                nonlocal _last_logged
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        log_lines.append(line)
                    pct, _ = _parse_qemu_line(line)
                    if pct is not None:
                        progress["percent"] = min(100.0, pct)
                        if pct - _last_logged >= 10.0:
                            log.info("  qemu-img progress: %.0f%%", pct)
                            _last_logged = pct

            stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
            stdout_thread.start()

            proc.stderr.read()  # drain stderr
            proc.wait()
            stdout_thread.join(timeout=5)

            try:
                with open(op_log, "w") as f:
                    f.write("\n".join(log_lines))
                    f.write("\n")
                log.info("  op log written: %s", op_log)
            except Exception as e:
                log.warning("  Could not write op log: %s", e)

            if proc.returncode != 0:
                log.error("  qemu-img FAILED (exit %d)", proc.returncode)
                for ln in log_lines[-10:]:
                    log.error("    %s", ln)
                progress["error"] = f"qemu-img exit {proc.returncode}"
            else:
                progress["percent"] = 100.0
                log.info("  VHDX image complete: %s", image_path)

        except Exception as e:
            progress["error"] = str(e)
            log.exception("  qemu-img exception: %s", e)
        finally:
            progress["done"] = True

    threading.Thread(target=_run_qemu, daemon=True).start()


def list_nvme_disks() -> list[dict]:
    """
    Return a list of NVMe devices.
    Each entry: {path, ns_path, model}
        path    — controller device  (e.g. /dev/nvme0)   used by sanitize / id-ctrl
        ns_path — namespace device   (e.g. /dev/nvme0n1) used by nvme format
    Uses 'nvme list -o json' where available; falls back to /dev/nvme* glob.
    """
    seen: set[str] = set()
    disks: list[dict] = []

    try:
        r = _run(["nvme", "list", "-o", "json"])
        data = json.loads(r.stdout)
        for dev in data.get("Devices", []):
            ns_path = dev.get("DevicePath", "")          # e.g. /dev/nvme0n1
            m = re.match(r"(/dev/nvme\d+)n\d+", ns_path)
            if m:
                ctrl = m.group(1)                        # e.g. /dev/nvme0
                if ctrl not in seen:
                    seen.add(ctrl)
                    disks.append({
                        "path":    ctrl,
                        "ns_path": ns_path,
                        "model":   dev.get("ModelNumber", "").strip(),
                    })
    except Exception:
        # Fallback: scan /dev for controller nodes (nvme0, not nvme0n1)
        for path in sorted(glob.glob("/dev/nvme[0-9]*")):
            name = os.path.basename(path)
            if re.match(r"^nvme\d+$", name) and path not in seen:
                seen.add(path)
                # Derive first namespace path; nvme format requires it
                disks.append({
                    "path":    path,
                    "ns_path": path + "n1",
                    "model":   "",
                })

    log.info("list_nvme_disks: %d device(s): %s",
             len(disks),
             ", ".join(f"{d['path']} ({d['ns_path']})" for d in disks))
    return disks


def nvme_get_capabilities(dev: str) -> dict:
    """
    Query nvme id-ctrl and return which secure erase operations are supported.

    Returned keys (all bool unless noted):
        crypto_sanitize  — nvme sanitize start-crypto-erase  (sanicap bit 0)
        block_sanitize   — nvme sanitize start-block-erase   (sanicap bit 1)
        exit_failure     — nvme sanitize exit-failure         (true if any sanitize supported)
        format_crypto    — nvme format -s 2                   (fna bit 2)
        format_user      — nvme format -s 1                   (always True when reachable)
        error            — str, non-empty if id-ctrl failed
    """
    caps = {
        "crypto_sanitize": False,
        "block_sanitize":  False,
        "exit_failure":    False,
        "format_crypto":   False,
        "format_user":     True,   # User Data Erase via Format is universally supported
        "error":           "",
    }
    try:
        r = _run(["nvme", "id-ctrl", dev])
        out = r.stdout

        # sanicap: bit 0 = Crypto Erase, bit 1 = Block Erase, bit 2 = Overwrite
        m = re.search(r"^sanicap\s*:\s*(0x[0-9a-fA-F]+|\d+)", out, re.MULTILINE)
        if m:
            sanicap = int(m.group(1), 0)
            caps["crypto_sanitize"] = bool(sanicap & 0x1)
            caps["block_sanitize"]  = bool(sanicap & 0x2)
            log.info("nvme_get_capabilities: %s sanicap=0x%02x "
                     "(crypto=%s block=%s)",
                     dev, sanicap,
                     caps["crypto_sanitize"], caps["block_sanitize"])
        else:
            log.warning("nvme_get_capabilities: sanicap field not found for %s", dev)

        # exit-failure is relevant whenever any sanitize operation is supported
        caps["exit_failure"] = caps["crypto_sanitize"] or caps["block_sanitize"]

        # fna bit 2: Cryptographic Erase supported via Format NVM (ses=2)
        m = re.search(r"^fna\s*:\s*(0x[0-9a-fA-F]+|\d+)", out, re.MULTILINE)
        if m:
            fna = int(m.group(1), 0)
            caps["format_crypto"] = bool(fna & 0x4)
            log.info("nvme_get_capabilities: %s fna=0x%02x (format_crypto=%s)",
                     dev, fna, caps["format_crypto"])

    except Exception as e:
        caps["error"] = str(e)
        log.error("nvme_get_capabilities: %s query failed: %s", dev, e)

    return caps


def nvme_sanitize_op(dev: str, action: str, progress: dict):
    """
    Issue 'nvme sanitize -a <action>' then poll nvme sanitize-log for completion.

    action: 'start-crypto-erase' | 'start-block-erase' | 'exit-failure'

    Progress is driven by SPROG (0–65535) from sanitize-log.
    Completion is detected by SSTAT [2:0] == 1 (success) or 3 (failure).
    'exit-failure' completes immediately without polling.

    progress dict: label, percent, done, error
    """
    label_map = {
        "start-crypto-erase": "Crypto Sanitize...",
        "start-block-erase":  "Block Sanitize...",
        "exit-failure":       "Exit Failure...",
    }
    progress["label"]   = label_map.get(action, f"Sanitize {action}...")
    progress["percent"] = 0.0

    def _run_op():
        try:
            # ---- Issue the sanitize command ----
            cmd = ["sudo", "nvme", "sanitize", dev, "-a", action]
            rc, out = _run_cmd_logged(cmd, "nvme-sanitize")
            if rc != 0:
                raise DiskOpsError(f"nvme sanitize {action} failed ({rc}): {out[:80]}")
            log.info("  nvme sanitize %s issued on %s", action, dev)

            # exit-failure is instantaneous — no sanitize-log polling needed
            if action == "exit-failure":
                progress["percent"] = 100.0
                return

            # ---- Poll sanitize-log for progress ----
            time.sleep(1.5)   # give device a moment to start
            _last_logged = -10.0
            stall_count  = 0

            while True:
                time.sleep(2)
                try:
                    r = _run(["sudo", "nvme", "sanitize-log", dev])
                    log_out = r.stdout
                    stall_count = 0
                except Exception as poll_err:
                    stall_count += 1
                    log.warning("  sanitize-log poll failed (%d/10): %s",
                                stall_count, poll_err)
                    if stall_count > 10:
                        raise DiskOpsError(
                            "sanitize-log unreachable for 20 s — "
                            "device may have completed or disconnected"
                        )
                    continue

                # SPROG: 0–65535 (65535 == 100 %)
                m_sprog = re.search(r"\(SPROG\)\s*:\s*(\d+)", log_out)
                if m_sprog:
                    sprog = int(m_sprog.group(1))
                    # Cap at 99 % until SSTAT confirms completion
                    pct = min(99.0, sprog / 65535 * 100)
                    progress["percent"] = pct
                    if pct - _last_logged >= 10.0:
                        log.info("  sanitize progress: %.0f%%", pct)
                        _last_logged = pct

                # SSTAT [2:0]: 0=no-op, 1=success, 2=in-progress, 3=failed
                m_sstat = re.search(r"\(SSTAT\)\s*:\s*(0x[0-9a-fA-F]+|\d+)", log_out)
                sstat_status = 0
                if m_sstat:
                    sstat_status = int(m_sstat.group(1), 0) & 0x7

                if sstat_status == 1:
                    progress["percent"] = 100.0
                    log.info("  sanitize complete (SSTAT=success)")
                    break
                elif sstat_status == 3:
                    raise DiskOpsError("Sanitize operation failed (SSTAT=3)")
                # 0 or 2: not started yet / in progress — continue polling

        except Exception as e:
            progress["error"] = str(e)
            log.error("  nvme_sanitize_op FAILED: %s", e)
        finally:
            progress["done"] = True

    threading.Thread(target=_run_op, daemon=True).start()


def nvme_format_op(ns_dev: str, ses: int, progress: dict):
    """
    Run 'nvme format' against a namespace device (e.g. /dev/nvme0n1).

    ns_dev — namespace block device path from nvme list DevicePath
    ses    — 1 = User Data Erase, 2 = Cryptographic Erase

    Always passes -n 0xffffffff so the format is broadcast to all namespaces.
    Progress jumps 0 % → 100 % on completion (Format NVM has no status log).

    progress dict: label, percent, done, error
    """
    ses_labels = {1: "User Data Erase", 2: "Crypto Erase"}
    progress["label"]   = f"Format ({ses_labels.get(ses, f'SES={ses}')})..."
    progress["percent"] = 0.0

    def _run_fmt():
        try:
            cmd = ["sudo", "nvme", "format", ns_dev, "-s", str(ses),
                   "-n", "0xffffffff"]
            log.info("  nvme format: %s  ses=%d  all-ns=yes", ns_dev, ses)
            rc, out = _run_cmd_logged(cmd, "nvme-format")
            if rc != 0:
                raise DiskOpsError(f"nvme format failed ({rc}): {out[:80]}")
            progress["percent"] = 100.0
            log.info("  nvme format complete: %s ses=%d", ns_dev, ses)
        except Exception as e:
            progress["error"] = str(e)
            log.error("  nvme_format_op FAILED: %s", e)
        finally:
            progress["done"] = True

    threading.Thread(target=_run_fmt, daemon=True).start()


def forensic_image(src_dev: str, dst_mount: str, serial: str, progress: dict):
    """
    Create a forensic sector image of src_dev using dcfldd.

    Output files written to dst_mount:
        {serial}.dd            — raw sector image
        {serial}.sha256.log    — sha256 hash written by dcfldd (hashlog=)
        {serial}.log           — full dcfldd stderr / operation log

    progress dict: label, percent, done, error, speed
    """
    image_path = os.path.join(dst_mount, f"{serial}.dd")
    hash_log   = os.path.join(dst_mount, f"{serial}.sha256.log")
    op_log     = os.path.join(dst_mount, f"{serial}.log")

    log.info("forensic_image: %s -> %s", src_dev, image_path)
    log.info("  hash log : %s", hash_log)
    log.info("  op log   : %s", op_log)

    progress["label"] = f"Imaging {src_dev}..."
    progress["percent"] = 0.0

    def _run_dcfldd():
        try:
            total_bytes = get_disk_size(src_dev)
            log.info("  Source size: %s", _fmt_bytes(total_bytes))
        except Exception as e:
            log.warning("  Could not get source size: %s", e)
            total_bytes = 0

        try:
            cmd = [
                "sudo", "dcfldd",
                f"if={src_dev}",
                f"of={image_path}",
                "hash=sha256",
                f"hashlog={hash_log}",
                "sizeprobe=if",     # enables "X% done" lines in output
                "statusinterval=16", # print status every 16 blocks (×512 B = 8 KB per block → ~8 MB intervals with bs=512)
                "bs=4096",           # forensically standard sector size
            ]
            log.info("  Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            stderr_lines: list[str] = []
            last_state: dict = {}

            def _read_stderr():
                for line in proc.stderr:
                    line = line.rstrip()
                    if not line:
                        continue
                    log.debug("  dcfldd: %s", line)
                    stderr_lines.append(line)
                    pct, speed = _parse_dcfldd_line(line, total_bytes, last_state)
                    if pct is not None:
                        progress["percent"] = min(100.0, pct)
                    if speed:
                        progress["speed"] = speed

            stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
            stderr_thread.start()

            # dcfldd stdout is empty; drain so the pipe never blocks
            proc.stdout.read()
            proc.wait()
            stderr_thread.join(timeout=5)

            # Write the full operation log to the destination filesystem
            try:
                with open(op_log, "w") as f:
                    f.write("\n".join(stderr_lines))
                    f.write("\n")
                log.info("  op log written: %s", op_log)
            except Exception as e:
                log.warning("  Could not write op log: %s", e)

            if proc.returncode != 0:
                log.error("  dcfldd FAILED (exit %d)", proc.returncode)
                for ln in stderr_lines[-10:]:
                    log.error("    %s", ln)
                progress["error"] = f"dcfldd exit {proc.returncode}"
            else:
                progress["percent"] = 100.0
                log.info("  forensic image complete : %s", image_path)
                log.info("  sha256 hash log         : %s", hash_log)

        except Exception as e:
            progress["error"] = str(e)
            log.exception("  dcfldd exception: %s", e)
        finally:
            progress["done"] = True

    t = threading.Thread(target=_run_dcfldd, daemon=True)
    t.start()
