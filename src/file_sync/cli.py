"""Safe, additive PC-to-server storage synchronization."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

APP = "file-sync"
STATE = Path.home() / ".local/state" / APP
CONFIG = Path.home() / ".config" / APP / "config.toml"
REMOTE_STATE = ".file-sync-state"
PARTIAL = ".file-sync-partial"
REGISTRY = ".file-sync-repositories"
RESERVED = {REMOTE_STATE, PARTIAL, REGISTRY, ".git"}


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    local: Path
    remote: str
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    host: str
    user: str | None
    base_path: str
    minimum_free_gb: float
    retries: int
    ssh_options: tuple[str, ...]
    rsync_options: tuple[str, ...]
    excludes: tuple[str, ...]

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


def fail(message: str) -> None:
    raise SyncError(message)


def safe_remote_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path:
        fail(f"server parent must be a non-empty relative POSIX path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        fail(f"unsafe server parent: {path!r}")
    if any(part in RESERVED for part in parts):
        fail(f"server parent uses reserved sync path: {path!r}")
    return path


def load_config(path: Path = CONFIG) -> Config:
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError:
        fail(f"config not found: {path}; copy config.example.toml first")
    except tomllib.TOMLDecodeError as error:
        fail(f"invalid TOML in {path}: {error}")
    connection = data.get("connection", {})
    defaults = data.get("defaults", {})
    host = connection.get("host")
    if not isinstance(host, str) or not host:
        fail("[connection].host is required")
    base_path = defaults.get("base_path", "/srv/storage")
    if not isinstance(base_path, str) or not base_path.startswith("/") or base_path == "/":
        fail("[defaults].base_path must be a non-root absolute server path")
    if "folders" in data:
        fail("[folders] is unsupported; pass <client-folder> <server-parent> on each command")
    return Config(
        host=host,
        user=connection.get("user") or None,
        base_path=base_path.rstrip("/"),
        minimum_free_gb=float(defaults.get("minimum_free_gb", 5)),
        retries=max(0, int(defaults.get("retries", 2))),
        ssh_options=tuple(str(item) for item in connection.get("ssh_options", ["-o", "BatchMode=yes"])),
        rsync_options=tuple(str(item) for item in defaults.get("rsync_options", [])),
        excludes=tuple(str(item) for item in defaults.get("excludes", [])),
    )


def make_target(client_folder: str, server_parent: str, config: Config) -> Target:
    local = Path(client_folder).expanduser().resolve()
    if local == local.parent or local.name in RESERVED:
        fail(f"unsafe client folder: {client_folder!r}")
    parent = safe_remote_path(server_parent)
    remote = safe_remote_path(f"{parent}/{local.name}")
    return Target(local, remote, config.excludes + (".git/", f"{PARTIAL}/", f"{REMOTE_STATE}/", f"{REGISTRY}/"))


def remote_root(config: Config, target: Target) -> str:
    return f"{config.base_path}/{target.remote}"


def excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
    parts = relative_path.split("/")
    for pattern in patterns:
        cleaned = pattern.rstrip("/")
        if cleaned and (fnmatch.fnmatch(relative_path, cleaned) or fnmatch.fnmatch(Path(relative_path).name, cleaned)):
            return True
        if pattern.endswith("/") and cleaned and any(fnmatch.fnmatch(part, cleaned) for part in parts):
            return True
    return False


def local_manifest(root: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    if not root.exists():
        return {}
    if not root.is_dir():
        fail(f"local path is not a directory: {root}")
    manifest: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if excluded(relative, patterns) or path.is_dir():
            continue
        if "\n" in relative:
            fail(f"newline in filename unsupported for safe remote scan: {path}")
        stat = path.lstat()
        if path.is_symlink():
            manifest[relative] = f"L:{os.readlink(path)}"
        elif path.is_file():
            manifest[relative] = f"F:{stat.st_size}:{int(stat.st_mtime)}"
    return manifest


REMOTE_MANIFEST_SCRIPT = r'''set -eu
root=$1
[ -d "$root" ] || exit 0
find "$root" -mindepth 1 \( -type f -o -type l \) -print | while IFS= read -r file; do
  rel=${file#"$root"/}
  if [ -L "$file" ]; then
    printf 'L\t%s\t%s\n' "$rel" "$(readlink "$file")"
  else
    printf 'F\t%s\t%s:%s\n' "$rel" "$(stat -c '%s' "$file")" "$(stat -c '%Y' "$file")"
  fi
done
'''


def ssh_command(config: Config, script: str, *arguments: str) -> tuple[list[str], str]:
    command = "sh -s -- " + " ".join(shlex.quote(argument) for argument in arguments)
    return ["ssh", *config.ssh_options, config.target, command], script


def run_remote(config: Config, script: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command, stdin = ssh_command(config, script, *arguments)
    result = subprocess.run(command, input=stdin, text=True, capture_output=True)
    if check and result.returncode:
        fail(f"SSH command failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def run_remote_input(config: Config, script: str, input_text: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = "sh -c " + shlex.quote(script) + " sync " + " ".join(shlex.quote(argument) for argument in arguments)
    result = subprocess.run(["ssh", *config.ssh_options, config.target, command], input=input_text, text=True, capture_output=True)
    if result.returncode:
        fail(f"SSH command failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def remote_manifest(config: Config, target: Target) -> dict[str, str]:
    output = run_remote(config, REMOTE_MANIFEST_SCRIPT, remote_root(config, target)).stdout
    manifest: dict[str, str] = {}
    for line in output.splitlines():
        kind, path, value = line.split("\t", 2)
        if not excluded(path, target.excludes):
            manifest[path] = f"{kind}:{value}"
    return manifest


def state_path(config: Config, target: Target) -> str:
    digest = hashlib.sha256(remote_root(config, target).encode()).hexdigest()
    return f"{config.base_path}/{REMOTE_STATE}/{digest}.json"


def load_baseline(config: Config, target: Target) -> dict[str, str] | None:
    output = run_remote(config, 'set -eu\n[ -f "$1" ] && cat -- "$1" || true\n', state_path(config, target)).stdout
    if not output.strip():
        return None
    try:
        manifest = json.loads(output)["manifest"]
        if not isinstance(manifest, dict):
            raise TypeError("manifest is not a mapping")
        return {str(path): str(value) for path, value in manifest.items()}
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        fail(f"invalid server baseline for {target.remote}: {error}")


def save_baseline(config: Config, target: Target, manifest: dict[str, str]) -> None:
    script = '''set -eu
directory=$1
file=$2
mkdir -p -- "$directory"
umask 077
tmp=$(mktemp "$directory/.tmp.XXXXXX")
cat > "$tmp"
mv -- "$tmp" "$file"
'''
    payload = json.dumps({"destination": remote_root(config, target), "manifest": manifest}, sort_keys=True)
    run_remote_input(config, script, payload, f"{config.base_path}/{REMOTE_STATE}", state_path(config, target))


def classify(source: dict[str, str], destination: dict[str, str], baseline: dict[str, str] | None) -> tuple[list[str], list[str], int]:
    copies: list[str] = []
    conflicts: list[str] = []
    skipped = 0
    for path, source_value in source.items():
        destination_value = destination.get(path)
        if destination_value is None:
            copies.append(path)
        elif source_value == destination_value:
            skipped += 1
        elif baseline is None:
            conflicts.append(path)
        elif source_value != baseline.get(path) and destination_value == baseline.get(path):
            copies.append(path)
        else:
            conflicts.append(path)
    return copies, conflicts, skipped


def estimate_bytes(source: dict[str, str], paths: list[str]) -> int:
    return sum(int(source[path].split(":", 2)[1]) for path in paths if source[path].startswith("F:"))


def format_size(byte_count: int) -> str:
    for divisor, unit in ((1_000_000_000, "GB"), (1_000_000, "MB"), (1_000, "KB")):
        if byte_count >= divisor:
            return f"{byte_count / divisor:.2f} {unit}"
    return f"{byte_count} bytes"


def free_bytes_remote(config: Config) -> int:
    output = run_remote(config, "set -eu\ndf -Pk -- \"$1\" | awk 'NR == 2 { print $4 * 1024 }'\n", config.base_path).stdout.strip()
    try:
        return int(float(output))
    except ValueError:
        fail(f"cannot read free space on server: {output!r}")


def require_space(available: int, needed: int, config: Config, where: str) -> None:
    reserve = int(config.minimum_free_gb * 1024**3)
    if available - needed < reserve:
        fail(f"insufficient free space on {where}: available={available}, transfer={needed}, reserve={reserve} bytes")


def rsync_command(config: Config, target: Target, direction: str, dry_run: bool) -> list[str]:
    remote = f"{config.target}:{shlex.quote(remote_root(config, target))}/"
    ssh = shlex.join(["ssh", *config.ssh_options])
    command = ["rsync", "--archive", f"--partial-dir={PARTIAL}", "--delay-updates", "--itemize-changes", "--stats", "--protect-args", "-e", ssh]
    command.extend(config.rsync_options)
    command.extend(f"--exclude={pattern}" for pattern in target.excludes)
    if dry_run:
        command.append("--dry-run")
    command.extend([str(target.local) + "/", remote] if direction == "push" else [remote, str(target.local) + "/"])
    return command


def log_operation(name: str, body: str) -> Path:
    directory = STATE / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{name}.log"
    path.write_text(body, encoding="utf-8")
    return path


def report_state(direction: str, state: str) -> None:
    print(f"{direction}: {state}", file=sys.stderr)


def run_rsync(command: list[str], direction: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not sys.stderr.isatty():
        report_state(direction, "syncing")
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    frames = "|/-\\"
    frame = 0
    while process.poll() is None:
        sys.stderr.write(f"\r{direction}: syncing {frames[frame % len(frames)]}")
        sys.stderr.flush()
        frame += 1
        time.sleep(0.15)
    stdout, stderr = process.communicate()
    sys.stderr.write("\r" + " " * (len(direction) + 11) + "\r")
    sys.stderr.flush()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def local_repository(target: Target) -> tuple[Path, str, str, str] | None:
    result = subprocess.run(["git", "-C", str(target.local), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if result.returncode:
        return None
    root = Path(result.stdout.strip()).resolve()
    relative = target.local.relative_to(root)
    suffix = 0 if relative == Path(".") else len(relative.parts)
    parts = target.remote.split("/")
    if suffix and len(parts) <= suffix:
        fail(f"cannot derive server repository root from {target.remote}")
    repository = "/".join(parts[:-suffix] if suffix else parts)
    origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], text=True, capture_output=True)
    if origin.returncode or not origin.stdout.strip():
        fail(f"local Git repository has no origin: {root}")
    branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"], text=True, capture_output=True)
    if branch.returncode or not branch.stdout.strip():
        fail(f"local Git repository is detached: {root}")
    return root, repository, origin.stdout.strip(), branch.stdout.strip()


REPOSITORY_SCRIPT = r'''set -eu
base=$1 repo_relative=$2 origin=$3 branch=$4
repo=$base/$repo_relative
registry=$base/.file-sync-state/repositories
fallback=$base/.file-sync-state/unmanaged-repositories
created=0
case $repo in "$base"/*) ;; *) echo "unsafe repository path" >&2; exit 2 ;; esac
if [ -e "$repo" ] && [ ! -d "$repo/.git" ]; then
  mkdir -p -- "$(dirname -- "$fallback")"
  touch "$fallback"
  grep -Fqx -- "$repo" "$fallback" || printf '%s\n' "$repo" >> "$fallback"
  printf '%s\n' fallback
  exit 0
fi
if [ ! -e "$repo" ]; then
  parent=$(dirname -- "$repo")
  mkdir -p -- "$parent"
  temporary=$(mktemp -d "$parent/.file-sync-clone.XXXXXX")
  trap 'rm -rf -- "$temporary"' EXIT
  if git clone --branch "$branch" --single-branch -- "$origin" "$temporary/repository"; then
    mv -- "$temporary/repository" "$repo"
    created=1
  else
    mkdir -p -- "$(dirname -- "$fallback")"
    touch "$fallback"
    grep -Fqx -- "$repo" "$fallback" || printf '%s\n' "$repo" >> "$fallback"
    printf '%s\n' fallback
    exit 0
  fi
fi
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null
git -C "$repo" config user.name "Storage Backup"
git -C "$repo" config user.email "$(id -un)@$(hostname)"
mkdir -p -- "$(dirname -- "$registry")"
touch "$registry"
grep -Fqx -- "$repo" "$registry" || printf '%s\n' "$repo" >> "$registry"
printf '%s\n' "$created"
'''


def prepare_server_repository(config: Config, target: Target) -> bool:
    repository = local_repository(target)
    if repository is None:
        return False
    _, remote_relative, origin, branch = repository
    result = run_remote(config, REPOSITORY_SCRIPT, config.base_path, remote_relative, origin, branch, check=False)
    if result.returncode:
        fail(f"Server Git setup failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    if result.stdout.strip() == "fallback":
        print("file-sync: warning: Server Git clone unavailable; syncing data without Server Git backup", file=sys.stderr)
        return False
    return result.stdout.strip() == "1"


def ensure_remote_directory(config: Config, target: Target) -> None:
    run_remote(config, 'set -eu\nmkdir -p -- "$1"\n', remote_root(config, target))


def sync_folder(config: Config, target: Target, direction: str, dry_run: bool) -> int:
    started = time.monotonic()
    if direction == "push" and not target.local.is_dir():
        fail(f"local source directory missing: {target.local}")
    if direction == "pull" and target.local.exists() and not target.local.is_dir():
        fail(f"local destination is not a directory: {target.local}")
    if direction == "push" and not dry_run:
        report_state(direction, "preparing server repository")
        cloned = prepare_server_repository(config, target)
        ensure_remote_directory(config, target)
    else:
        cloned = False
    if direction == "pull" and not target.local.exists() and not dry_run:
        target.local.mkdir(parents=True)
    report_state(direction, "scanning files")
    local, remote = local_manifest(target.local, target.excludes), remote_manifest(config, target)
    baseline = load_baseline(config, target)
    if cloned and baseline is None:
        baseline = remote
    source, destination = (local, remote) if direction == "push" else (remote, local)
    copies, conflicts, skipped = classify(source, destination, baseline)
    transfer = estimate_bytes(source, copies)
    report_state(direction, "checking conflicts and free space")
    pc_parent = target.local.parent if target.local.parent.exists() else Path.home()
    available = shutil.disk_usage(pc_parent).free if direction == "pull" else free_bytes_remote(config)
    require_space(available, transfer, config, "PC" if direction == "pull" else "server")
    summary = f"{direction} {target.remote}: copy/update={len(copies)} skip={skipped} conflicts={len(conflicts)} delete=0 estimate={format_size(transfer)} ({transfer} bytes)"
    if conflicts:
        detail = "\n".join(conflicts[:50])
        log_operation(f"{direction}-{hashlib.sha256(target.remote.encode()).hexdigest()[:12]}", summary + "\nCONFLICTS:\n" + detail)
        fail(f"{summary}\nconflicts require manual resolution:\n{detail}")
    if dry_run:
        print(summary)
        return 0
    command = rsync_command(config, target, direction, False)
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(config.retries + 1):
        result = run_rsync(command, direction)
        if result.returncode == 0:
            break
        if attempt < config.retries:
            report_state(direction, f"retrying in {2**attempt}s")
            time.sleep(2**attempt)
    assert result is not None
    elapsed = time.monotonic() - started
    log_path = log_operation(f"{direction}-{hashlib.sha256(target.remote.encode()).hexdigest()[:12]}", f"{summary}\nduration={elapsed:.1f}s\nexit={result.returncode}\ncommand={shlex.join(command)}\n\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}")
    if result.returncode:
        fail(f"rsync failed after {config.retries + 1} attempt(s); log: {log_path}")
    report_state(direction, "verifying transfer")
    remote_after = remote_manifest(config, target)
    if direction == "push":
        missing = [path for path in copies if remote_after.get(path) != local.get(path)]
        if missing:
            fail("post-push verification failed: " + ", ".join(missing[:20]))
    report_state(direction, "saving conflict baseline")
    save_baseline(config, target, remote_after)
    print(f"{summary}\ncompleted in {elapsed:.1f}s; log: {log_path}")
    return 0


def verify(config: Config, target: Target, checksum: bool) -> int:
    if not target.local.is_dir():
        fail(f"local directory missing: {target.local}")
    if checksum:
        command = rsync_command(config, target, "push", True)
        command.insert(2, "--checksum")
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            fail(f"checksum verification failed to run: {result.stderr.strip()}")
        changes = [line for line in result.stdout.splitlines() if line[:1] in {">", "<", "c", "."}]
        if changes:
            fail("checksum verification found differences:\n" + "\n".join(changes[:50]))
        print("checksum verification passed")
    else:
        local, remote = local_manifest(target.local, target.excludes), remote_manifest(config, target)
        missing = [path for path in local if path not in remote]
        if missing:
            fail("server missing local paths:\n" + "\n".join(missing[:50]))
        print(f"existence verification passed: {len(local)} local paths exist on server")
    return 0


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(prog=APP, description="Safe additive PC-to-server sync")
    argument_parser.add_argument("--config", type=Path, default=CONFIG)
    subcommands = argument_parser.add_subparsers(dest="command", required=True)
    status = subcommands.add_parser("status")
    status.add_argument("paths", nargs="*")
    for name in ("push", "pull"):
        command = subcommands.add_parser(name)
        command.add_argument("client_folder")
        command.add_argument("server_parent")
        command.add_argument("--dry-run", action="store_true")
    check = subcommands.add_parser("verify")
    check.add_argument("client_folder")
    check.add_argument("server_parent")
    check.add_argument("--checksum", action="store_true")
    config = subcommands.add_parser("config")
    config.add_argument("action", choices=["check"])
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "config":
            print(f"config valid: {args.config}; base_path={config.base_path}")
            return 0
        if args.command == "status":
            if not args.paths:
                print(f"server={config.target} base_path={config.base_path}")
                return 0
            if len(args.paths) != 2:
                fail("status requires both <client-folder> and <server-parent>")
            target = make_target(args.paths[0], args.paths[1], config)
            local, remote = local_manifest(target.local, target.excludes), remote_manifest(config, target)
            baseline = load_baseline(config, target)
            _, push_conflicts, _ = classify(local, remote, baseline)
            _, pull_conflicts, _ = classify(remote, local, baseline)
            print(f"{target.remote}: local={len(local)} remote={len(remote)} baseline={'yes' if baseline else 'no'} conflicts={len(set(push_conflicts + pull_conflicts))}")
            return 0
        target = make_target(args.client_folder, args.server_parent, config)
        if args.command in {"push", "pull"}:
            return sync_folder(config, target, args.command, args.dry_run)
        return verify(config, target, args.checksum)
    except SyncError as error:
        print(f"{APP}: error: {error}", file=sys.stderr)
        return 2
