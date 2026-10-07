"""First-time Wi-Fi setup (docs/spec.md): the LEDWall-Setup hotspot and captive portal.

State machine, one radio: scan first (the list is cached), then raise the hotspot. Joining
drops the hotspot, so the phone loses this server mid-flow; the wall shows every step as
text through the engine (`show_text`) and the hotspot comes back on failure.

Triggers: no network within `setup_timeout_s` of the web app starting, the engine's
`wifi_setup_requested` marker changing (button held 10s), or Settings. A button double
press (the engine's `button_double_count` going up) shows the hostname and IP again.
"""
import asyncio
import logging
import os
import secrets
import time

from .net import NetError

log = logging.getLogger("ledposter.wifisetup")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/O, 1/l/i: it is typed from a card
TEXT_REFRESH_S = 50
JOINED_LINGER_S = 120  # how long the "joined" result stays readable for a page that polls it
BUTTON_RETRY_S = 30


def setup_password(data_dir):
    """$LEDPOSTER_DATA/setup-password: the hotspot password printed on the card. The
    installer (M8) creates it; a missing file is created here so a dev box works too."""
    p = os.path.join(data_dir, "setup-password")
    try:
        with open(p) as f:
            pw = f.read().strip()
        if len(pw) >= 8:
            return pw
    except OSError:
        pass
    pw = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(10))
    os.makedirs(data_dir, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    log.warning("setup hotspot password created at %s; print it on the card", p)
    return pw


class WifiSetup:
    def __init__(self, net, show_text, data_dir, ssid="LEDWall-Setup", timeout_s=60, show_ip_s=10,
                 sleep=asyncio.sleep, to_thread=asyncio.to_thread):
        self.net, self.show_text, self.ssid, self.timeout_s, self.show_ip_s = net, show_text, ssid, timeout_s, show_ip_s
        self.sleep, self.to_thread = sleep, to_thread
        self.password = setup_password(data_dir)
        self.state = {"mode": "off", "hotspot_up": False, "networks": [], "scanned_at": None, "target": None,
                      "step": 0, "reason": None, "ip": None, "hostname": None, "trigger": None}
        self._lock = asyncio.Lock()
        self._button_at = -BUTTON_RETRY_S
        self._text_at = 0
        self._join_task = None
        self._double_seen = None  # last button_double_count seen

    # -- helpers --

    async def _net(self, fn, *args):
        return await self.to_thread(fn, *args)

    async def _text(self, text, seconds):
        self._text_at = time.monotonic()
        try:
            await self.show_text(text, seconds)
        except Exception as e:  # the wall text is best effort; never breaks the flow
            log.info("show_text failed: %s", e)

    def _set(self, **kw):
        self.state.update(kw)

    async def _hotspot_text(self):
        await self._text(f"Wi-Fi setup\nJoin {self.ssid}\npassword: on the card\nthen open http://{self.net.hotspot_ip}", 60)

    # -- background loop --

    async def loop(self, engine):
        """Boot check, IP display, button watch and hotspot text refresh. Runs for the life
        of the app; every error is logged and the loop carries on."""
        t0 = time.monotonic()
        booted = False
        while True:
            try:
                if not booted:
                    booted = await self._boot_tick(t0, engine)
                await self._button_tick(engine)
                await self._double_tick(engine)
                if self.state["hotspot_up"] and time.monotonic() - self._text_at > TEXT_REFRESH_S:
                    await self._hotspot_text()
                if self.state["mode"] == "joined" and time.monotonic() - self._text_at > JOINED_LINGER_S:
                    self._set(mode="off", step=0, trigger=None)  # the result was shown; back to normal
            except Exception:
                log.exception("wifi setup loop")
            await self.sleep(2)

    async def _boot_tick(self, t0, engine):
        """True once boot handling is done (IP shown, or setup started)."""
        if self.state["hotspot_up"]:
            return True
        try:
            connected = await self._net(self.net.connected)
        except NetError as e:
            log.warning("network check failed, no setup hotspot: %s", e)
            return True
        if connected:
            if not engine.online:
                return False  # wait for the engine so the IP is seen
            await self.show_ip()
            return True
        if time.monotonic() - t0 >= self.timeout_s:
            await self.start("No known Wi-Fi network at boot")
            return True
        return False

    async def _button_tick(self, engine):
        """A 10s hold sets `wifi_setup_requested`; we raise the hotspot and ack it. The flag
        is only cleared by the ack, so a failed ack is retried on the next poll."""
        if not engine.setup_request(engine.status):
            return
        if time.monotonic() - self._button_at < BUTTON_RETRY_S:
            return  # the hotspot could not come up a moment ago; do not hammer NetworkManager
        await self.start("Button held")
        if not self.state["hotspot_up"]:
            self._button_at = time.monotonic()
        if await engine.ack_wifi_setup() and engine.status:
            engine.status["wifi_setup_requested"] = False  # until the poller refreshes it

    async def _double_tick(self, engine):
        """Each new double press shows the address: the IP, or the hotspot's while setup runs.
        The first count seen (and a lower one after an engine restart) is the baseline."""
        n = (engine.status or {}).get("button_double_count")
        if not isinstance(n, int):
            return
        seen, self._double_seen = self._double_seen, n
        if seen is None or n <= seen:
            return
        await (self._hotspot_text() if self.state["hotspot_up"] else self.show_ip())

    async def show_ip(self):
        try:
            st = await self._net(self.net.status)
        except NetError as e:
            log.info("show_ip: %s", e)
            return
        if st.get("address"):
            self._set(hostname=st["hostname"], ip=st["address"])
            await self._text(f"{st['hostname']}.local\n{st['address']}", self.show_ip_s)

    # -- actions --

    async def start(self, trigger="requested"):
        """Scan, then raise the hotspot. No-op when it is already up."""
        async with self._lock:
            if self.state["hotspot_up"]:
                return
            log.info("wifi setup: %s", trigger)
            self._set(mode="starting", trigger=trigger, reason=None, target=None, step=0)
            await self._scan()
            try:
                await self._net(self.net.hotspot_up, self.password)
            except NetError as e:
                log.error("hotspot failed: %s", e)
                self._set(mode="off", reason=f"Hotspot failed: {e}")
                return
            self._set(mode="hotspot", hotspot_up=True)
            await self._hotspot_text()

    async def stop(self):
        async with self._lock:
            if self.state["hotspot_up"]:
                await self._net(self.net.hotspot_down)
            self._set(mode="off", hotspot_up=False, step=0)

    async def _scan(self):
        try:
            nets = await self._net(self.net.scan)
            self._set(networks=nets, scanned_at=time.time())
        except NetError as e:
            log.warning("scan failed: %s", e)
            if not self.state["networks"]:
                self._set(reason=f"Scan failed: {e}")

    async def rescan(self):
        """Fresh scan. With the hotspot up this drops it for the duration (one radio)."""
        async with self._lock:
            if not self.state["hotspot_up"]:
                await self._scan()
                return self.state["networks"]
            await self._net(self.net.hotspot_down)
            self._set(hotspot_up=False, mode="scanning")
            try:
                await self._scan()
            finally:
                try:
                    await self._net(self.net.hotspot_up, self.password)
                    self._set(hotspot_up=True, mode="hotspot")
                    await self._hotspot_text()
                except NetError as e:
                    log.error("hotspot did not come back: %s", e)
                    self._set(mode="off", reason=f"Hotspot failed: {e}")
            return self.state["networks"]

    def join(self, ssid, password=None, hidden=False, delay=1.5):
        """Start joining in the background and return at once, so the phone gets its HTTP
        response before the hotspot goes away. Progress is in `state`."""
        if self._join_task and not self._join_task.done():
            raise NetError("Already joining a network")
        self._set(mode="joining", target=ssid, step=1, reason=None, ip=None)
        self._join_task = asyncio.create_task(self._join(ssid, password, hidden, delay))
        return self._join_task

    async def _join(self, ssid, password, hidden, delay):
        await self.sleep(delay)
        async with self._lock:
            was_hotspot = self.state["hotspot_up"]
            if was_hotspot:
                await self._net(self.net.hotspot_down)
                self._set(hotspot_up=False)
            await self._text(f"Joining\n{ssid}...", 60)
            try:
                st = await self._net(self.net.join, ssid, password, hidden)
            except NetError as e:
                reason = str(e)
                log.warning("join %s failed: %s", ssid, reason)
                self._set(mode="failed", reason=reason, step=1)
                if was_hotspot:
                    try:
                        await self._net(self.net.hotspot_up, self.password)
                        self._set(hotspot_up=True)
                    except NetError as e2:
                        log.error("hotspot did not come back: %s", e2)
                await self._text(f"Wi-Fi failed\n{reason}" + ("\nSetup hotspot is back" if was_hotspot else ""), 30)
                return
            self._set(step=2)
            ip, host = st.get("address"), st.get("hostname")
            if not ip:
                self._set(mode="failed", reason="Joined, but no IP address yet", step=2)
                await self._text(f"Joined {ssid}\nno IP address yet", 30)
                return
            self._set(mode="joined", step=3, ip=ip, hostname=host)
            await self._text(f"Joined {ssid}\n{ip}\nhttp://{host}.local", 15)

    def public(self, first_run=False):
        """What the setup page polls."""
        s = dict(self.state)
        if not s["hostname"]:
            try:
                s["hostname"] = self.state["hostname"] = self.net.hostname()
            except NetError:
                pass
        s.update(ssid=self.ssid, hotspot_ip=self.net.hotspot_ip, first_run=first_run)
        return s
