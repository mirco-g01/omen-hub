"""`omen-hub --adaptive`: the adaptive brightness service (omen-hub-luce.service).

Every `interval_s` seconds: one webcam shot (omen_hub/camlight.py), the room
light on the calibrated 0-100 % scale, and through powerdevil the screen and
keyboard levels that the two curves give for it (omen_hub/adaptive.py).

It stays out of the way:

* **Camera.** No shot while anyone else has any node of the camera module
  open (video call, howdy on the IR node, the hub's calibration), and a shot
  in progress is dropped the moment someone opens one (inotify). The camera
  is claimed before any of its controls is touched, and handed back in
  automatic exposure. No shot either with the lid closed, the session locked
  or you away for 10 minutes (input idle time from omen-fxd).
* **powerdevil.** Brightness goes through its D-Bus API with our own
  context, so the OSD stays quiet, omen-backlight-sync keeps mirroring the
  EC register, and powerdevil's idle dimming (a separate ratio on the
  screen) is not undone. A change made by anyone else -- the brightness
  keys, the panel slider, the hub -- pauses the automatic control of that
  output (screen and keyboard apart) for `pause_hours`, or until the room
  light moves `resume_delta` points; the tray switch does the same for both.
  The level powerdevil sets when you plug or unplug is not your choice: it
  pauses nothing, and the room is measured again.
* **Keyboard.** Only touched while you are typing (idle < 1 minute): away,
  powerdevil may have dimmed it, and nobody needs it lit.

Signals: SIGHUP rereads the settings (and re-applies the curves to the last
reading, no photo), SIGUSR1 measures now, SIGUSR2 carries out the request in
adaptive.REQUEST (pause/resume, from `omen-hub --luce`, the tray switch).
"""

from __future__ import annotations

import glob
import os
import signal
import socket
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QCoreApplication, QMetaType, QObject, QSocketNotifier, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBus, QDBusArgument, QDBusConnection, QDBusMessage

from . import adaptive as A
from . import camlight

SB = ("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness", "org.kde.ScreenBrightness")
SB_DISPLAY_IFACE = "org.kde.ScreenBrightness.Display"
KBD = ("org.kde.Solid.PowerManagement", "/org/kde/Solid/PowerManagement/Actions/KeyboardBrightnessControl",
       "org.kde.Solid.PowerManagement.Actions.KeyboardBrightnessControl")
UPOWER = ("org.freedesktop.UPower", "/org/freedesktop/UPower", "org.freedesktop.UPower")
LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
SAVER = ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver")
PROPS = "org.freedesktop.DBus.Properties"
SUPPRESS_OSD = 0x1


def log(msg: str):
    print(msg, flush=True)


def lid_closed() -> bool:
    for f in glob.glob("/proc/acpi/button/lid/*/state"):
        try:
            if "closed" in open(f).read():
                return True
        except OSError:
            pass
    return False


def idle_seconds() -> float | None:
    """Seconds since the last key or mouse event, from omen-fxd (it reads
    /dev/input as root; Wayland gives a session client no idle time).
    OMEN_HUB_LUCE_IGNORE_IDLE=1 (tests only) behaves as if you were typing."""
    if os.environ.get("OMEN_HUB_LUCE_IGNORE_IDLE") == "1":
        return 0.0
    try:
        from . import desktop
        idle = (desktop.fx({"cmd": "status"}, timeout=1.0).get("status") or {}).get("idle") or {}
    except Exception:
        return None
    if not idle.get("available", False) or idle.get("seconds") is None:
        return None
    return float(idle["seconds"])


