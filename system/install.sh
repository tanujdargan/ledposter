#!/bin/bash
# Sets up a fresh Raspberry Pi (Raspberry Pi OS Lite 64-bit, Pi 3B) as the LED wall, from a
# checkout of this repo (milestone 8). Idempotent: run it again after pulling to re-apply
# system files or rebuild. Usage (README: Install):
#
#   git clone --recurse-submodules git@github.com:tanujdargan/ledposter.git && cd ledposter
#   sudo ./system/install.sh [--prebuilt TARBALL] [--ssh-key "ssh-ed25519 AAAA..."]
#                            [--ssh key-only|disabled] [--hostname ledposter] [--no-overlay]
#                            [--data-dir PATH] [--force-build] [--dry-run]
#
# --prebuilt (recommended; tools/deploy.sh runs all of it from your computer) unpacks a release
# made by tools/build-release.sh, checked against TARBALL.sha256 when that file is next to it:
# nothing is compiled on the Pi and the build packages are not installed. Without it the Pi
# builds the engine and the venv itself (slow, heavy on a Pi 3B: the fallback).
#
# What it does, in order: apt packages, the service user ledwall and the build user
# ledwall-build, the /data partition (or --data-dir), the release under /data/app (unpacked,
# or built as ledwall-build; owned by root), the data files (config, setup password), system
# files (units, polkit, avahi, dnsmasq, journald, NetworkManager, apt, sysctl, modprobe),
# persistence of Wi-Fi profiles across the overlay, hostname, boot config (audio off,
# isolcpus=3, performance governor, I2C), services, firewall, SSH mode, and last the read-only
# overlay (needs a reboot). --dry-run prints every action without doing it. Never locks you
# out: SSH is left as it is until the end and the resulting mode is printed; over SSH without
# a key it refuses unless --ssh is given. The login user you run it as is never changed.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DATA=/data
DATA_DIR_OPT=
WANT_HOSTNAME=ledposter
SSH_KEY=
SSH_MODE=
NO_OVERLAY=0
FORCE_BUILD=0
DRY=0
PREBUILT=
# The unprivileged accounts. Not "ledposter": that is a likely name for the Pi's login user (in
# group sudo), and these names get the polkit grants and own the app's files (docs/DEVIATIONS.md).
SVC_USER=ledwall
BUILD_USER=ledwall-build  # compiles and pip-installs releases; owns nothing that root runs
# RUNTIME_PACKAGES, RUNTIME_LIBS, BUILD_PACKAGES, OPTIONAL_PACKAGES: the one apt list, shared
# with tools/build-release.sh.
# shellcheck source-path=SCRIPTDIR source=packages.sh disable=SC1091
. "$SRC/system/packages.sh"
# Not needed on a headless wall; each one costs boot time or writes to the card.
DISABLE_UNITS="bluetooth.service hciuart.service triggerhappy.service ModemManager.service apt-daily.timer
  apt-daily-upgrade.timer man-db.timer dphys-swapfile.service e2scrub_all.timer ledposter-wifi.timer"
ENABLE_UNITS="ledposter-boot.service ledposter-engine.service ledposter-web.service ledposter-firewall.service
  ledposter-ssh.service ledposter-update.service ledposter-maintenance.timer ledposter-maintenance-resume.service
  ledposter-net-rollback.service ledposter-wifi.service avahi-daemon.service NetworkManager.service"

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prebuilt) PREBUILT="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --ssh) SSH_MODE="$2"; shift 2 ;;
    --hostname) WANT_HOSTNAME="$2"; shift 2 ;;
    --no-overlay) NO_OVERLAY=1; shift ;;
    --data-dir) DATA="$2"; DATA_DIR_OPT=1; shift 2 ;;
    --force-build) FORCE_BUILD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done
case "$SSH_MODE" in "" | key-only | disabled) ;; *) echo "--ssh must be key-only or disabled" >&2; exit 2 ;; esac

# -- helpers --------------------------------------------------------------------------

log() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
run() {
  if [ "$DRY" = 1 ]; then printf '    + %s\n' "$*"; else "$@"; fi
}
# write MODE PATH < content
write() {
  local mode="$1" path="$2" content
  content="$(cat)"
  if [ "$DRY" = 1 ]; then
    printf '    + write %s (mode %s)\n' "$path" "$mode"
    printf '%s\n' "$content" | sed 's/^/      | /'
    return
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" > "$path.tmp"
  chmod "$mode" "$path.tmp"
  mv -f "$path.tmp" "$path"
}
inst() { run install -D -m "$1" "$2" "$3"; }  # mode src dst
# A directory with a fixed owner and mode; never follows a symlink that may be in the way.
owned_dir() {  # owner mode path
  if [ -L "$3" ]; then die "$3 is a symlink; refusing to touch it"; fi
  run mkdir -p "$3"
  run chown "$1" "$3"
  run chmod "$2" "$3"
}
# The on-Pi build (no --prebuilt): one compile job, lowest CPU priority, no pip cache, so a Pi 3B
# on a weak supply is not pushed out of memory (same limits as web/ledposter/updater.py).
as_build() { run runuser -u "$BUILD_USER" -- env HOME="$DATA/app/build" MAKEFLAGS=-j1 LANG=C.UTF-8 nice -n 19 "$@"; }
have() { command -v "$1" > /dev/null 2>&1; }
sha256_of() { if have sha256sum; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
have_systemd() { [ -d /run/systemd/system ]; }
is_pi() { [ -f /boot/firmware/config.txt ] && have raspi-config; }
in_file() { [ -f "$2" ] && grep -qF -- "$1" "$2"; }
# Units are written for /data; --data-dir rewrites the paths.
install_unit() {
  local dst
  dst="/etc/systemd/system/$(basename "$1")"
  if [ "$DATA" = /data ]; then
    inst 644 "$1" "$dst"
  elif [ "$DRY" = 1 ]; then
    note "+ install $1 -> $dst with /data replaced by $DATA"
  else
    sed "s#/data#$DATA#g" "$1" > "$dst" && chmod 644 "$dst"
  fi
}
# The system files come from this checkout and the release from the --prebuilt tarball: they
# must be the same version.
same_version() {
  local v
  v="$(checkout_version)"
  [ "$PREBUILT_VERSION" = "$v" ] || die "$PREBUILT is release $PREBUILT_VERSION but this checkout is $v.
  Check out $PREBUILT_VERSION here (tools/deploy.sh sends a matching checkout), or build the tarball for this checkout."
}

# -- preflight ------------------------------------------------------------------------

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then die "run as root: sudo $0"; fi
if [ ! -d "$SRC/engine" ] || [ ! -d "$SRC/web" ]; then die "$SRC does not look like the ledposter checkout"; fi
LOGIN_USER="${SUDO_USER:-}"
LOGIN_HOME="$(getent passwd "${LOGIN_USER:-root}" 2> /dev/null | cut -d: -f6 || true)"
EXISTING_KEYS="${LOGIN_HOME:-/root}/.ssh/authorized_keys"
if [ -z "$SSH_MODE" ]; then
  if [ -n "$SSH_KEY" ] || [ -s "$EXISTING_KEYS" ]; then SSH_MODE=key-only; else SSH_MODE=disabled; fi
fi
if [ -n "${SSH_CONNECTION:-}" ] && [ "$SSH_MODE" = disabled ] && [ "$DRY" = 0 ]; then
  die "you are connected over SSH and no key is set up: after a reboot SSH would be off and you would need a keyboard.
  Pass --ssh-key 'ssh-ed25519 AAAA... you@laptop' to keep access (recommended), or --ssh disabled to confirm."
fi
if [ "$SSH_MODE" = key-only ] && [ -z "$SSH_KEY" ] && [ ! -s "$EXISTING_KEYS" ]; then
  die "--ssh key-only needs --ssh-key, or an existing $EXISTING_KEYS"
fi
if [ -n "$DATA_DIR_OPT" ] && [ "$NO_OVERLAY" = 0 ] && ! { have findmnt && findmnt -n "$DATA" > /dev/null 2>&1; }; then
  warn "$DATA is a directory on the root filesystem: with the overlay on it would vanish at reboot. Implying --no-overlay."
  NO_OVERLAY=1
fi
# The release is named after the checkout (`git describe`). A fresh Raspberry Pi OS Lite has no
# git until step_apt installs it, so the name is settled in step_version, after the packages.
# `describe --dirty` refreshes and rewrites the index (even with --no-optional-locks): as root
# that would make the login user's .git/index root's. So it works on a private copy.
checkout_version() {
  local v idx tmp
  idx="$(git -C "$SRC" rev-parse --path-format=absolute --git-path index 2> /dev/null || true)"
  tmp="$(mktemp)"
  if [ -n "$idx" ] && cp "$idx" "$tmp" 2> /dev/null; then
    v="$(GIT_INDEX_FILE="$tmp" git -C "$SRC" describe --tags --always --dirty 2> /dev/null || echo dev)"
  else
    v=dev
  fi
  rm -f "$tmp" "$tmp.lock"
  echo "${v//\//-}"
}
VERSION="$(if have git; then checkout_version; else echo "(named after the packages are installed)"; fi)"
REL=
# The service and build accounts are system users this script makes; never a person's login.
# (The login user, often called ledposter, is only read: its authorized_keys, above.)
for u in "$SVC_USER" "$BUILD_USER"; do
  ent="$(getent passwd "$u" 2> /dev/null || true)"
  [ -n "$ent" ] || continue
  home="$(printf '%s\n' "$ent" | cut -d: -f6)"
  in_sudo="$(id -nG "$u" 2> /dev/null | tr ' ' '\n' | grep -x sudo || true)"
  if [ "${home#/home/}" != "$home" ] || [ -n "$in_sudo" ]; then
    die "a login user named $u already exists (home $home${in_sudo:+, in group sudo}).
  The installer needs that name for the wall's unprivileged account, which gets polkit rights and
  owns the app's files, and will not take over a person's account. Rename or remove that user
  (from another login), then run the installer again."
  fi
done
if [ -n "$PREBUILT" ]; then
  [ -f "$PREBUILT" ] || die "--prebuilt $PREBUILT: no such file"
  PREBUILT="$(cd "$(dirname "$PREBUILT")" && pwd)/$(basename "$PREBUILT")"
  case "$(basename "$PREBUILT")" in
    ledposter-*-arm64.tar.zst | ledposter-*-arm64.tar.gz) ;;
    *) die "--prebuilt expects ledposter-<version>-arm64.tar.zst (or .tar.gz) from tools/build-release.sh" ;;
  esac
  PREBUILT_VERSION="$(basename "$PREBUILT")"
  PREBUILT_VERSION="${PREBUILT_VERSION#ledposter-}"
  PREBUILT_VERSION="${PREBUILT_VERSION%-arm64.tar.*}"
  if have git; then same_version; fi  # else after step_apt
  if [ -f "$PREBUILT.sha256" ]; then
    want="$(cut -d' ' -f1 < "$PREBUILT.sha256")"
    [ "$want" = "$(sha256_of "$PREBUILT")" ] || die "$PREBUILT does not match $PREBUILT.sha256 (truncated or corrupted copy?)"
  else
    warn "no $PREBUILT.sha256 next to $PREBUILT: the tarball is not verified"
  fi
  if [ "$DRY" = 0 ] && [ "$(uname -m)" != aarch64 ]; then die "--prebuilt releases are arm64; this machine is $(uname -m)"; fi
