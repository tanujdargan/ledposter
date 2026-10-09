# shellcheck shell=sh disable=SC2034  # the variables are for the scripts that source this file
# The one place the apt list lives. Sourced by system/install.sh (on the Pi) and
# tools/build-release.sh (the debian:trixie arm64 build container), so a prebuilt release is
# compiled against exactly the packages the wall runs with.
#
# RUNTIME_PACKAGES: what the wall needs to run a release (always installed).
# RUNTIME_LIBS: the engine's shared libraries without their -dev package; the name carries the
#   soname, so apt installs whichever of these the distribution has (trixie: libyaml-cpp0.8,
#   Bookworm: libyaml-cpp0.7). The -dev package in BUILD_PACKAGES pulls in the same one.
# BUILD_PACKAGES: what compiling the engine and creating the venv needs. `install.sh --prebuilt`
#   skips these; then nothing on the Pi can compile (docs/DEVIATIONS.md).
# OPTIONAL_PACKAGES: installed when apt has them. overlayroot is what raspi-config's overlay uses
#   on current Raspberry Pi OS; older raspi-config builds do not need it.
RUNTIME_PACKAGES="git ffmpeg python3 python3-numpy avahi-daemon network-manager ufw unattended-upgrades polkitd
  curl i2c-tools jq openssh-client zstd"
RUNTIME_LIBS="libyaml-cpp0.8 libyaml-cpp0.7"
BUILD_PACKAGES="build-essential libyaml-cpp-dev nlohmann-json3-dev python3-venv python3-pip"
OPTIONAL_PACKAGES="overlayroot socat"
