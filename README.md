# Pi USB Disk Manager

Turn your Raspberry Pi into a disk management appliance, from viewing files to wiping disks to cloning one disk onto another. Tool was largely written via agentic engineering workflow (aka LLM/AI); functionalities were individually verified. 


## Features

| Operation | Tool | Description |
|---|---|---|
| Browse Disk | mount | Navigate a USB drive's filesystem to preview if it has files |
| Format Disk | parted + mkfs | MBR partition table + FAT32 / exFAT / NTFS |
| Wipe Disk | dd | Overwrite entire disk with zeros |
| Secure Erase (NVMe) | nvme-cli | Sanitize or Format NVM (crypto / block / user data) |
| Copy Files | rsync | File-level copy between two mounted partitions |
| Clone Disk | dd | Sector-by-sector disk clone |
| RAW/dd Image (dcfldd) | dcfldd | Raw `.dd` image + SHA-256 hash log |
| Forensic Image (E01) | ewfacquire | EnCase E01 image with embedded MD5 + SHA-256 |
| VHDX Image | qemu-img | Dynamic VHDX image (Sparse Disk) |
| PiShrink (dd ONLY) | pishrink.sh | Copy a `.dd` image to `.img` and shrink it to its minimum size |
| Shutdown | shutdown | Safe OS shutdown with confirmation |

All destructive operations require double confirmation. Long-pressing button A (5 seconds) from anywhere returns to the main menu. Forensics image ONLY meants images are stored in know forensics image formats; this project does NOT protect or writeblock your source disk in any way. This project is NOT a replacement for forensics imager validation.

## Hardware Requirements

Raspberry Pi Zero or above
- Pi Zero is compatible with USB HAT or USB extension
- Using a Pi 4 will be fastest as it has USB 3.0 ports and you can skip the USB HAT

Adafruit Bonnet OLED for Pi https://www.adafruit.com/product/3531\
microSD for Raspian OS (8GB+)\
Power Supply for your Pi

## Button Layout

| Button | BCM Pin | Function |
|---|---|---|
| UP | 17 | Navigate up |
| DOWN | 22 | Navigate down |
| LEFT | 27 | Back / cancel |
| RIGHT | 23 | Enter / confirm |
| SELECT | 4 | Confirm |
| A | 5 | Back / cancel; hold 5 s to return to main menu |
| B | 6 | Context action / confirm |

## Installation

Install Raspian on your raspberry pi. Name your user `pi`.

Clone or copy the project files to your Raspberry Pi, then run the install script:

```bash
git clone https://github.com/alvin-000/pi_usb_disk_manager.git /home/pi/install_files
cd /home/pi/install_files
bash install.sh
```

The script will:

1. Enable I2C and set the bus speed to 1 MHz
2. Install all system packages (`parted`, `rsync`, `dcfldd`, `ewf-tools`, `nvme-cli`, `qemu-utils`, etc.)
3. Create a Python virtual environment and install pip dependencies
4. Configure passwordless `sudo` for disk operations
5. Install and enable the `usb_imager` systemd service

> A reboot is required after installation for the I2C changes to take effect.

### Manual verification

```bash
# Check OLED is detected on I2C bus 1
i2cdetect -y 1        # should show a device at 0x3C

# Run manually to test before enabling the service
cd /home/pi/usb_imager
.venv/bin/python main.py

# Start the service
sudo systemctl start usb_imager

# Follow logs
journalctl -u usb_imager -f
```

## Project Structure

```
usb_imager/
├── main.py          # Entry point, event loop, all operation flows
├── config.py        # GPIO pins, display constants, UI settings
├── display.py       # SSD1306 OLED driver wrapper (Pillow rendering)
├── buttons.py       # GPIO button handler with debounce and hold detection
├── menu.py          # Hierarchical menu engine (stack-based navigation)
├── file_browser.py  # Scrollable file/directory browser
├── disk_ops.py      # All disk operations (mount, copy, clone, image, wipe, NVMe)
├── progress.py      # Progress bar screen (polls shared progress dict)
├── requirements.txt # Python pip dependencies
├── install.sh       # One-shot setup script
└── usb_imager.service  # systemd unit file
```

## Python Dependencies

Installed into a virtualenv via `requirements.txt`:

- `adafruit-circuitpython-ssd1306` — SSD1306 OLED driver
- `adafruit-blinka` — CircuitPython compatibility layer for Linux
- `gpiozero` — GPIO button abstraction with debounce
- `RPi.GPIO` — GPIO backend for gpiozero on Raspberry Pi
- `Pillow` — image rendering (installed via `apt` as `python3-pil`)

## Configuration

All hardware and UI settings are in `config.py`. Key values:

| Setting | Default | Description |
|---|---|---|
| `I2C_ADDRESS` | `0x3C` | OLED I2C address |
| `MOUNT_BASE` | `/mnt/usb_imager` | Base directory for mount points |
| `EXCLUDED_PREFIXES` | `mmcblk`, `zram` | Devices hidden from all menus (protects Pi SD card) |
| `DEBOUNCE_MS` | `0.05 s` | Button debounce time |
| `A_HOLD_TIME` | `5.0 s` | Hold duration to trigger return-to-main-menu |

## NVMe Secure Erase Notes

The NVMe flow queries device capabilities via `nvme id-ctrl` before presenting options, so only methods actually supported by the connected drive are shown:

| Method | Command | Description |
|---|---|---|
| Cryptographic | `nvme sanitize` | Crypto erase (sanicap bit 2) |
| Block | `nvme sanitize` | Block erase (sanicap bit 1) |
| Exit Failure | `nvme sanitize` | Exit a failed sanitize operation |
| Crypto (Legacy) | `nvme format` | Crypto erase via Format NVM (fna bit 2) |
| User Data (Legacy) | `nvme format` | User data erase via Format NVM (fna bit 0) |

Sanitize operations show live progress polled from `nvme sanitize-log` (SPROG / SSTAT fields).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