fi

log "ledposter installer: $VERSION from $SRC -> $DATA (hostname $WANT_HOSTNAME, ssh $SSH_MODE, overlay $([ "$NO_OVERLAY" = 1 ] && echo off || echo on))"
[ "$DRY" = 1 ] && note "dry run: nothing is changed"
[ -n "$PREBUILT" ] && note "release: prebuilt $PREBUILT$([ -f "$PREBUILT.sha256" ] && echo " (sha256 ok)")"

# -- steps ----------------------------------------------------------------------------

step_version() {
  VERSION="$(checkout_version)"
  REL="$DATA/app/releases/$VERSION"
  log "Release name: $VERSION"
  case "$VERSION" in *-dirty) warn "the checkout has uncommitted changes; the release is built from HEAD, not the working tree" ;; esac
  if [ -n "$PREBUILT" ]; then same_version; fi
  return 0
}

step_apt() {
  log "Packages"
  if ! have apt-get; then note "no apt-get: skipping"; return; fi
  local optional="" build="$BUILD_PACKAGES"
  if [ -n "$PREBUILT" ]; then build=""; note "prebuilt release: not installing the build packages ($BUILD_PACKAGES)"; fi
  run env DEBIAN_FRONTEND=noninteractive apt-get update -q
  for p in $RUNTIME_LIBS $OPTIONAL_PACKAGES; do  # after the update: apt-cache only knows what the lists have
    if [ "$DRY" = 1 ] || apt-cache show "$p" > /dev/null 2>&1; then optional="$optional $p"; fi
  done
  # shellcheck disable=SC2086  # word splitting of the package list is the point
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $RUNTIME_PACKAGES $build $optional
}

step_user() {
  log "Service user $SVC_USER, build user $BUILD_USER"
  for u in "$SVC_USER" "$BUILD_USER"; do
    if ! have getent || ! getent group "$u" > /dev/null; then run groupadd --system "$u"; fi
    if ! id "$u" > /dev/null 2>&1; then
      run useradd --system --gid "$u" --home-dir "$DATA/app" --no-create-home --shell /usr/sbin/nologin "$u"
    fi
  done
  for g in gpio i2c video; do  # the engine drops to this user: GPIO/I2C access after the drop
    if have getent && getent group "$g" > /dev/null; then run usermod -aG "$g" "$SVC_USER"; fi
  done
}

no_room() {
  cat >&2 <<EOF

No room for the data partition on $1: no unallocated space after the root partition and no
partition labelled 'ledpostdata'. Raspberry Pi OS grows the root partition to fill the card on
its first boot, so on a card that has booted once this is expected. Options:

  a) Fresh card (recommended): flash it again and BEFORE the first boot open cmdline.txt on the
     boot partition (bootfs) from your computer and delete the word
         resize
     (Trixie; on older images the text init=/usr/lib/raspi-config/init_resize.sh instead)
     The root then stays about 5GB; boot, run this installer, and it creates the data
     partition in the free space (everything big lives on /data).
  b) This card: shrink the root partition from another computer (gparted), leaving at least
     1GB free at the end of the card, then run the installer again.
  c) Make the partition yourself (ext4, label ledpostdata, any size): it is found by label.
  d) Testing only: --data-dir /home/$LOGIN_USER/lpdata (a directory on the root; implies --no-overlay).
