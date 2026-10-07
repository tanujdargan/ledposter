"""/api/system/* and /api/health (milestone 8): software update and rollback, SSH mode, the
maintenance window, restarts, reboot and factory reset. Mounted by app.create_app; the OS
side is system.System, the root-side units are in system/systemd."""
import asyncio
import logging
import os
from typing import Literal, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import __version__, system
from .net import NetError

log = logging.getLogger("ledposter.sysapi")


class Health(BaseModel):
    ok: bool = True
    version: str
    release: Optional[str] = Field(None, description="Name of the release directory the app runs from")
    engine: bool = Field(description="The engine answers on its control socket")


class UpdateStatus(BaseModel):
    state: Literal["idle", "fetching", "building", "testing", "switching", "restarting", "checking",
                   "done", "failed", "rolled_back"] = "idle"
    action: Optional[Literal["update", "rollback"]] = None
    target: Optional[str] = None
    from_release: Optional[str] = None
    step: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    error: Optional[str] = None
    started: Optional[int] = None
    updated: Optional[int] = None


class SshSettings(BaseModel):
    mode: Literal["disabled", "key-only"]
    authorized_keys: str = Field("", max_length=20000, description="One public key per line")


class Maintenance(BaseModel):
    enabled: bool = True
    day: int = Field(6, ge=0, le=6, description="0 = Monday")
    hour: int = Field(4, ge=0, le=23, description="Local hour the window opens")


class SystemInfo(BaseModel):
    version: str
    release: Optional[str]
    previous: Optional[str] = Field(None, description="Release a rollback would switch to")
    overlay: bool = Field(description="Root filesystem is the read-only overlay")
    hostname: str
    update: UpdateStatus
    ssh: SshSettings
    maintenance: Maintenance
    maintenance_pending: bool = Field(description="A maintenance window is in progress (two reboots)")
    reset_plan: list[str] = Field(description="What a factory reset erases from the data dir")


class Updates(BaseModel):
    current: Optional[str]
    newer: list[str]
    all: list[str]


class UpdateRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Restart(BaseModel):
    what: Literal["web", "engine", "reboot"]


class FactoryReset(BaseModel):
    hostname: str = Field(description="Type the wall's hostname to confirm")


class Ok(BaseModel):
    ok: bool = True


def release_name(root):
    try:
        real = os.path.realpath(root)
        return os.path.basename(real) if "/releases/" in real else None
    except OSError:
        return None


