#!/bin/bash
# Builds a release for the wall on a computer instead of on the Pi (a Pi 3B on a weak supply
# should never compile). Needs Docker (Docker Desktop on Apple Silicon runs linux/arm64
# natively; on x86 Linux install qemu-user-static/binfmt for arm64). Usage:
#
#   tools/build-release.sh [--tag TAG] [--out DIR] [--allow-dirty] [--print-version]
#
# In a debian:trixie linux/arm64 container with the installer's apt packages (system/packages.sh)
# it checks out TAG (default: HEAD; a checkout with uncommitted changes is refused unless
# --allow-dirty, and even then the release is HEAD, not the working tree), builds the vendor
# library and the engine (make, make test), makes the venv with --system-site-packages, pip
# installs web/requirements.txt, checks the web app imports, and byte-compiles. A venv has
# absolute paths in it, and the engine has its fonts dir compiled in, so the release is built
# at the path it is installed to: /data/app/releases/<version>. <version> is `git describe
# --tags --always` of TAG, the name system/install.sh gives the release dir.
#
# Output (DIR, default dist/): ledposter-<version>-arm64.tar.zst (.tar.gz without zstd) and
# <that>.sha256. Install it with `sudo ./system/install.sh --prebuilt <tarball>` from a checkout
# of the same version, or let tools/deploy.sh do both. --print-version only prints <version>.
set -euo pipefail
umask 022  # the caller's umask must not leak into the release (a 077 one made it unreadable to ledwall)

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TAG=HEAD
OUT="$REPO/dist"
ALLOW_DIRTY=0
PRINT_VERSION=0
IMAGE_BASE=debian:trixie
PLATFORM=linux/arm64

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }
die() { printf 'build-release.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --print-version) PRINT_VERSION=1; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

commit="$(git -C "$REPO" rev-parse --verify --quiet "$TAG^{commit}")" || die "$TAG is not a commit or tag in $REPO"
VERSION="$(git -C "$REPO" describe --tags --always "$commit")"
VERSION="${VERSION//\//-}"  # as system/install.sh names the release dir
if [ "$PRINT_VERSION" = 1 ]; then echo "$VERSION"; exit 0; fi
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  [ "$ALLOW_DIRTY" = 1 ] || die "the checkout has uncommitted changes; commit them, or --allow-dirty to build $TAG ($commit) anyway"
  echo "build-release.sh: uncommitted changes are NOT in the release; building $TAG ($commit)" >&2
fi
command -v docker > /dev/null || die "docker not found"
docker info > /dev/null 2>&1 || die "Docker is not running (macOS: open -a Docker, then wait for 'docker info' to work)"
ORIGIN="$(git -C "$REPO" remote get-url origin 2> /dev/null || echo git@github.com:tanujdargan/ledposter.git)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/ledposter-release.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# The source: a clone of the commit with its submodule, as system/install.sh clones it.
echo "==> $VERSION ($commit): source"
git clone -q --no-checkout "$REPO" "$work/src"
git -C "$work/src" checkout -q --detach "$commit"
git -C "$work/src" remote set-url origin "$ORIGIN"
if [ -d "$REPO/vendor/rpi-rgb-led-matrix/lib" ]; then  # no network: the submodule from this checkout
  git -C "$work/src" config submodule.vendor/rpi-rgb-led-matrix.url "$REPO/vendor/rpi-rgb-led-matrix"
  git -C "$work/src" -c protocol.file.allow=always submodule update -q --init
else
  git -C "$work/src" submodule update -q --init
fi

# The build image: debian:trixie arm64 + the installer's packages, from that version's list
# (or this checkout's, for tags older than system/packages.sh). Docker caches it by content.
packages="$work/src/system/packages.sh"
[ -f "$packages" ] || packages="$REPO/system/packages.sh"
mkdir "$work/image"
cp "$packages" "$work/image/packages.sh"
cat > "$work/image/Dockerfile" <<EOF
FROM --platform=$PLATFORM $IMAGE_BASE
COPY packages.sh /tmp/packages.sh
RUN . /tmp/packages.sh && apt-get update -q && libs="" && for p in \$RUNTIME_LIBS; do \\
      if apt-cache show "\$p" > /dev/null 2>&1; then libs="\$libs \$p"; fi; done && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \\
      \$RUNTIME_PACKAGES \$BUILD_PACKAGES \$libs && rm -rf /var/lib/apt/lists/*
EOF
echo "==> build image ($IMAGE_BASE $PLATFORM + system/packages.sh)"
image="ledposter-release-builder:$( (cat "$work/image/Dockerfile" "$work/image/packages.sh") | git hash-object --stdin | cut -c1-12)"
docker build -q --platform "$PLATFORM" -t "$image" "$work/image" > /dev/null

echo "==> build in the container (make, make test, venv, pip, byte-compile)"
name="ledposter-$VERSION-arm64.tar"
rm -f "$OUT/$name.zst" "$OUT/$name.zst.sha256" "$OUT/$name.gz" "$OUT/$name.gz.sha256"
docker run --rm --platform "$PLATFORM" -v "$work/src:/src:ro" -v "$OUT:/out" \
  -e VERSION="$VERSION" -e HOST_IDS="$(id -u):$(id -g)" "$image" bash -euo pipefail -c '
    rel="/data/app/releases/$VERSION"
    mkdir -p /data/app/releases
    cp -a /src "$rel"
    make -C "$rel/engine" -j"$(nproc)"
    make -C "$rel/engine" test
    python3 -m venv --system-site-packages "$rel/web/.venv"
    "$rel/web/.venv/bin/pip" install -q --no-cache-dir --retries 5 --timeout 60 -r "$rel/web/requirements.txt"
    (cd "$rel/web" && .venv/bin/python -c "import ledposter.app")
    # /data/app is read-only to the web app on the wall: compile now, it cannot write __pycache__ there
    "$rel/web/.venv/bin/python" -m compileall -q -j 0 "$rel/web" > /dev/null
    help="$("$rel/engine/ledengine" --help 2>&1 || true)"  # usage on stderr, exit 2
    case "$help" in *"usage: ledengine"*) ;; *) echo "ledengine does not run: $help" >&2; exit 1 ;; esac
    # Everyone may read and run the release (the wall runs it as ledwall); only root writes it.
    # The checkout came from the host with its umask, so set this explicitly.
    chmod -R u+rwX,go+rX,go-w "$rel"
    name="ledposter-$VERSION-arm64.tar"
    if command -v zstd > /dev/null; then
      tar -C /data/app/releases --numeric-owner --owner=0 --group=0 -cf - "$VERSION" | zstd -q -T0 -19 -o "/out/$name.zst"
      name="$name.zst"
    else
      tar -C /data/app/releases --numeric-owner --owner=0 --group=0 -czf "/out/$name.gz" "$VERSION"
      name="$name.gz"
    fi
    (cd /out && sha256sum "$name" > "$name.sha256")
    chown "$HOST_IDS" "/out/$name" "/out/$name.sha256"
  '
for f in "$OUT/$name.zst" "$OUT/$name.gz"; do
  if [ -f "$f" ]; then
    echo "==> $f ($(du -h "$f" | cut -f1))"
    cat "$f.sha256"
  fi
done