EOF
  exit 1
}

step_data() {
  log "Data partition at $DATA"
  if [ -n "$DATA_DIR_OPT" ]; then
    run mkdir -p "$DATA"
    note "using the directory $DATA (no separate partition)"
    return
  fi
  if have findmnt && findmnt -n /data > /dev/null 2>&1; then
    note "/data is already mounted ($(findmnt -no SOURCE,FSTYPE,OPTIONS /data))"
    return
  fi
  if ! have blkid || ! have sfdisk; then die "blkid/sfdisk not found; cannot set up the data partition (use --data-dir for testing)"; fi
  local dev disk free part partuuid
  dev="$(blkid -L ledpostdata 2> /dev/null || true)"
  if [ -z "$dev" ]; then
    disk="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
    free="$(sfdisk -F "$disk" 2> /dev/null | sed -n 's/^Unpartitioned space .*: .*, \([0-9]*\) bytes.*/\1/p' | head -n 1)"
    free="${free:-0}"
    if [ "$free" -lt 1073741824 ]; then no_room "$disk"; fi
    note "creating a $((free / 1048576)) MB partition in the free space on $disk"
    if [ "$DRY" = 1 ]; then note "+ echo ',,L' | sfdisk --append $disk; mkfs.ext4 -L ledpostdata <new partition>"; dev="$disk"p9; else
      # Start after the last partition (1MiB aligned): sfdisk's default start is the first free
      # gap, which on Pi images is the 7MB before bootfs (found the hard way: a 7MB /data).
      local dn last=0 s
      dn="$(basename "$disk")"
      for p in /sys/block/"$dn"/"$dn"*; do
        [ -r "$p/start" ] || continue
        s=$(($(cat "$p/start") + $(cat "$p/size")))
        if [ "$s" -gt "$last" ]; then last=$s; fi
      done
      echo "$(((last + 2047) / 2048 * 2048)),,L" | sfdisk --append --no-reread "$disk"
      partprobe "$disk" 2> /dev/null || partx -a "$disk" 2> /dev/null || true
      sleep 1
      part="$(lsblk -nlo NAME "$disk" | tail -n 1)"
      dev="/dev/$part"
      mkfs.ext4 -q -F -L ledpostdata "$dev"
    fi
  else
    note "found partition labelled ledpostdata: $dev"
  fi
  partuuid="$(blkid -s PARTUUID -o value "$dev" 2> /dev/null || echo "XXXX-XX")"
  if ! in_file " /data " /etc/fstab; then
    # nofail: a missing or broken data partition must not drop the boot into emergency mode; the
    # wall's units require the mount themselves, SSH falls back to the installed keys (README).
    local line="PARTUUID=$partuuid  /data  ext4  defaults,nofail,noatime,commit=15,x-systemd.device-timeout=10  0  2"
    note "fstab: $line"
    if [ "$DRY" = 0 ]; then echo "$line" >> /etc/fstab; fi
  fi
  run mkdir -p /data
  if have_systemd; then run systemctl daemon-reload; fi
  run mount /data
}

