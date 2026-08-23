#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
usage() {
  cat <<'USAGE'
Usage:
  ./install.sh client
  sudo ./install.sh server
USAGE
}

install_client() {
  command -v python3 >/dev/null || { echo "python3 missing" >&2; exit 1; }
  command -v rsync >/dev/null || { echo "rsync missing" >&2; exit 1; }
  command -v ssh >/dev/null || { echo "OpenSSH missing" >&2; exit 1; }
  mkdir -p "$HOME/.local/bin" "$HOME/.config/file-sync"
  ln -sfn "$root/sync" "$HOME/.local/bin/file-sync"
  if [ ! -e "$HOME/.config/file-sync/config.toml" ]; then
    cp "$root/config.example.toml" "$HOME/.config/file-sync/config.toml"
    chmod 600 "$HOME/.config/file-sync/config.toml"
    echo "Created ~/.config/file-sync/config.toml; edit connection settings."
  fi
  echo "Installed ~/.local/bin/file-sync"
}

install_server() {
  local user=thaodlq storage_root=/mnt/hdd/fw/thaodlq2/storage group key
  id "$user" >/dev/null
  [[ "$storage_root" == /* && "$storage_root" != "/" && "$storage_root" != *$'\n'* ]] || { echo "STORAGE_ROOT must be a safe absolute directory" >&2; exit 2; }
  group=$(id -gn "$user")
  command -v git >/dev/null || { echo "git missing" >&2; exit 1; }
  command -v rsync >/dev/null || { echo "rsync missing" >&2; exit 1; }
  command -v ssh-keygen >/dev/null || { echo "ssh-keygen missing" >&2; exit 1; }
  command -v timedatectl >/dev/null || { echo "systemd/timedatectl missing" >&2; exit 1; }
  install -d -o "$user" -g "$group" -m 0750 "$storage_root"
  install -d -o "$user" -g "$group" -m 0700 "$storage_root/.file-sync-state"
  install -d -o root -g root -m 0755 /usr/local/lib/file-sync-backup
  install -m 0755 "$root/server/backup.sh" /usr/local/lib/file-sync-backup/backup.sh
  sed -e "s/__STORAGE_USER__/$user/g" -e "s/__STORAGE_GROUP__/$group/g" "$root/server/storage-backup.service" > /etc/systemd/system/storage-backup.service
  install -m 0644 "$root/server/storage-backup.timer" /etc/systemd/system/storage-backup.timer
  cat > /etc/file-sync-backup.conf <<CONFIG
STORAGE_ROOT=$storage_root
PUSH_RETRIES=2
CONFIG
  chmod 600 /etc/file-sync-backup.conf
  key="/home/$user/.ssh/file-sync-backup_ed25519"
  if [ ! -f "$key" ]; then
    sudo -u "$user" ssh-keygen -q -t ed25519 -N '' -f "$key"
  fi
  systemctl daemon-reload
  echo "Deploy public key; grant write access to both git.fpt.net repositories before enabling backups:"
  cat "$key.pub"
  echo "Storage root: $storage_root"
  echo "Storage owner: $(stat -c '%U:%G %a' "$storage_root")"
  echo "Then run: sudo systemctl enable --now storage-backup.timer"
}

case ${1:-} in
  client) install_client ;;
  server) [ "$(id -u)" -eq 0 ] || { echo "run server setup via sudo" >&2; exit 1; }; [ "$#" -eq 1 ] || { usage >&2; exit 2; }; install_server ;;
  *) usage >&2; exit 2 ;;
esac
