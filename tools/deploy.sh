#!/bin/bash
# Installs (or updates) the wall from this computer: builds the release here (or reuses it),
# copies it and a checkout of the same version to the Pi, and runs the installer there with
# --prebuilt, streaming its output. The Pi compiles nothing. Usage:
#
#   tools/deploy.sh <ssh-target> [--tag TAG] [--out DIR] [--install-args "..."]
#                   [--sudo-pass-file FILE] [--rebuild] [--allow-dirty]
#
#   tools/deploy.sh ledposter@ledposter.local --install-args "--ssh-key 'ssh-ed25519 AAAA... me'"
#
# <ssh-target> is anything ssh accepts (user@host, a Host from ~/.ssh/config). TAG defaults to
# HEAD. The tarball comes from tools/build-release.sh into DIR (default dist/); an existing one
# with its .sha256 is reused unless --rebuild. On the Pi everything goes to ~/ledposter-deploy/
# (the checkout in src/), then `sudo ./system/install.sh --prebuilt ... <install-args>` runs
# there. sudo may ask for the login password: the session is `ssh -t` so it can. Instead,
# --sudo-pass-file FILE sends the first line of FILE to `sudo -S` on the Pi's stdin; it never
# appears on a command line (keep FILE chmod 600, or skip it and type the password).
# --install-args is passed to install.sh as it is (split by the Pi's shell).
# shellcheck disable=SC2029  # the remote commands are built here on purpose (fixed dir, the user's --install-args)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=
TAG=HEAD
OUT="$REPO/dist"
INSTALL_ARGS=
PASS_FILE=
REBUILD=0
DIRTY=()
REMOTE_DIR=ledposter-deploy  # relative to the login user's home on the Pi

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }
die() { printf 'deploy.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --install-args) INSTALL_ARGS="$2"; shift 2 ;;
    --sudo-pass-file) PASS_FILE="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --allow-dirty) DIRTY=(--allow-dirty); shift ;;
    -h | --help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$TARGET" ] || usage; TARGET="$1"; shift ;;
  esac
done
[ -n "$TARGET" ] || usage
if [ -n "$PASS_FILE" ]; then [ -r "$PASS_FILE" ] || die "--sudo-pass-file $PASS_FILE: not readable"; fi

commit="$(git -C "$REPO" rev-parse --verify --quiet "$TAG^{commit}")" || die "$TAG is not a commit or tag in $REPO"
VERSION="$("$REPO/tools/build-release.sh" --tag "$commit" --print-version)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

# The checkout the installer runs from: the same commit, with its tags (install.sh names the
# release with git describe and must get $VERSION) and the real origin (the updater's remote).
work="$(mktemp -d "${TMPDIR:-/tmp}/ledposter-deploy.XXXXXX")"
trap 'rm -rf "$work"' EXIT
git clone -q --no-checkout "$REPO" "$work/src"
git -C "$work/src" checkout -q --detach "$commit"
git -C "$work/src" remote set-url origin "$(git -C "$REPO" remote get-url origin 2> /dev/null || echo git@github.com:tanujdargan/ledposter.git)"
got="$(git -C "$work/src" describe --tags --always --dirty)"
[ "${got//\//-}" = "$VERSION" ] || die "the checkout describes itself as $got, the release is $VERSION"
grep -q -- '--prebuilt)' "$work/src/system/install.sh" || die "$VERSION's system/install.sh has no --prebuilt (older than tools/deploy.sh): pick a newer tag"

tarball=
for f in "$OUT/ledposter-$VERSION-arm64.tar.zst" "$OUT/ledposter-$VERSION-arm64.tar.gz"; do
  if [ -f "$f" ] && [ -f "$f.sha256" ]; then tarball="$f"; break; fi
done
if [ -z "$tarball" ] || [ "$REBUILD" = 1 ]; then
  "$REPO/tools/build-release.sh" --tag "$commit" --out "$OUT" "${DIRTY[@]}"
  tarball="$OUT/ledposter-$VERSION-arm64.tar.zst"
  [ -f "$tarball" ] || tarball="$OUT/ledposter-$VERSION-arm64.tar.gz"
else
  echo "==> reusing $tarball (--rebuild to build it again)"
fi
if [ ! -f "$tarball" ] || [ ! -f "$tarball.sha256" ]; then die "no tarball for $VERSION in $OUT"; fi

echo "==> copying $VERSION to $TARGET:$REMOTE_DIR/"
ssh "$TARGET" "mkdir -p $REMOTE_DIR/src"
if ssh "$TARGET" "command -v rsync > /dev/null"; then
  rsync -a --delete "$work/src/" "$TARGET:$REMOTE_DIR/src/"
  rsync -a "$tarball" "$tarball.sha256" "$TARGET:$REMOTE_DIR/"
else  # no rsync on the Pi: the same over tar
  echo "    (no rsync on the Pi: copying with tar over ssh)"
  ssh "$TARGET" "rm -rf $REMOTE_DIR/src && mkdir -p $REMOTE_DIR/src"
  COPYFILE_DISABLE=1 tar -C "$work/src" -cf - . | ssh "$TARGET" "tar -C $REMOTE_DIR/src -xf -"
  COPYFILE_DISABLE=1 tar -C "$(dirname "$tarball")" -cf - "$(basename "$tarball")" "$(basename "$tarball").sha256" | ssh "$TARGET" "tar -C $REMOTE_DIR -xf -"
fi

if ssh "$TARGET" "grep -Eq 'overlayroot=tmpfs|boot=overlay' /proc/cmdline"; then
  echo "    NOTE: the Pi's root is the read-only overlay. The release (on /data) is installed, but changes"
  echo "    to system files (units, polkit, /etc) are lost at the next reboot; for those: sudo ledposter-sys"
  echo "    overlay off, reboot, deploy again, sudo ledposter-sys overlay on, reboot (README: Software updates)."
fi
remote_tar="../$(basename "$tarball")"
echo "==> sudo ./system/install.sh --prebuilt $remote_tar $INSTALL_ARGS (on $TARGET)"
if [ -n "$PASS_FILE" ]; then
  # sudo -S reads the password from stdin (the first line of the file); -p '' keeps the prompt
  # out of the output. The installer itself gets the rest of stdin, which is empty.
  head -n 1 "$PASS_FILE" | ssh "$TARGET" "cd $REMOTE_DIR/src && sudo -S -p '' ./system/install.sh --prebuilt $remote_tar $INSTALL_ARGS"
else
  ssh -t "$TARGET" "cd $REMOTE_DIR/src && sudo ./system/install.sh --prebuilt $remote_tar $INSTALL_ARGS"
fi