step_release() {
  log "Release $VERSION under $DATA/app"
  # /data/app is root's: the engine runs from it as root, so the web app (user ledwall) may
  # read and execute but never write here. A prebuilt release is unpacked by root; otherwise
  # make/venv/pip run as ledwall-build in the release dir, which root then takes over (same as
  # web/ledposter/updater.py; docs/DEVIATIONS.md). Either way: root:root, then `.ready`.
  owned_dir root:root 755 "$DATA/app"
  owned_dir root:root 755 "$DATA/app/releases"
  owned_dir "$BUILD_USER:$BUILD_USER" 700 "$DATA/app/build"
  run rm -rf "$DATA/app/pip-cache"  # m8 kept the pip cache here, owned by its service user
  if [ -f "$REL/.ready" ] && [ "$FORCE_BUILD" = 0 ]; then
    note "already installed (use --force-build to replace it)"
  elif [ -n "$PREBUILT" ]; then
    # Unpacked next to the releases (same filesystem) and moved into place, so a half-unpacked
    # tree never has the release's name. GNU tar keeps entries under the extraction dir.
    local tmp="$DATA/app/releases/.unpack-$VERSION"
    note "unpacking the prebuilt release: nothing is compiled on this machine"
    run rm -rf "$REL" "$tmp"
    owned_dir root:root 700 "$tmp"
    run tar -C "$tmp" --no-same-owner -xf "$PREBUILT"
    if [ "$DRY" = 0 ]; then
      local top
      top="$(find "$tmp" -mindepth 1 -maxdepth 1 -printf '%f ')"
      [ "$top" = "$VERSION " ] || die "$PREBUILT must hold exactly one directory, $VERSION/; it has: $top"
    fi
    run mv "$tmp/$VERSION" "$REL"
    run rmdir "$tmp"
    run chown -R root:root "$REL"
    if [ "$DRY" = 0 ]; then
      # Built on debian:trixie arm64: the venv needs the same Python minor version, the engine its libraries.
      local want have_py missing
      want="$(sed -n 's/^version *= *\([0-9]*\.[0-9]*\).*/\1/p' "$REL/web/.venv/pyvenv.cfg")"
      have_py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
      [ "$want" = "$have_py" ] || die "the prebuilt venv is for Python $want, this system has $have_py (build the release on the matching Debian, or drop --prebuilt)"
      missing="$(ldd "$REL/engine/ledengine" | grep 'not found' || true)"
      [ -z "$missing" ] || die "the prebuilt engine is missing libraries: $missing"
      "$REL/web/.venv/bin/python" -I -c 'import fastapi, uvicorn, yaml, numpy' || die "the prebuilt venv does not import its packages"
    fi
    run touch "$REL/.ready"
    # The OS image may ship a compiler anyway (Raspberry Pi OS Lite trixie does): this marker, not
    # the absence of gcc, is what stops the in-app updater from building on the wall.
    write 644 "$DATA/app/.prebuilt" <<< "installed from a prebuilt release; update with tools/deploy.sh"
  else
    run rm -rf "$REL"
    run git clone -q "$SRC" "$REL"
    if [ -d "$SRC/vendor/rpi-rgb-led-matrix/lib" ]; then
      # Clone the submodule from the local checkout: no network, and unlike --reference it
      # also works when that checkout is shallow.
      run git -C "$REL" config submodule.vendor/rpi-rgb-led-matrix.url "$SRC/vendor/rpi-rgb-led-matrix"
      run git -C "$REL" -c protocol.file.allow=always submodule update --init
    else
      run git -C "$REL" submodule update --init
    fi
    note "building the engine as $BUILD_USER (many minutes on a Pi 3B; tools/deploy.sh avoids this)"
    run chown -R "$BUILD_USER:$BUILD_USER" "$REL"
    as_build make -C "$REL/engine"
    as_build python3 -m venv --system-site-packages "$REL/web/.venv"
    as_build "$REL/web/.venv/bin/pip" install -q --no-cache-dir --retries 5 --timeout 60 -r "$REL/web/requirements.txt"
    as_build make -C "$REL/engine" test
    run chown -R root:root "$REL"
    run touch "$REL/.ready"
  fi
  if [ "$DRY" = 0 ]; then
    # Before it goes live: the service user must be able to run it (a release packed with a
    # private umask once passed every root-side check and then failed as ledwall).
    (cd "$REL/web" && runuser -u "$SVC_USER" -- ./.venv/bin/python -c 'import ledposter.app') \
      || die "$SVC_USER cannot run the web app in $REL (permissions?); current is unchanged"
    local help  # usage goes to stderr with exit 2, so match the text, not the status (pipefail)
    help="$(runuser -u "$SVC_USER" -- "$REL/engine/ledengine" --help 2>&1 || true)"
    case "$help" in
      *"usage: ledengine"*) ;;
      *) die "$SVC_USER cannot run $REL/engine/ledengine (permissions?): $help; current is unchanged" ;;
    esac
  fi
  run ln -sfn "releases/$VERSION" "$DATA/app/.current.tmp"
  run mv -fT "$DATA/app/.current.tmp" "$DATA/app/current"
  # How the updater reaches the repo: the ssh form of this checkout's origin (deploy key).
  local origin
  origin="$(git -C "$SRC" remote get-url origin 2> /dev/null || echo git@github.com:tanujdargan/ledposter.git)"
  case "$origin" in https://github.com/*) origin="${origin#https://github.com/}"; origin="git@github.com:${origin%.git}.git" ;; esac
  write 644 "$DATA/app/origin" <<< "$origin"
  if [ ! -f "$DATA/app/deploy_key" ]; then
    note "creating the read-only deploy key $DATA/app/deploy_key"
    run ssh-keygen -q -t ed25519 -N "" -C "ledposter-$WANT_HOSTNAME" -f "$DATA/app/deploy_key"
  fi
  # The key is read-only on GitHub; group ledwall so "Check for updates" (git ls-remote in
  # the web app) can use it. Root's otherwise.
  run chown -h root:root "$DATA/app/origin" "$DATA/app/deploy_key.pub"
  run chown -h "root:$SVC_USER" "$DATA/app/deploy_key"
  run chmod 640 "$DATA/app/deploy_key"
  if [ -f "$DATA/app/token" ]; then run chown -h "root:$SVC_USER" "$DATA/app/token"; run chmod 640 "$DATA/app/token"; fi
  if [ ! -s "$DATA/app/known_hosts" ] && have ssh-keyscan; then  # pinned host keys for the updater's git over ssh
    if [ "$DRY" = 1 ]; then note "+ ssh-keyscan github.com > $DATA/app/known_hosts"; else
      if ssh-keyscan -T 10 -t ed25519,ecdsa,rsa github.com > "$DATA/app/known_hosts.tmp" 2> /dev/null; then
        mv -f "$DATA/app/known_hosts.tmp" "$DATA/app/known_hosts"
      else
        rm -f "$DATA/app/known_hosts.tmp"; warn "could not reach github.com to pin its host key; the first update accepts it"
      fi
    fi
  fi
  # Anything left over from an m8 install (owned by its service user) becomes root's; -h/-P: no symlink is followed.
  run chown -h -R root:root "$DATA/app/releases" "$DATA/app/current"
  [ -L "$DATA/app/previous" ] && run chown -h root:root "$DATA/app/previous"
  for f in update.json pending.json; do [ -f "$DATA/app/$f" ] && run chown -h root:root "$DATA/app/$f"; done
  run rm -f "$DATA/app/update-request.json"  # m8 kept the request here; it is now $DATA/update-request.json
  return 0
}

