#!/bin/bash
# Install / remove the R1 assistant as background systemd services.
#   sudo bash service/install_service.sh            # install + enable at boot + start now
#   sudo bash service/install_service.sh --no-start # install + enable, start on next boot
#   sudo bash service/install_service.sh --remove   # stop, disable and delete the units
# Logs:   journalctl -u r1-assistant -f        (assistant)
#         tail -f /home/unitree/*.log          (riva_server, llama-server, daemons)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR=/etc/systemd/system

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash $0 $*"; exit 1
fi

if [ "$1" = "--remove" ]; then
  systemctl stop r1-assistant.service r1-services.service 2>/dev/null || true
  systemctl disable r1-assistant.service r1-services.service 2>/dev/null || true
  rm -f "$UNIT_DIR/r1-assistant.service" "$UNIT_DIR/r1-services.service"
  systemctl daemon-reload
  echo "Removed. Run the assistant manually with: bash /home/unitree/app.sh"
  exit 0
fi

install -m 644 "$HERE/r1-services.service" "$UNIT_DIR/r1-services.service"
install -m 644 "$HERE/r1-assistant.service" "$UNIT_DIR/r1-assistant.service"
systemctl daemon-reload
systemctl enable r1-services.service r1-assistant.service
# So manual runs (python3 test_vision_voice_assistant.py --tap) can also read the clicker; the service
# itself gets the group via SupplementaryGroups. Takes effect at the next login.
usermod -aG input unitree || true

if [ "$1" != "--no-start" ]; then
  # A manually started assistant would fight over the mic/speaker
  pkill -f "[t]est_vision_voice_assistant.py" || true
  systemctl start r1-services.service
  systemctl start r1-assistant.service
  systemctl --no-pager status r1-assistant.service | head -5
fi
echo "Installed. Follow the assistant with: journalctl -u r1-assistant -f"
