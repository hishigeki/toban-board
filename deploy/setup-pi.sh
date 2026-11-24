#!/bin/bash
# Raspberry Pi OS Lite (Bookworm) 上で root として実行:  sudo bash deploy/setup-pi.sh
# 先に、このフォルダ一式を /opt/toban に置いておくこと。
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "root で実行してください"; exit 1; }

apt-get update
apt-get install -y python3-venv
id toban &>/dev/null || useradd --system --home /opt/toban --shell /usr/sbin/nologin toban
python3 -m venv /opt/toban/venv
/opt/toban/venv/bin/pip install --no-cache-dir -r /opt/toban/requirements.txt

mkdir -p /etc/toban
[ -f /etc/toban/config.toml ] || cp /opt/toban/config.example.toml /etc/toban/config.toml
chown -R root:toban /etc/toban && chmod 750 /etc/toban
chmod 640 /etc/toban/* 2>/dev/null || true

# --- SDカードへの書き込みを減らす ---
# ジャーナルをメモリのみに
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' > /etc/systemd/journald.conf.d/volatile.conf
# スワップを無効化
systemctl disable --now dphys-swapfile 2>/dev/null || true
# /tmp /var/tmp /var/log を tmpfs に
grep -q 'TOBAN-TMPFS' /etc/fstab || cat >> /etc/fstab <<'FSTAB'
# TOBAN-TMPFS
tmpfs /tmp     tmpfs defaults,noatime,nosuid,size=32m 0 0
tmpfs /var/tmp tmpfs defaults,noatime,nosuid,size=16m 0 0
tmpfs /var/log tmpfs defaults,noatime,nosuid,size=16m 0 0
FSTAB
# ルートに noatime
sed -i -E '/ \/ +ext4/ s/defaults(,noatime)?/defaults,noatime/' /etc/fstab

cp /opt/toban/deploy/toban.service /etc/systemd/system/toban.service
systemctl daemon-reload
systemctl enable toban
echo
echo "完了。次に /etc/toban/config.toml と service-account.json を置き、"
echo "  sudo systemctl start toban"
echo "で起動してください。動作確認後、READMEの「読み取り専用化」を実施します。"