step_files() {
  log "Data files"
  # The layout (docs/ARCHITECTURE.md): $DATA root:ledwall 1775, so the web app can add its
  # own files (config.yaml, secret, the unit inputs) but cannot rename or remove root's app/,
  # state/ and logs/; media/, ssh/ and logs/web/ are the web app's. Root never follows a path
  # the app owns: ledposter-sys layout only creates what is missing and chowns what it made.
  owned_dir "root:$SVC_USER" 1775 "$DATA"
  owned_dir root:root 755 "$DATA/state"
  if [ -d "$DATA/logs" ] && [ ! -L "$DATA/logs" ] && [ "$(stat -c %u "$DATA/logs")" != 0 ]; then  # an m8 install: logs/ was the service user's
    run chown -h root:root "$DATA/logs"
    for f in update.log maintenance.log; do [ -f "$DATA/logs/$f" ] && run chown -h root:root "$DATA/logs/$f"; done
    run rm -f "$DATA/logs"/web.log*
  fi
  if [ "$DRY" = 1 ]; then note "+ ledposter-sys layout"; else LEDPOSTER_DATA="$DATA" sh "$SRC/system/bin/ledposter-sys" layout; fi
  run rm -f "$DATA/state/maintenance-request"  # m8 put the request here; it is now $DATA/maintenance-request
  if [ ! -s "$DATA/setup-password" ]; then
    local pw
    pw="$(LC_ALL=C tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' < /dev/urandom | head -c 10 || true)"  # tr gets SIGPIPE: fine
    [ "${#pw}" = 10 ] || die "could not generate the setup password"
    write 600 "$DATA/setup-password" <<< "$pw"
  fi
  run chown -h "$SVC_USER:$SVC_USER" "$DATA/setup-password"
  local keys="" tmp
  tmp="$(mktemp)"
  if [ -n "$SSH_KEY" ]; then printf '%s\n' "$SSH_KEY" > "$tmp"; keys="$tmp"; elif [ -s "$EXISTING_KEYS" ]; then cat "$EXISTING_KEYS" > "$tmp"; keys="$tmp"; fi
  chmod 644 "$tmp"  # public keys; init-config runs as the service user
  local args=("$DATA/config.yaml" --ssh "$SSH_MODE")
  [ -n "$keys" ] && args+=(--keys "$keys")
  # Writes the config (a fresh one from config/config.example.yaml, or just the ssh section of
  # an existing one) and the files the units read: firewall.rules, ssh/mode, ssh/authorized_keys,
  # maintenance.conf. As the service user, so they are its files and root chowns nothing.
  if [ "$DRY" = 1 ]; then note "+ (cd $REL/web && LEDPOSTER_DATA=$DATA runuser -u $SVC_USER -- .venv/bin/python -m ledposter.system init-config ${args[*]})"; else
    for f in config.yaml firewall.rules maintenance.conf; do  # an m8 install wrote these as root
      [ -f "$DATA/$f" ] && [ ! -L "$DATA/$f" ] && chown -h "$SVC_USER:$SVC_USER" "$DATA/$f"
    done
    for f in mode authorized_keys; do [ -f "$DATA/ssh/$f" ] && [ ! -L "$DATA/ssh/$f" ] && chown -h "$SVC_USER:$SVC_USER" "$DATA/ssh/$f"; done
    (cd "$REL/web" && LEDPOSTER_DATA="$DATA" runuser -u "$SVC_USER" -- "$REL/web/.venv/bin/python" -m ledposter.system init-config "${args[@]}")
  fi
  rm -f "$tmp"
}