def install(app, st, require_auth, apply_later):
    """Adds the routes. `st` is app.state (cfg, engine, system, net, library, save)."""
    sysm = st.system
    root = os.path.join(os.path.dirname(__file__), "..", "..")

    def status_model():
        s = sysm.update_status()
        return UpdateStatus(**{k: v for k, v in s.items() if k in UpdateStatus.model_fields})

    async def sync_and_apply():
        """Rewrites the unit input files from the config and starts the units whose input changed."""
        try:
            changed = await asyncio.to_thread(sysm.sync_files, st.cfg)
        except ValueError as e:
            raise HTTPException(422, str(e))
        for name in changed:
            unit = "firewall" if name == "firewall.rules" else "ssh" if name.startswith("ssh/") else None
            if unit:
                try:
                    await asyncio.to_thread(sysm.start, unit)
                except system.SystemError_ as e:
                    log.warning("%s not applied: %s", unit, e)
        return changed

    st.system_sync = sync_and_apply

    @app.get("/api/health", response_model=Health, tags=["system"],
             summary="Liveness for the updater and monitoring (no login)")
    def health():
        return Health(version=__version__, release=release_name(root), engine=st.engine.online)

    @app.get("/api/system", response_model=SystemInfo, tags=["system"])
    async def system_info(who=Depends(require_auth)):
        rel = sysm.releases()
        try:
            host = await asyncio.to_thread(st.net.hostname)
        except NetError:
            host = "ledposter"
        return SystemInfo(version=__version__, release=rel["current"] or release_name(root), previous=rel["previous"],
                          overlay=sysm.overlay_active(), hostname=host, update=status_model(),
                          ssh=SshSettings(**st.cfg["ssh"]), maintenance=Maintenance(**st.cfg["maintenance"]),
                          maintenance_pending=sysm.maintenance_pending(),
                          reset_plan=system.factory_reset_plan(st.data_dir)["delete"])

    @app.get("/api/system/update", response_model=UpdateStatus, tags=["system"], summary="Update progress (poll while running)")
    def update_status(who=Depends(require_auth)):
        return status_model()

    @app.get("/api/system/updates", response_model=Updates, tags=["system"],
             summary="Release tags on the repository newer than the running one")
    async def updates(who=Depends(require_auth)):
        current = sysm.releases()["current"] or release_name(root)
        try:
            r = await asyncio.to_thread(sysm.check_updates, current)
        except system.SystemError_ as e:
            raise HTTPException(503, str(e))
        return Updates(current=current, **r)

    @app.post("/api/system/update", response_model=UpdateStatus, status_code=202, tags=["system"],
              summary="Update to a release tag (fetch, build, test, switch, health check or roll back)")
    async def start_update(body: UpdateRequest, who=Depends(require_auth)):
        s = sysm.update_status()
        if s.get("state") in system.updater.STEPS["update"]:
            raise HTTPException(409, f"An update is already running ({s['state']})")
        try:
            await asyncio.to_thread(sysm.request_update, body.target)
        except (ValueError, system.SystemError_) as e:
            raise HTTPException(503 if isinstance(e, system.SystemError_) else 422, str(e))
        return UpdateStatus(state="fetching", action="update", target=body.target, step=1, total=6,
                            message=f"Starting update to {body.target}")

    @app.post("/api/system/rollback", response_model=UpdateStatus, status_code=202, tags=["system"],
              summary="Switch back to the previous release")
    async def rollback(who=Depends(require_auth)):
        s = sysm.update_status()
        if s.get("state") in system.updater.STEPS["update"]:
            raise HTTPException(409, f"An update is already running ({s['state']})")
        try:
            await asyncio.to_thread(sysm.request_rollback)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except system.SystemError_ as e:
            raise HTTPException(503, str(e))
        return UpdateStatus(state="switching", action="rollback", target=sysm.releases()["previous"], step=1, total=3,
                            message="Starting rollback")

    @app.put("/api/system/ssh", response_model=SshSettings, tags=["system"],
             summary="SSH off, or key-only with these authorized keys (applied by ledposter-ssh.service)")
    async def put_ssh(body: SshSettings, who=Depends(require_auth)):
        try:
            keys = system.parse_authorized_keys(body.authorized_keys)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if body.mode == "key-only" and not keys:
            raise HTTPException(422, "Key-only SSH needs at least one public key, or you would be locked out")
        st.cfg["ssh"] = {"mode": body.mode, "authorized_keys": "".join(k + "\n" for k in keys)}
        st.save()
        await sync_and_apply()
        return SshSettings(**st.cfg["ssh"])

    @app.put("/api/system/maintenance", response_model=Maintenance, tags=["system"],
             summary="Weekly security-update window (the wall reboots twice; docs/DEVIATIONS.md)")
    async def put_maintenance(body: Maintenance, who=Depends(require_auth)):
        st.cfg["maintenance"] = body.model_dump()
        st.save()
        await sync_and_apply()
        return body

    @app.post("/api/system/maintenance/run", response_model=Ok, tags=["system"],
              summary="Start the maintenance window now (disables the overlay, reboots, upgrades, re-enables, reboots)")
    async def run_maintenance(who=Depends(require_auth)):
        try:
            await asyncio.to_thread(sysm.request_maintenance)
        except system.SystemError_ as e:
            raise HTTPException(503, str(e))
        return Ok()

    @app.post("/api/system/restart", response_model=Ok, tags=["system"],
              summary="Restart the web app or the engine, or reboot the Pi (1.5s after this answer)")
    async def restart(body: Restart, who=Depends(require_auth)):
        async def go():
            if body.what == "reboot":
                await st.engine.show_text("Restarting...", 60)
                await asyncio.to_thread(sysm.reboot)
            else:
                await asyncio.to_thread(sysm.restart, body.what)
        apply_later(go)
        return Ok()

    @app.post("/api/system/factory_reset", response_model=Ok, tags=["system"],
              summary="Erase config, media, tokens and Wi-Fi profiles (keeps the setup card password); reboots into setup mode",
              description="`hostname` must be the wall's current hostname: the UI asks for it to be typed.")
    async def factory_reset(body: FactoryReset, who=Depends(require_auth)):
        try:
            host = await asyncio.to_thread(st.net.hostname)
        except NetError:
            host = "ledposter"
        if body.hostname.strip().lower() != host.lower():
            raise HTTPException(422, f"Type the hostname ({host}) to confirm")

        async def go():
            await st.engine.show_text("Factory reset...", 300)
            await asyncio.to_thread(sysm.request_factory_reset)
        apply_later(go)
        return Ok()
