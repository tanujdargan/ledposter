"""The root-side shell scripts (system/bin/ledposter-sys, ledposter-net) against files the
unprivileged web app could have written: run with stubbed system commands on PATH and a temp
data dir. What must hold: maintenance.conf is parsed, never sourced; every input is read without
following symlinks and refused unless it is a plain file of the service user; the DHCP rollback
runs from the note at boot and treats its content as data."""
import os
import stat
import subprocess
import sys

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "..", "system", "bin")
SYS = os.path.join(BIN, "ledposter-sys")
NET = os.path.join(BIN, "ledposter-net")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")


@pytest.fixture
def world(tmp_path):
    """Stub commands log their argv to calls.log; `date` says Sunday 04:xx (the default window)."""
    stubs, data, log = tmp_path / "bin", tmp_path / "data", tmp_path / "calls.log"
    stubs.mkdir()
    data.mkdir()
    (data / "state").mkdir()
    for name in ("ufw", "systemctl", "nmcli", "apt-get", "unattended-upgrade", "sync", "hostnamectl", "sshd", "raspi-config", "runuser"):
        (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
                                  + ('[ "$1 $2 $3 $4" = "-t -f DEVICE,CONNECTION device" ] && echo "wlan0:Digiana"\n' if name == "nmcli" else "")
                                  + "exit 0\n")
        (stubs / name).chmod(0o755)
    (stubs / "date").write_text('#!/bin/sh\ncase "$1" in +%u) echo 7 ;; +%H) echo 04 ;; *) echo "2026-10-04 04:00:00" ;; esac\n')
    (stubs / "date").chmod(0o755)
    (stubs / "ledposter-sys").symlink_to(os.path.abspath(SYS))
    (stubs / "ledposter-net").symlink_to(os.path.abspath(NET))
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", LEDPOSTER_DATA=str(data))

    def run(*argv, script=SYS):
        r = subprocess.run(["sh", script, *argv], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def calls():
        return log.read_text().splitlines() if log.exists() else []
    return type("World", (), {"run": staticmethod(run), "calls": staticmethod(calls), "data": data, "log": log})


def test_input_reads_only_plain_files_of_the_service_user(world, tmp_path):
    d = world.data
    (d / "maintenance.conf").write_text("ENABLED=1\nDAY=6\nHOUR=4\n")
    assert world.run("input", "maintenance.conf") == (0, "ENABLED=1\nDAY=6\nHOUR=4\n")
    rc, out = world.run("input", "missing")
    assert rc == 2 and out == ""
    secret = tmp_path / "shadow"
    secret.write_text("root:$6$hash\n")
    (d / "net-rollback").symlink_to(secret)
    rc, out = world.run("input", "net-rollback")
    assert rc == 1 and "refusing" in out and "hash" not in out
    (d / "ssh").mkdir()
    (d / "ssh" / "mode").symlink_to(secret)
    rc, out = world.run("input", "ssh/mode")
    assert rc == 1 and "refusing" in out and "hash" not in out
    (d / "ssh").rename(d / "ssh-real")
    (d / "ssh").symlink_to(tmp_path)  # a directory component swapped for a symlink
    (tmp_path / "mode").write_text("key-only\n")
    rc, out = world.run("input", "ssh/mode")
    assert rc == 1 and "refusing" in out
    (d / "ssh").unlink()
    os.mkfifo(d / "factory-reset")
    rc, out = world.run("input", "factory-reset")
    assert rc == 1 and "not a regular file" in out
    (d / "big").write_bytes(b"x" * 65537)
    rc, out = world.run("input", "big")
    assert rc == 1 and "too big" in out
    for bad in ("../etc/passwd", "/etc/passwd", "a/../b", ""):
        rc, out = world.run("input", bad)
        assert rc != 0 and "passwd" not in out.replace(bad, ""), bad


def test_maintenance_conf_is_parsed_not_sourced(world):
    d = world.data
    marker = d / "pwned"
    # what a compromised web app might write: shell in the values, extra lines, out-of-range numbers
    for conf in (f"ENABLED=1; touch {marker}\nDAY=6\nHOUR=4\n", f"ENABLED=$(touch {marker})\nDAY=6\nHOUR=4\n",
                 f"ENABLED=1\nDAY=6\nHOUR=4\ntouch {marker}\n", "ENABLED=1\nDAY=7\nHOUR=4\n", "ENABLED=2\nDAY=6\nHOUR=4\n",
                 "ENABLED=1\nDAY=6\nHOUR=24\n", "ENABLED=1\nDAY=6\n", "ENABLED=1\nDAY=06\nHOUR=4\n", "export ENABLED=1\n"):
        (d / "maintenance.conf").write_text(conf)
        rc, out = world.run("maintenance", "timer")
        assert rc == 1 and "refused" in out, conf
        assert not marker.exists() and not world.calls(), conf
    # a good file in the window: the upgrade runs (overlay off here, so straight to apt)
    (d / "maintenance.conf").write_text("ENABLED=1\nDAY=6\nHOUR=4\n")
    rc, out = world.run("maintenance", "timer")
    assert rc == 0 and [c.split()[0] for c in world.calls()] == ["apt-get", "unattended-upgrade"]
    assert "upgrade finished" in (d / "logs" / "maintenance.log").read_text()
    world.log.unlink()
    # not the window: nothing happens; disabled: nothing happens; missing file: the defaults (Sunday 04:00) apply
    (d / "maintenance.conf").write_text("ENABLED=1\nDAY=2\nHOUR=4\n")
    assert world.run("maintenance", "timer") == (0, "") and not world.calls()
    (d / "maintenance.conf").write_text("ENABLED=0\nDAY=6\nHOUR=4\n")
    assert world.run("maintenance", "timer") == (0, "") and not world.calls()
    (d / "maintenance.conf").unlink()
    rc, _ = world.run("maintenance", "timer")
    assert rc == 0 and [c.split()[0] for c in world.calls()] == ["apt-get", "unattended-upgrade"]
    world.log.unlink()
    # "Run now": the request file at the top of the data dir (state/ is root's), removed once seen
    (d / "maintenance.conf").write_text("ENABLED=0\nDAY=0\nHOUR=0\n")
    (d / "maintenance-request").write_text("1700000000\n")
    rc, _ = world.run("maintenance", "timer")
    assert rc == 0 and world.calls() and not (d / "maintenance-request").exists()


def test_firewall_and_ssh_inputs_are_validated_line_by_line(world):
    d = world.data
    (d / "firewall.rules").write_text("default deny incoming\nallow 80/tcp\nallow 22/tcp; rm -rf /\n")
    rc, out = world.run("firewall")
    assert rc == 1 and "refusing line" in out and not world.calls()
    (d / "firewall.rules").write_text("default deny incoming\ndefault allow outgoing\nlogging off\nallow 80/tcp\n")
    rc, out = world.run("firewall")
    assert rc == 0 and [c for c in world.calls() if c.startswith("ufw")] == [
        "ufw --force reset", "ufw default deny incoming", "ufw default allow outgoing", "ufw logging off", "ufw allow 80/tcp", "ufw --force enable"]
    world.log.unlink()
    (d / "firewall.rules").unlink()
    (d / "firewall.rules").symlink_to("/etc/passwd")
    rc, out = world.run("firewall")
    assert rc == 1 and "refused" in out and not world.calls()
    (d / "ssh").mkdir()
    (d / "ssh" / "mode").write_text("key-only\n")
    (d / "ssh" / "authorized_keys").write_text('command="rm -rf /" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDQ6Z8ZQ7u9w3mT2Hn5QxqV0lR9ZkD5z1y8o7PeXYgAb me\n')
    rc, out = world.run("ssh")
    assert rc == 1 and "refusing line" in out and not world.calls()
    (d / "ssh" / "mode").unlink()
    (d / "ssh" / "mode").symlink_to("/etc/hostname")
    rc, out = world.run("ssh")
    assert rc == 1 and "refused" in out and not world.calls()


def test_rollback_note_is_data_and_runs_at_boot_too(world):
    """ledposter-net-rollback.service runs when the timer elapses and, since the timer does not
    survive a reboot, at boot while the note exists (ConditionPathExists in the unit)."""
    d = world.data
    rc, out = world.run("rollback", script=NET)
    assert rc == 0 and "no rollback pending" in out and not world.calls()
    (d / "net-rollback").write_text("Digiana\n1700000120\n192.168.1.51\n")
    rc, out = world.run("rollback", script=NET)
    assert rc == 0 and "back to DHCP" in out and not (d / "net-rollback").exists()
    assert world.calls()[0] == "nmcli connection modify Digiana ipv4.method auto ipv4.addresses  ipv4.gateway  ipv4.dns  ipv4.ignore-auto-dns no"
    assert world.calls()[-1] == "nmcli device reapply wlan0"
    world.log.unlink()
    for bad in ("--help\n", "-x\n", "a\tb\n", "\n"):
        (d / "net-rollback").write_text(bad)
        rc, out = world.run("rollback", script=NET)
        assert rc == 1 and not world.calls() and not (d / "net-rollback").exists(), bad
    (d / "net-rollback").symlink_to("/etc/passwd")
    rc, out = world.run("rollback", script=NET)
    assert rc == 1 and "refused" in out and not world.calls() and not os.path.lexists(d / "net-rollback")
    unit = open(os.path.join(BIN, "..", "systemd", "ledposter-net-rollback.service")).read()
    assert "ConditionPathExists=/data/net-rollback" in unit and "WantedBy=multi-user.target" in unit


def test_layout_creates_only_what_is_missing_and_follows_nothing(world, tmp_path):
    d = world.data
    rc, out = world.run("layout")
    assert rc == 0
    for sub in ("media", "ssh", "logs", "logs/web", "state"):
        assert stat.S_ISDIR(os.lstat(d / sub).st_mode), sub
    (d / "media").rmdir()
    (d / "media").symlink_to(tmp_path)
    rc, out = world.run("layout")
    assert rc == 1 and "symlink" in out
