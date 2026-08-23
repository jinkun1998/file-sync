# Safe PC ↔ Server Sync

`file-sync` is additive `rsync` over SSH. Push and pull never pass `--delete`.

## Client

```sh
cd file-sync
./install.sh client
$EDITOR ~/.config/file-sync/config.toml
file-sync config check
file-sync push models/anyka/2026_model models/anyka --dry-run
file-sync push models/anyka/2026_model models/anyka
file-sync verify models/anyka/2026_model models/anyka --checksum
```

Commands use the current directory for relative client paths. The server parent is always relative to `[defaults].base_path`; the client basename is retained:

```text
file-sync push models/anyka/2026_model models/anyka
/mnt/hdd/fw/thaodlq2/storage/models/anyka/2026_model
```

```text
file-sync push <client-folder> <server-parent> [--dry-run]
file-sync pull <client-folder> <server-parent> [--dry-run]
file-sync verify <client-folder> <server-parent> [--checksum]
file-sync status [<client-folder> <server-parent>]
file-sync config check
```

`[folders.*]` is unsupported. TOML holds connection, server base path, exclusions, retries, free-space reserve, and `rsync` options only.

## Safety

- Server traversal, `.git`, sync state, partial, registry paths rejected.
- `.git/` always excluded from transfer. Local and server Git metadata remain independent.
- Existing same-path differences abort. First sync requires an empty destination unless a Server Git clone was just created.
- Baselines use the full server destination. Pull and push are additive.
- Logs: `~/.local/state/file-sync/logs/`.

## Server repositories

On a push from a local Git worktree, `file-sync` derives its root and matching Server root. For example, a local `models/anyka/2026_model` worktree rooted at `models/anyka` maps to Server repository `models/anyka`.

If that Server root is absent, it clones the local `origin` active branch before syncing. An existing non-Git directory fails safely. Each discovered repository is registered once, in first-push order, for scheduled backup.

## Server setup

Run as root on the Server. Setup targets account `thaodlq` and `/mnt/hdd/fw/thaodlq2/storage`; it creates `/home/thaodlq/.ssh/file-sync-backup_ed25519` when absent and prints its public key.

```sh
cd file-sync
sudo ./install.sh server
```

Grant that public key write access in `git.fpt.net` to both repositories before enabling the timer:

```text
git@git.fpt.net:iot/iot-internal/CDC/platforms/anyka/anyka-cdc-model.git
git@git.fpt.net:iot/iot-internal/CDC/platforms/sigmastar/sigmastar-cdc-model.git
```

Then enable backups:

```sh
sudo systemctl enable --now storage-backup.timer
systemctl list-timers storage-backup.timer
journalctl -u storage-backup.service
```

The monotonic timer runs every `18h`. Each registered repository runs independently: `git add -A`, skip unchanged, timestamped commit, push its own `origin` and active branch. A failed repository does not stop later repositories; the service returns failure after processing all failures.

Normal Git is unsuitable for large or high-churn binaries. Keep separate recovery storage for those files.
