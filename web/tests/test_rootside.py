"""The root-side shell scripts (system/bin/ledposter-sys, ledposter-net) against files the
unprivileged web app could have written: run with stubbed system commands on PATH and a temp
data dir. What must hold: maintenance.conf is parsed, never sourced; every input is read without
following symlinks and refused unless it is a plain file of the service user; the DHCP rollback
runs from the note at boot and treats its content as data."""
import os
import re
import stat
import subprocess
import sys

import pytest

from ledposter import updater

BIN = os.path.join(os.path.dirname(__file__), "..", "..", "system", "bin")
SYS = os.path.join(BIN, "ledposter-sys")
NET = os.path.join(BIN, "ledposter-net")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")


@pytest.fixture
def world(tmp_path):
    """Stub commands log their argv to calls.log; `date` says Sunday 04:xx (the default window).
    `sshd` rejects a drop-in with the new lines (MaxStartups) while world.sshd_fail exists, and rejects PerSourcePenalty* while world.sshd_old does."""
    stubs, data, log = tmp_path / "bin", tmp_path / "data", tmp_path / "calls.log"
    stubs.mkdir()
    data.mkdir()
    (data / "state").mkdir()
    sshd_fail, sshd_old = tmp_path / "sshd-fail", tmp_path / "sshd-old"
    for name in ("ufw", "systemctl", "nmcli", "apt-get", "unattended-upgrade", "sync", "hostnamectl", "sshd", "raspi-config", "runuser", "install"):
        (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
                                  + ('[ "$1 $2 $3 $4" = "-t -f DEVICE,CONNECTION device" ] && echo "wlan0:Digiana"\n' if name == "nmcli" else "")
                                  + (f'[ -e "{sshd_fail}" ] && grep -qs MaxStartups "{tmp_path}/etc-ssh/sshd_config.d/10-ledposter.conf" && exit 1\n[ -e "{sshd_old}" ] && case "$*" in *PerSourcePenalty*) exit 1 ;; esac\n'
                                     if name == "sshd" else "")
                                  + "exit 0\n")
        (stubs / name).chmod(0o755)
    (stubs / "date").write_text('#!/bin/sh\ncase "$1" in +%u) echo 7 ;; +%H) echo 04 ;; *) echo "2026-10-04 04:00:00" ;; esac\n')
    (stubs / "date").chmod(0o755)
    (stubs / "ledposter-sys").symlink_to(os.path.abspath(SYS))
    (stubs / "ledposter-net").symlink_to(os.path.abspath(NET))
    etc_ssh = tmp_path / "etc-ssh"
    etc_ssh.mkdir()
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", LEDPOSTER_DATA=str(data), LEDPOSTER_SSH_ETC=str(etc_ssh),
               LEDPOSTER_CMDLINE=str(tmp_path / "cmdline.txt"))

    def run(*argv, script=SYS):
        r = subprocess.run(["sh", script, *argv], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def calls():
        return log.read_text().splitlines() if log.exists() else []
    return type("World", (), {"run": staticmethod(run), "calls": staticmethod(calls), "data": data, "log": log,
                              "etc_ssh": etc_ssh, "sshd_fail": sshd_fail, "sshd_old": sshd_old})


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


def test_boot_restores_a_garbled_wifi_profile_from_backup(world, tmp_path):
    nm, bak = world.data / "state" / "nm", world.data / "state" / "nm-backup"
    nm.mkdir()
    good = "[connection]\nid=SlowWifi\ntype=wifi\n"
    (nm / "SlowWifi.nmconnection").write_text(good)
    assert world.run("boot")[0] == 0
    assert (bak / "SlowWifi.nmconnection").read_text() == good
    assert stat.S_IMODE(os.stat(bak).st_mode) == 0o700
    (nm / "SlowWifi.nmconnection").write_bytes(b"\xff\xfe garbage")  # power cut mid-write
    rc, out = world.run("boot")
    assert rc == 0 and "garbled: restoring" in out
    assert (nm / "SlowWifi.nmconnection").read_text() == good
    assert (bak / "SlowWifi.nmconnection").read_text() == good  # garbage never overwrites the backup
    assert sorted(os.listdir(nm)) == ["SlowWifi.nmconnection"]  # no temp file left behind
    (nm / "SlowWifi.nmconnection").unlink()  # the only profile gone: put back
    rc, out = world.run("boot")
    assert rc == 0 and "no good Wi-Fi profile left" in out and (nm / "SlowWifi.nmconnection").read_text() == good
    (nm / "SlowWifi.nmconnection").unlink()
    (nm / "SlowWifi.nmconnection").symlink_to(tmp_path / "elsewhere")
    rc, out = world.run("boot")
    assert rc == 0 and not (tmp_path / "elsewhere").exists()


def test_boot_keeps_a_deleted_wifi_profile_deleted_while_another_is_good(world):
    nm, bak = world.data / "state" / "nm", world.data / "state" / "nm-backup"
    nm.mkdir()
    old, home = "[connection]\nid=Old\n", "[connection]\nid=Home\n"
    (nm / "Old.nmconnection").write_text(old)
    (nm / "Home.nmconnection").write_text(home)
    assert world.run("boot")[0] == 0 and sorted(os.listdir(bak)) == ["Home.nmconnection", "Old.nmconnection"]
    (nm / "Old.nmconnection").unlink()  # forgotten in Settings
    rc, out = world.run("boot")
    assert rc == 0 and "dropping its backup" in out
    assert sorted(os.listdir(nm)) == ["Home.nmconnection"] and sorted(os.listdir(bak)) == ["Home.nmconnection"]
    (nm / "Home.nmconnection").write_text("garbage\n")  # all Wi-Fi gone: only Home comes back, Old stays forgotten
    rc, out = world.run("boot")
    assert rc == 0 and (nm / "Home.nmconnection").read_text() == home and not (nm / "Old.nmconnection").exists()


def test_boot_carries_on_when_the_wifi_guard_fails(world):
    nm = world.data / "state" / "nm"
    nm.mkdir()
    (world.data / "state" / "nm-backup").symlink_to(world.data)  # root_dir dies on it
    (world.data / "state" / "hostname").write_text("wall-x\n")
    rc, out = world.run("boot")
    assert rc == 0 and "guard failed" in out and any(c.startswith("hostnamectl set-hostname wall-x") for c in world.calls())


def test_ssh_drop_in_is_validated_and_a_rejected_one_never_replaces_the_old(world):
    d, conf = world.data, world.etc_ssh / "sshd_config.d" / "10-ledposter.conf"
    (d / "ssh").mkdir()
    (d / "ssh" / "mode").write_text("key-only\n")
    (d / "ssh" / "authorized_keys").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDQ6Z8ZQ7u9w3mT2Hn5QxqV0lR9ZkD5z1y8o7PeXYgAb me\n")
    rc, out = world.run("ssh")
    assert rc == 0 and "key-only, 1 key(s)" in out
    text = conf.read_text()
    assert "PasswordAuthentication no" in text and "MaxStartups 20:30:60" in text and "LoginGraceTime 30" in text
    assert "PerSourcePenaltyExemptList 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,fe80::/10,fd00::/8" in text
    assert "systemctl reload-or-restart ssh.service" in world.calls()
    world.sshd_old.touch()  # OpenSSH < 9.8: the option is left out
    assert world.run("ssh")[0] == 0 and "PerSourcePenalty" not in conf.read_text() and "MaxStartups" in conf.read_text()
    world.sshd_old.unlink()
    world.log.unlink()
    conf.write_text("# previous\nPasswordAuthentication no\n")
    world.sshd_fail.touch()
    rc, out = world.run("ssh")
    assert rc == 1 and "rejected" in out and conf.read_text() == "# previous\nPasswordAuthentication no\n"
    assert "systemctl reload-or-restart ssh.service" in world.calls()  # started on the restored one (boot)
    assert sorted(os.listdir(conf.parent)) == ["10-ledposter.conf"]
    conf.unlink()  # no previous drop-in: a minimal key-only one, never none
    rc, out = world.run("ssh")
    assert rc == 1 and "PasswordAuthentication no" in conf.read_text() and "PermitRootLogin no" in conf.read_text()
    assert "/etc/ssh/ledposter_authorized_keys" in conf.read_text()  # the wall's keys still work


def test_overlay_keeps_data_out_of_it_and_can_still_be_turned_off(world, tmp_path):
    """raspi-config writes plain overlayroot=tmpfs (which overlays /data too, in RAM) and its
    disable removes only that exact token."""
    cmd = tmp_path / "cmdline.txt"
    cmd.write_text("overlayroot=tmpfs console=tty1 root=PARTUUID=27b94958-02 rootwait")  # as raspi-config left it
    assert world.run("overlay", "on")[0] == 0
    assert cmd.read_text() == "overlayroot=tmpfs:recurse=0 console=tty1 root=PARTUUID=27b94958-02 rootwait"
    assert world.run("overlay", "on")[0] == 0  # twice: no double suffix
    assert cmd.read_text().count("recurse=0") == 1
    assert world.run("overlay", "off")[0] == 0  # back to the exact token raspi-config removes
    assert cmd.read_text() == "overlayroot=tmpfs console=tty1 root=PARTUUID=27b94958-02 rootwait"
    assert not (tmp_path / "cmdline.txt.new").exists()


def test_service_accounts_are_ledwall_not_the_login_user():
    """The Pi's login user may well be called ledposter (and be in group sudo): no unit, polkit
    rule or root-side script may treat that name as the wall's unprivileged account."""
    system = os.path.join(BIN, "..")
    units = {n: open(os.path.join(system, "systemd", n)).read() for n in os.listdir(os.path.join(system, "systemd"))}
    assert "User=ledwall" in units["ledposter-web.service"] and "Group=ledwall" in units["ledposter-web.service"]
    assert "--user ledwall --group ledwall" in units["ledposter-engine.service"] and "Group=ledwall" in units["ledposter-engine.service"]
    for name, text in units.items():
        assert not re.search(r"^(User|Group)=(?!ledwall$)", text, re.M), name  # root otherwise
        assert not re.search(r"^ExecStart=.*--(user|group) (?!ledwall\b)", text, re.M), name
    for name in os.listdir(os.path.join(system, "polkit")):
        rule = open(os.path.join(system, "polkit", name)).read()
        assert 'subject.user != "ledwall"' in rule and '"ledposter"' not in rule, name
    assert "\nsvc_user=ledwall\n" in open(SYS).read()
    install = open(os.path.join(system, "install.sh")).read()
    assert "\nSVC_USER=ledwall\n" in install and "\nBUILD_USER=ledwall-build " in install
    assert updater.SERVICE_USER == "ledwall" and updater.BUILD_USER == "ledwall-build"