step_system_files() {
  log "System files"
  inst 755 "$SRC/system/bin/ledposter-net" /usr/local/bin/ledposter-net
  inst 755 "$SRC/system/bin/ledposter-sys" /usr/local/bin/ledposter-sys
  for u in "$SRC"/system/systemd/*.service "$SRC"/system/systemd/*.timer; do install_unit "$u"; done
  inst 644 "$SRC/system/polkit/49-ledposter-network.rules" /etc/polkit-1/rules.d/49-ledposter-network.rules
  inst 644 "$SRC/system/polkit/50-ledposter-system.rules" /etc/polkit-1/rules.d/50-ledposter-system.rules
  inst 644 "$SRC/system/avahi/ledposter.service" /etc/avahi/services/ledposter.service
  inst 644 "$SRC/system/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf" /etc/NetworkManager/dnsmasq-shared.d/ledposter-captive.conf
  inst 644 "$SRC/system/etc/NetworkManager-ledposter.conf" /etc/NetworkManager/conf.d/ledposter.conf
  inst 644 "$SRC/system/etc/journald-ledposter.conf" /etc/systemd/journald.conf.d/ledposter.conf
  inst 644 "$SRC/system/etc/apt-52ledposter" /etc/apt/apt.conf.d/52ledposter
  inst 644 "$SRC/system/etc/sysctl-ledposter.conf" /etc/sysctl.d/90-ledposter.conf  # E1.31 multicast groups (M7), RT throttling off
  inst 644 "$SRC/system/etc/modprobe-ledposter-blacklist-audio.conf" /etc/modprobe.d/ledposter-blacklist-audio.conf
  # fake-hwclock keeps its stamp on /data, so the clock is roughly right before NTP even with
  # the overlay (the stamp file would otherwise be frozen at install time).
  write 644 /etc/default/fake-hwclock <<EOF
# Written by ledposter install.sh: keep the clock stamp on the writable data partition.
FILE=$DATA/state/fake-hwclock.data
EOF
  if have_systemd; then run systemctl daemon-reload; fi
}

step_network() {
  log "NetworkManager"
  if ! have nmcli || ! have_systemd || ! nmcli -t general status > /dev/null 2>&1; then note "NetworkManager not running: skipping"; return; fi
  # Stable DHCP on the profiles that already exist (new ones get it from conf.d and the app).
  nmcli -t -f NAME,TYPE connection show | while IFS=: read -r name type; do
    case "$type" in 802-11-wireless | 802-3-ethernet) run nmcli connection modify "$name" ipv4.dhcp-client-id mac ipv4.dhcp-send-hostname yes ;; esac
  done
  # Wi-Fi profiles must survive the overlay: keep them on /data and bind-mount the dir.
  local nmdir=/etc/NetworkManager/system-connections
  if ! in_file " $nmdir " /etc/fstab; then
    run mkdir -p "$DATA/state/nm"
    run chmod 700 "$DATA/state/nm"
    if [ "$DRY" = 0 ] && [ -d "$nmdir" ]; then cp -a "$nmdir/." "$DATA/state/nm/"; fi
    local line="$DATA/state/nm  $nmdir  none  bind,nofail,x-systemd.requires-mounts-for=$DATA  0  0"
    note "fstab: $line"
    if [ "$DRY" = 0 ]; then echo "$line" >> /etc/fstab; fi
    run systemctl daemon-reload
    run mount "$nmdir"
    run nmcli connection reload
  else
    note "Wi-Fi profiles already persisted in $DATA/state/nm"
  fi
}

step_hostname() {
  log "Hostname $WANT_HOSTNAME"
  if [ "$DRY" = 1 ]; then note "+ ledposter-sys hostname $WANT_HOSTNAME"; else LEDPOSTER_DATA="$DATA" /usr/local/bin/ledposter-sys hostname "$WANT_HOSTNAME"; fi
}

step_boot_config() {
  log "Boot config (audio off, isolcpus=3, performance governor, I2C on)"
  if ! is_pi; then note "not a Raspberry Pi OS boot partition: skipping"; return; fi
  local cfg=/boot/firmware/config.txt cmd=/boot/firmware/cmdline.txt tok name
  if findmnt -no OPTIONS /boot/firmware | grep -q '^ro,\|,ro,\|,ro$'; then run mount -o remount,rw /boot/firmware; fi
  if grep -q '^dtparam=audio=on' "$cfg"; then run sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$cfg"; fi
  if ! grep -q '^dtparam=audio=off' "$cfg"; then run sh -c "echo 'dtparam=audio=off' >> $cfg"; fi
  # Faster to a picture: no 1 s firmware wait, no rainbow splash, no camera/DSI probing.
  for tok in boot_delay=0 disable_splash=1 camera_auto_detect=0 display_auto_detect=0; do
    name="${tok%%=*}"
    if grep -q "^$name=" "$cfg"; then run sed -i "s/^$name=.*/$tok/" "$cfg"; else run sh -c "echo '$tok' >> $cfg"; fi
  done
  # cmdline.txt is one line. A token is appended to it only when its key is not there at all,
  # so a value set by hand is kept and nothing is ever added twice. isolcpus=3: the matrix
  # library's refresh thread has core 3 to itself; the performance governor keeps the clock
  # (and so the refresh timing) from stepping with load. cloud-init=disabled: it only matters
  # on the imager's first boot, and after that cost ~10 s of CPU on every boot.
  for tok in isolcpus=3 cpufreq.default_governor=performance cloud-init=disabled; do
    name="${tok%%=*}="
    if ! grep -Eq "(^|[[:space:]])${name//./\\.}" "$cmd"; then run sed -i "1 s/\$/ $tok/" "$cmd"; fi
  done
  run raspi-config nonint do_i2c 0
}