class Service(QObject):
    captured = pyqtSignal(object, object)       # (Reading | None, Exception | None), from the camera thread

    def __init__(self):
        super().__init__()
        self.cfg = A.load()
        self.bus = QDBusConnection.sessionBus()
        self.sysbus = QDBusConnection.systemBus()
        self.dev = camlight.find_device() or camlight.DEFAULT_DEVICE
        self.watch = camlight.OpenWatch(camlight.module_nodes(self.dev))
        self._inotify = QSocketNotifier(self.watch.fd, QSocketNotifier.Type.Read, self)
        self._inotify.activated.connect(self._on_inotify)
        self.abort_r, self.abort_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.thread: threading.Thread | None = None
        self.capture_end = 0.0                    # monotonic: our USB wake-up is not a stranger
        self.captured.connect(self._on_captured)

        self.last: dict | None = None            # the last reading
        self.exposure: int | None = None         # 100 µs units, planned for the next shot
        self.dark = A.DarkMemory()
        self._capped = False
        self._requested = False                   # "Misura ora" pending: a full shot, reported in the log
        self._this_requested = False              # ...and the shot in progress is that one
        self.pauses = A.Pauses.load()
        self.sent = {o: None for o in A.OUTPUTS}  # last % we asked for
        # room light (0-100) of the measurement that set each current level
        self.anchor: dict[str, float | None] = {o: None for o in A.OUTPUTS}
        self.kbd_ours_until = 0.0                 # keyboard changes before this are our ramp
        self.skipped: dict[str, str] = {}         # output -> why it was left alone
        self.power_changed_at = 0.0
        self.state, self.reason = "avvio", ""
        self._logged_reason = None
        self.next_at: float | None = None

        self.display, self.display_max = None, 10000
        self._find_display()
        self.kbd_max = 100
        try:
            self.kbd_max = int(self._call(self.bus, *KBD, "keyboardBrightnessMax")[0]) or 100
        except Exception:
            pass
        self.on_battery = self._read_on_battery()
        self.locked = self._read_locked()

        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self.tick)
        self.ramps: dict[str, dict] = {}
        for out in A.OUTPUTS:
            t = QTimer(self)
            t.timeout.connect(lambda o=out: self._ramp_step(o))
            self.ramps[out] = {"timer": t, "values": []}

        self._connect_signals()
        self._unix_signals()
        log(f"avvio: fotocamera {self.dev} (nodi {', '.join(self.watch.nodes.values())}), "
            f"schermo {self.display or 'non trovato'}, "
            f"{'batteria' if self.on_battery else 'rete'}, ogni {self.cfg['interval_s']} s")
        self.schedule(2.0)

    # -- D-Bus plumbing ---------------------------------------------------------------------
    def _call(self, bus, service, path, iface, method, *args):
        msg = QDBusMessage.createMethodCall(service, path, iface, method)
        if args:
            msg.setArguments(list(args))
        reply = bus.call(msg, QDBus.CallMode.Block, 3000)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise RuntimeError(reply.errorMessage())
        return reply.arguments()

    def _prop(self, bus, service, path, iface, name):
        return self._call(bus, service, path, PROPS, "Get", iface, name)[0]

    def _connect_signals(self):
        ok = [
            self.bus.connect(SB[0], SB[1], SB[2], "BrightnessChanged", self._on_brightness),
            self.bus.connect(SB[0], SB[1], SB[2], "DisplayAdded", self._on_displays),
            self.bus.connect(SB[0], SB[1], SB[2], "DisplayRemoved", self._on_displays),
            self.bus.connect(SB[0], SB[1], SB[2], "BrightnessRangeChanged", self._on_displays),
            self.bus.connect(KBD[0], KBD[1], KBD[2], "keyboardBrightnessChanged", self._on_kbd),
            self.bus.connect(SAVER[0], SAVER[1], SAVER[2], "ActiveChanged", self._on_lock),
            self.sysbus.connect(UPOWER[0], UPOWER[1], PROPS, "PropertiesChanged", self._on_upower),
            self.sysbus.connect(LOGIN1[0], LOGIN1[1], LOGIN1[2], "PrepareForSleep", self._on_sleep),
        ]
        if not all(ok):
            log(f"attenzione: {ok.count(False)} segnali D-Bus non collegati")

    def _find_display(self):
        self.display = None
        try:
            names = self._prop(self.bus, SB[0], SB[1], SB[2], "DisplaysDBusNames")
            for name in names:
                path = f"{SB[1]}/{name}"
                if self._prop(self.bus, SB[0], path, SB_DISPLAY_IFACE, "IsInternal"):
                    self.display = name
                    self.display_max = int(self._prop(self.bus, SB[0], path, SB_DISPLAY_IFACE,
                                                      "MaxBrightness")) or 10000
                    return
        except Exception as exc:
            log(f"schermo interno non trovato: {exc}")

    def _read_on_battery(self) -> bool:
        try:
            return bool(self._prop(self.sysbus, *UPOWER, "OnBattery"))
        except Exception:
            for f in glob.glob("/sys/class/power_supply/A*/online"):
                try:
                    return open(f).read().strip() == "0"
                except OSError:
                    pass
            return False

    def _read_locked(self) -> bool:
        try:
            return bool(self._call(self.bus, *SAVER, "GetActive")[0])
        except Exception:
            return False

    def screen_pct(self) -> float | None:
        if not self.display:
            self._find_display()
        if not self.display:
            return None
        try:
            v = self._prop(self.bus, SB[0], f"{SB[1]}/{self.display}", SB_DISPLAY_IFACE, "Brightness")
            return int(v) / self.display_max * 100
        except Exception:
            return None

    def kbd_pct(self) -> float | None:
        try:
            return int(self._call(self.bus, *KBD, "keyboardBrightness")[0]) / self.kbd_max * 100
        except Exception:
            return None

    def _set_screen(self, pct: float):
        value = max(1, round(pct / 100 * self.display_max))
        self._call(self.bus, SB[0], f"{SB[1]}/{self.display}", SB_DISPLAY_IFACE, "SetBrightnessWithContext",
                   int(value), QDBusArgument(SUPPRESS_OSD, QMetaType.Type.UInt.value), A.CONTEXT)

    def _set_kbd(self, pct: float):
        self.sent["keyboard"] = pct
        self._call(self.bus, *KBD, "setKeyboardBrightnessSilent", int(round(pct / 100 * self.kbd_max)))

    # -- unix signals -----------------------------------------------------------------------
    def _unix_signals(self):
        self._sig_r, self._sig_w = socket.socketpair()
        for s in (self._sig_r, self._sig_w):
            s.setblocking(False)
        signal.set_wakeup_fd(self._sig_w.fileno())
        for sig in (signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: None)      # the byte on the wakeup fd does the work
        self._sig_notifier = QSocketNotifier(self._sig_r.fileno(), QSocketNotifier.Type.Read, self)
        self._sig_notifier.activated.connect(self._on_unix_signal)

    def _on_unix_signal(self, *_):
        try:
            data = self._sig_r.recv(64)
        except BlockingIOError:
            return
        for num in data:
            if num == signal.SIGHUP:
                self.reload()
            elif num == signal.SIGUSR1:
                log("misura richiesta")
                self._requested = True
                self.schedule(0)
            elif num == signal.SIGUSR2:
                self.on_request()
            elif num in (signal.SIGTERM, signal.SIGINT):
                self.quit()

    # -- schedule ----------------------------------------------------------------------------
    def wanted(self) -> list[str]:
        return [o for o in A.OUTPUTS if A.is_active(self.cfg, o, self.on_battery)]

    def schedule(self, seconds: float):
        self.timer.start(int(max(0.0, seconds) * 1000))
        self.next_at = time.time() + max(0.0, seconds)

    def _idle_why(self) -> str:
        out = [o for o in A.OUTPUTS if self.cfg[o]["enabled"]]
        if not out:
            return "schermo e tastiera disattivati"
        return "attiva solo a batteria: ora sei a rete"

    def blocked(self) -> str | None:
        if lid_closed():
            return "coperchio chiuso"
        if self.locked:
            return "sessione bloccata"
        busy = self.camera_busy()
        if busy:
            return busy
        idle = idle_seconds()
        if idle is not None and idle >= A.AWAY_S:
            return f"sei via da {int(idle // 60)} min"
        if not os.path.exists(self.dev):
            found = camlight.find_device(self.watch)
            if not found:
                return "fotocamera non trovata (spenta?)"
            self.dev = found
            self.watch.rewatch(camlight.module_nodes(found))
        return None

    def camera_busy(self) -> str | None:
        """Why the camera is someone else's right now, or None."""
        self.watch.read()
        if time.monotonic() - self.watch.last_other_open < A.STRANGER_GRACE_S:
            return "fotocamera appena aperta da un'altra applicazione"
        if time.monotonic() - self.capture_end > 3 and camlight.streaming(self.dev):
            return "fotocamera in uso da un'altra applicazione"
        if camlight.holders(camlight.module_nodes(self.dev)):
            return "fotocamera aperta da un'altra applicazione"
        return None

    def tick(self):
        self.next_at = None
        if self.thread is not None:
            return
        self.check_pauses(None)
        if not self.wanted():
            self.set_state("pausa", self._idle_why())
            return                           # power change, settings or a request wake us
        why = self.blocked()
        if why:
            if self._requested:
                log(f"misura richiesta non fatta: {why}")
                self._requested = False
            self.schedule(self.cfg["interval_s"])
            self.set_state("attesa", why)
            return
        while True:                          # a stale abort byte would kill this shot
            try:
                os.read(self.abort_r, 64)
            except BlockingIOError:
                break
        self.watch.rewatch(camlight.module_nodes(self.dev))
        dev, ms, start = self.dev, self.cfg["max_exposure_ms"], self.exposure
        # a requested measurement is a real one: no one-frame check reusing the last long shot
        self._this_requested, self._requested = self._requested, False
        self._capped = not self._this_requested and self.dark.short_only(time.time(), start)
        cap = camlight.FRAME_FLOOR if self._capped else None
        self.thread = threading.Thread(target=self._capture, args=(dev, ms, start, cap),
                                       name="scatto", daemon=True)
        self.thread.start()

    def _capture(self, dev, max_ms, start, cap):
        try:
            reading = camlight.measure(dev, max_ms, start, abort_fd=self.abort_r, watch=self.watch,
                                       cap_exposure=cap)
        except Exception as exc:              # reported in the main thread
            self.captured.emit(None, exc)
        else:
            self.captured.emit(reading, None)

    def _on_inotify(self, *_):
        if self.watch.read():
            self._abort_shot()

    def _abort_shot(self):
        if self.thread is not None:
            try:
                os.write(self.abort_w, b"x")
            except OSError:
                pass

    @pyqtSlot(object, object)
    def _on_captured(self, reading, err):
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.thread = None
        self.capture_end = time.monotonic()
        interval = self.cfg["interval_s"]
        if err is not None:
            if isinstance(err, camlight.Busy):
                self.set_state("attesa", "fotocamera occupata")
            elif isinstance(err, camlight.Aborted):
                self.set_state("attesa", "scatto interrotto: un'altra applicazione ha aperto la fotocamera")
            elif isinstance(err, (camlight.Unavailable, TimeoutError)):
                self.set_state("attesa", str(err))
            else:
                log("errore nello scatto:\n" + "".join(traceback.format_exception(err)))
                self.set_state("errore", f"{type(err).__name__}: {err}")
            if self._this_requested:
                log(f"misura richiesta non riuscita: {self.reason}")
            self._this_requested = False
            self.schedule(interval)
            self.publish()
            return
        too_dark = reading.mean - camlight.BLACK < camlight.V_LOW
        exposure = int(round(reading.exposure_ms * 10))
        lum, source = self.dark.settle(time.time(), self._capped, too_dark, reading.lum, exposure)
        if not (self._capped and too_dark):      # a dark check keeps the long plan
            self.exposure = reading.next_exposure or exposure
        cal = self.cfg["calibration"]
        self.last = {"at": time.time(), "lum": lum, "mean": reading.mean, "source": source,
                     "long_at": self.dark.at, "exposure_ms": reading.exposure_ms, "on_s": reading.on_s,
                     "next_exposure_ms": self.exposure / 10,
                     "shots": len(reading.frames), "saturated": reading.saturated,
                     "x": camlight.ambient(lum, cal["dark"], cal["bright"])}
        requested, self._this_requested = self._this_requested, False
        changed = self.apply()
        if requested and not changed:
            r = self.last
            moved = ", ".join(f"{A.NAMES[o]} sospeso" if self.pauses.paused(o) else
                              f"{A.NAMES[o]} impostato con luce {self.anchor[o]:.0f} %"
                              for o in self.wanted() if self.pauses.paused(o) or self.anchor[o] is not None)
            log(f"misura richiesta: luce {r['x']:.0f} % (stima {r['lum']:.3g}, {r['exposure_ms']:g} ms, "
                f"{r['on_s']:.2f} s), livelli invariati" + (f" ({moved}; soglia {self.cfg['min_delta']} punti)"
                                                            if moved else ""))
        # a request that came in during this shot gets its own, at once
        self.schedule(0 if self._requested else interval)
        self.set_state("ok", "")

    # -- applying -------------------------------------------------------------------------------
    def apply(self, force: bool = False):
        """Bring each active output to its curve. A level only moves once the
        room light is `min_delta` points away from the measurement that set it
        (its anchor), so slow drifts add up; `force` skips that test (new
        curve, a resumed output)."""
        if not self.last:
            return False
        x = self.last["x"]
        done = []
        self.skipped = {}
        self.check_pauses(x)
        if any(self.pauses.paused(o) for o in A.OUTPUTS):
            self.pauses.save()                # the first measurement after a pause is its start
        for out in self.wanted():
            if self.pauses.paused(out):
                self.skipped[out] = "sospesa"
                continue
            want = A.target(self.cfg, out, x, A.profile_of(self.on_battery))
            try:
                if out == "keyboard":
                    idle = idle_seconds()
                    if idle is not None and idle >= A.KBD_ACTIVE_S:
                        self.skipped[out] = "non stai scrivendo"
                        continue
                cur = self.screen_pct() if out == "screen" else self.kbd_pct()
                if cur is None:
                    self.skipped[out] = f"{A.NAMES[out]} non raggiungibile"
                    continue
                if not force and not A.light_moved(x, self.anchor[out], self.cfg["min_delta"]):
                    continue                      # the room has not moved enough: the level stays
                if not A.level_differs(want, cur):
                    if self.anchor[out] is None:
                        self.anchor[out] = x      # already right: this measurement holds it
                        self.sent[out] = cur
                    continue
                self._ramp_to(out, cur, want)
                self.anchor[out] = x
                self.sent[out] = want
                done.append(f"{A.NAMES[out]} {cur:.0f}→{want:.0f} %")
            except Exception as exc:
                self.skipped[out] = str(exc)
        if done:
            r = self.last
            log(f"luce {x:.0f} % (stima {r['lum']:.3g}, {r['exposure_ms']:g} ms, "
                f"{r['shots']} scatti, {r['on_s']:.2f} s): " + ", ".join(done)
                + f" in {self.cfg['transition_s']:g} s")
        return bool(done)

    def _ramp_to(self, out: str, start: float, end: float):
        quantum = 0.1 if out == "screen" else 100 / self.kbd_max
        values, pause = A.ramp(start, end, self.cfg["transition_s"], quantum)
        r = self.ramps[out]
        r["timer"].stop()
        r["values"] = values
        if out == "keyboard":                 # its echoes must not look like your hand
            self.kbd_ours_until = time.monotonic() + len(values) * pause + 1.5
        self._ramp_step(out)
        if r["values"]:
            r["timer"].start(max(10, int(pause * 1000)))

    def _ramp_step(self, out: str):
        r = self.ramps[out]
        if not r["values"]:
            r["timer"].stop()
            return
        try:
            (self._set_screen if out == "screen" else self._set_kbd)(r["values"].pop(0))
        except Exception as exc:
            log(f"luminosità {A.NAMES[out]} non impostata: {exc}")
            r["values"] = []
        if not r["values"]:
            r["timer"].stop()

    # -- what others do -----------------------------------------------------------------------
    @pyqtSlot(QDBusMessage)
    def _on_brightness(self, msg):
        try:
            name, value, _client, context = msg.arguments()[:4]
        except ValueError:
            return
        if name != self.display or context == A.CONTEXT:
            return
        pct = int(value) / self.display_max * 100
        self.ramps["screen"]["timer"].stop()  # you took over: no more steps of ours
        self.ramps["screen"]["values"] = []
        self._by_hand("screen", pct)

    @pyqtSlot(QDBusMessage)
    def _on_kbd(self, msg):
        try:
            pct = int(msg.arguments()[0]) / self.kbd_max * 100
        except (IndexError, TypeError, ValueError):
            return
        if time.monotonic() < self.kbd_ours_until:
            return                            # our own ramp coming back
        idle = idle_seconds()
        if idle is not None and idle >= 20:
            return                            # powerdevil dimming it while you are away
        if self.sent["keyboard"] is not None and abs(pct - self.sent["keyboard"]) <= 1.5:
            return                            # ...and putting back our level when you return
        self._by_hand("keyboard", pct)

    def _by_hand(self, out: str, pct: float):
        """You set this output yourself: its automatic control pauses."""
        if time.monotonic() - self.power_changed_at < A.PROFILE_LOAD_S:
            return                            # powerdevil loading the new power profile
        if out not in self.wanted():
            return                            # nothing of ours to pause
        self.sent[out] = pct
        if not self.pauses.paused(out):
            self.pause([out], "a mano", f"{A.NAMES[out]} portato a mano a {pct:.0f} %")
        self.publish()

    # -- pauses ---------------------------------------------------------------------------------
    def pause(self, outs: list[str], why: str, what: str):
        x = self.last["x"] if self.last and time.time() - self.last["at"] < 15 * 60 else None
        for out in outs:
            self.pauses.pause(out, time.time(), x, why)
        self.pauses.save()
        h = self.cfg["pause_hours"]
        log(f"{what}: regolazione automatica sospesa per {h:g} h"
            + (f" o finché la luce non cambia di {self.cfg['resume_delta']} punti"
               if self.cfg["resume_delta"] < A.RESUME_RANGE[1] else ""))

    def resume(self, outs: list[str], why: str):
        outs = [o for o in outs if self.pauses.paused(o)]
        if not outs:
            return
        for out in outs:
            self.pauses.resume(out)
            self.anchor[out] = None           # back to the curve at the next measurement
        self.pauses.save()
        log(", ".join(A.NAMES[o] for o in outs) + f": regolazione automatica ripresa ({why})")

    def check_pauses(self, x: float | None):
        for out in A.OUTPUTS:
            why = self.pauses.due(out, time.time(), x, self.cfg["pause_hours"], self.cfg["resume_delta"])
            if why:
                self.resume([out], why)
                if x is None and self.wanted():
                    self.schedule(0)          # the timer ran out: measure and apply now

    def on_request(self):
        req = A.read_request()
        if not req:
            return
        if req["azione"] == "pausa":
            outs = [o for o in req["uscite"] if not self.pauses.paused(o)]
            if outs:
                self.pause(outs, "dal pannello", "spenta dal pannello")
        else:
            self.resume(req["uscite"], "dal pannello")
            if self.last and time.time() - self.last["at"] < 15 * 60:
                self.apply(force=True)
            elif self.wanted():
                self.schedule(0)
        self.publish()

    @pyqtSlot(QDBusMessage)
    def _on_displays(self, _msg):
        self._find_display()

    @pyqtSlot(QDBusMessage)
    def _on_lock(self, msg):
        try:
            self.locked = bool(msg.arguments()[0])
        except IndexError:
            return
        if not self.locked:
            self.schedule(2.0)                # back at the laptop: the room may have changed

    @pyqtSlot(QDBusMessage)
    def _on_sleep(self, msg):
        try:
            going = bool(msg.arguments()[0])
        except IndexError:
            return
        if going:
            self.timer.stop()
            self._abort_shot()
        else:
            self.schedule(4.0)

    @pyqtSlot(QDBusMessage)
    def _on_upower(self, _msg):
        ob = self._read_on_battery()
        if ob == self.on_battery:
            return
        self.on_battery = ob
        self.power_changed_at = time.monotonic()
        self.anchor = {o: None for o in A.OUTPUTS}    # powerdevil has just set its own levels
        log(f"alimentazione: {'batteria' if ob else 'rete'}")
        if self.wanted():
            self.schedule(2.5)                # after powerdevil has applied its profile
        else:
            self.timer.stop()
            self.next_at = None
            self.set_state("pausa", self._idle_why())

    # -- requests -------------------------------------------------------------------------------
    def reload(self):
        old = self.cfg
        self.cfg = A.load()
        if old["max_exposure_ms"] != self.cfg["max_exposure_ms"]:
            self.exposure = None
            self.dark = A.DarkMemory()
        if self.last:
            cal = self.cfg["calibration"]
            self.last["x"] = camlight.ambient(self.last["lum"], cal["dark"], cal["bright"])
            if time.time() - self.last["at"] < 15 * 60:
                self.apply(force=True)        # a new curve shows at once, no photo needed
        log(f"impostazioni rilette (ogni {self.cfg['interval_s']} s, esposizione max "
            f"{self.cfg['max_exposure_ms']} ms, transizione {self.cfg['transition_s']:g} s, "
            f"variazione minima {self.cfg['min_delta']} punti, pausa a mano {self.cfg['pause_hours']:g} h "
            f"o {self.cfg['resume_delta']} punti)")
        if self.wanted():
            if not self.timer.isActive() or self.timer.remainingTime() > self.cfg["interval_s"] * 1000:
                self.schedule(1.0 if not self.last else self.cfg["interval_s"])
        else:
            self.timer.stop()
            self.next_at = None
            self.set_state("pausa", self._idle_why())
        self.publish()

    def quit(self):
        log("arresto")
        self.timer.stop()
        if self.thread is not None:
            self._abort_shot()
            self.thread.join(timeout=3)       # the camera goes back to auto exposure
        self.set_state("fermo", "")
        QCoreApplication.quit()

    # -- status -------------------------------------------------------------------------------
    def set_state(self, state: str, reason: str):
        self.state, self.reason = state, reason
        key = (state, reason)
        if key != self._logged_reason and state in ("attesa", "pausa", "errore"):
            log(f"{state}: {reason}")
        self._logged_reason = key
        self.publish()

    def publish(self):
        x = self.last["x"] if self.last else None
        outs = {}
        for out in A.OUTPUTS:
            active = A.is_active(self.cfg, out, self.on_battery)
            outs[out] = {
                "active": active,
                "target": (A.target(self.cfg, out, x, A.profile_of(self.on_battery))
                           if (active and x is not None) else None),
                "anchor": self.anchor[out],
                "paused": (dict(self.pauses.d[out], until=self.pauses.until(out, self.cfg["pause_hours"]))
                           if self.pauses.paused(out) else None),
                "level": self.sent[out] if active else None,
                "skipped": self.skipped.get(out),
            }
        A.write_status({
            "state": self.state, "reason": self.reason, "on_battery": self.on_battery,
            "profile": A.profile_of(self.on_battery),
            "device": self.dev,
            "reading": self.last, "next_at": self.next_at,
            "screen": outs["screen"], "keyboard": outs["keyboard"],
        })


def main() -> int:
    app = QCoreApplication(sys.argv[:1])
    if not QDBusConnection.sessionBus().isConnected():
        log("nessun bus di sessione: il servizio va avviato dentro la sessione grafica")
        return 1
    service = Service()                       # noqa: F841 -- lives as long as the loop
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
