#!/bin/bash
# install.sh — One-shot setup script for USB Imager on Raspberry Pi (Raspbian Trixie)
set -e

INSTALL_DIR="/home/pi/usb_imager"
SERVICE_USER="pi"

echo "==> Enabling I2C interface..."
sudo raspi-config nonint do_i2c 0

echo "==> Setting I2C bus speed to 1 MHz..."
CONFIG_FILE="/boot/firmware/config.txt"
if ! grep -q "i2c_baudrate=1000000" "$CONFIG_FILE"; then
    echo "dtparam=i2c_baudrate=1000000" | sudo tee -a "$CONFIG_FILE"
fi

echo "==> Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y \
    python3-venv \
    python3-dev \
    python3-pil \
    build-essential \
    parted \
    exfatprogs \
    dosfstools \
    rsync \
    dcfldd \
    i2c-tools \
    ntfs-3g \
    ewf-tools \
    nvme-cli \
    qemu-utils

echo "==> Creating install directory..."
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Copying project files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in main.py config.py display.py buttons.py menu.py disk_ops.py file_browser.py progress.py requirements.txt; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    fi
done

echo "==> Creating Python virtual environment..."
# --system-site-packages lets the venv use python3-pil from apt (avoids
# building Pillow from source, which fails without extra build deps)
python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$INSTALL_DIR/requirements.txt"
deactivate

echo "==> Configuring sudoers for disk operations..."
SUDOERS_FILE="/etc/sudoers.d/usb_imager"
sudo tee "$SUDOERS_FILE" > /dev/null <<'EOF'
# Allow pi user to run disk management commands without password
pi ALL=(ALL) NOPASSWD: /bin/mount, /bin/umount, /sbin/blockdev, /bin/dd, /sbin/mkfs.fat, /sbin/mkfs.exfat, /usr/bin/rsync, /sbin/parted, /usr/sbin/parted, /bin/udevadm, /usr/bin/udevadm, /sbin/shutdown, /usr/sbin/shutdown, /usr/bin/dcfldd, /usr/bin/ewfacquire, /usr/sbin/nvme, /usr/bin/qemu-img
EOF
sudo chmod 440 "$SUDOERS_FILE"
sudo visudo -c  # Validate sudoers syntax

echo "==> Installing systemd service..."
sudo cp "$SCRIPT_DIR/usb_imager.service" /etc/systemd/system/usb_imager.service
sudo systemctl daemon-reload
sudo systemctl enable usb_imager

echo ""
echo "==> Installation complete!"
echo ""
echo "Verification steps:"
echo "  1. i2cdetect -y 1          — should show device at 0x3C"
echo "  2. cd $INSTALL_DIR && .venv/bin/python main.py   — manual test"
echo "  3. sudo systemctl start usb_imager"
echo "  4. journalctl -u usb_imager -f"
echo ""
echo "NOTE: Reboot required for I2C changes to take effect."