step_services() {
  log "Services"
  if ! have_systemd; then note "systemd is not running (container?): skipping enable/disable"; return; fi
  for u in $DISABLE_UNITS; do
    if systemctl list-unit-files "$u" 2> /dev/null | grep -q "^$u"; then run systemctl disable --now "$u" || true; fi
  done
  if have dphys-swapfile; then run dphys-swapfile swapoff || true; fi
  # ssh.service is decided at every boot by ledposter-ssh.service from /data/ssh/mode.
  run systemctl disable ssh.service ssh.socket 2> /dev/null || true
  # shellcheck disable=SC2086
  run systemctl enable $ENABLE_UNITS
  if [ -f "$DATA/state/fake-hwclock.data" ] || [ ! -f /etc/fake-hwclock.data ]; then :; else run cp /etc/fake-hwclock.data "$DATA/state/fake-hwclock.data"; fi
}

step_firewall_ssh() {
  log "Firewall and SSH ($SSH_MODE)"
  if have_systemd && have ufw; then
    if [ "$DRY" = 1 ]; then note "+ ledposter-sys firewall"; else LEDPOSTER_DATA="$DATA" /usr/local/bin/ledposter-sys firewall; fi
  else
    note "ufw/systemd not available here: rules stay in $DATA/firewall.rules for ledposter-firewall.service"
  fi
  if [ "$SSH_MODE" = key-only ]; then
    if have_systemd && have sshd; then
      if [ "$DRY" = 1 ]; then note "+ ledposter-sys ssh"; else LEDPOSTER_DATA="$DATA" /usr/local/bin/ledposter-sys ssh; fi
    fi
    note "SSH: key-only (password logins are off from now on; your key is in $DATA/ssh/authorized_keys)"
  else
    note "SSH: disabled after the next reboot (this session keeps working until then)"
  fi
}

step_overlay() {
  log "Read-only root (overlay)"
  if [ "$NO_OVERLAY" = 1 ]; then note "--no-overlay: root stays writable (sudo ledposter-sys overlay on later)"; return; fi
  if ! is_pi; then note "not Raspberry Pi OS: skipping"; return; fi
  if grep -Eq 'overlayroot=tmpfs|boot=overlay' /proc/cmdline; then note "overlay already active"; run touch "$DATA/state/overlay-wanted"; return; fi
  run raspi-config nonint enable_bootro
  if [ "$DRY" = 1 ]; then note "+ ledposter-sys overlay on"; else LEDPOSTER_DATA="$DATA" /usr/local/bin/ledposter-sys overlay on; fi
  note "the overlay takes effect at the next boot"
}

summary() {
  local pw="(see $DATA/setup-password)" pub="(see $DATA/app/deploy_key.pub)"
  [ -s "$DATA/setup-password" ] && pw="$(cat "$DATA/setup-password")"
  [ -s "$DATA/app/deploy_key.pub" ] && pub="$(cat "$DATA/app/deploy_key.pub")"
  cat <<EOF

================================================================================
  LED wall installed: $VERSION  ->  http://$WANT_HOSTNAME.local/

  Setup hotspot password (print this on the card in the box):

      $pw

  Hotspot: LEDWall-Setup   Portal: http://10.42.0.1/   Admin password: set on first visit
  SSH: $SSH_MODE$([ "$SSH_MODE" = key-only ] && echo " (log in as ${LOGIN_USER:-your user} with your key)")
  Root filesystem: $([ "$NO_OVERLAY" = 1 ] && echo "writable (--no-overlay)" || echo "read-only overlay from the next boot")

  Software updates from the app need read access to the repo. Add this deploy key at
  https://github.com/<owner>/<repo>/settings/keys (read-only is enough; it is the only
  credential the wall holds, and a compromised web app could read it):

      $pub

  Next: sudo reboot, then docs/ACCEPTANCE.md (tools/acceptance.sh) and the README's
  "Testing milestone 8 on the Pi". Settings > Software shows the version and updates.
================================================================================
EOF
}

step_apt
step_version
step_user
step_data
step_release
step_files
step_system_files
step_network
step_hostname
step_boot_config
step_services
step_firewall_ssh
step_overlay
summary
