"""Adaptive brightness: settings, curves, light model, open watch.

No camera and no D-Bus needed. The camera test at the end runs only with
OMEN_HUB_CAMERA_TEST=1 (it lights the camera LED for a second).
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from omen_hub import adaptive as A  # noqa: E402
from omen_hub import camlight as C  # noqa: E402
from omen_hub import trayapplet  # noqa: E402


class SettingsTest(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x" / "luce.json"
            self.assertEqual(A.load(p), A.validate(None))
            cfg = A.load(p)
            cfg["screen"]["when"] = "battery"
            cfg["keyboard"]["curves"]["ac"] = [[10, 70], [60, 0]]
            A.save(cfg, p)
            again = A.load(p)
            self.assertEqual(again["screen"]["when"], "battery")
            self.assertEqual(again["keyboard"]["curves"]["ac"], [[10, 70], [60, 0]])
            self.assertEqual(again["keyboard"]["curves"]["battery"], A.DEFAULTS["keyboard"]["curves"]["battery"])
            self.assertEqual(again["interval_s"], 30)
            self.assertEqual(again["max_exposure_ms"], 250)

    def test_bad_values_are_cleaned(self):
        cfg = A.validate({
            "screen": {"enabled": 0, "when": "sometimes", "curve": [[50, 150], [-5, 20], [50, 30]]},
            "keyboard": {"curve": [[1, 2]]},
            "interval_s": 3, "max_exposure_ms": "lots",
            "calibration": {"dark": 30, "bright": 40, "zone": A.CAL_ZONE},
        })
        self.assertFalse(cfg["screen"]["enabled"])
        self.assertEqual(cfg["screen"]["when"], "always")
        # clamped, sorted, the duplicate x keeps the last value
        self.assertEqual(cfg["screen"]["curves"]["ac"], [[0, 20], [50, 30]])
        self.assertEqual(cfg["keyboard"]["curves"]["battery"], A.DEFAULTS["keyboard"]["curves"]["battery"])
        self.assertEqual(cfg["interval_s"], A.INTERVAL_RANGE[0])
        self.assertEqual(cfg["max_exposure_ms"], 250)
        # bright too close to dark: back to the guesses
        self.assertEqual(cfg["calibration"]["dark"], A.DEFAULTS["calibration"]["dark"])

    def test_calibration_of_another_zone_is_ignored(self):
        old = A.validate({"calibration": {"dark": 0.0067, "bright": 900, "dark_at": 1}})   # no zone: whole frame
        self.assertEqual(old["calibration"]["dark"], A.DEFAULTS["calibration"]["dark"])
        self.assertIsNone(old["calibration"]["dark_at"])

    def test_torch_that_missed_the_lens_is_dropped(self):
        cfg = A.validate({"calibration": {"dark": 0.0067, "bright": 0.31, "dark_at": 1, "bright_at": 2,
                                          "zone": A.CAL_ZONE}})
        self.assertEqual(cfg["calibration"]["dark"], 0.0067)            # a good dark end stays
        self.assertEqual(cfg["calibration"]["dark_at"], 1)
        self.assertEqual(cfg["calibration"]["bright"], A.DEFAULTS["calibration"]["bright"])
        self.assertIsNone(cfg["calibration"]["bright_at"])
        ok = A.validate({"calibration": {"dark": 0.01, "bright": 1500, "zone": A.CAL_ZONE}})
        self.assertEqual(ok["calibration"]["bright"], 1500)

    def test_garbage_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "luce.json"
            p.write_text("{not json")
            self.assertEqual(A.load(p), A.validate(None))


class CurveTest(unittest.TestCase):
    cfg = A.validate({"screen": {"curve": [[20, 10], [60, 90]]},
                      "keyboard": {"curve": [[0, 60], [50, 0]]}})

    def test_interpolation_and_flat_ends(self):
        c = self.cfg["screen"]["curves"]["ac"]
        self.assertEqual(A.curve_value(c, 0), 10)
        self.assertEqual(A.curve_value(c, 40), 50)
        self.assertEqual(A.curve_value(c, 100), 90)

    def test_target_offset_and_floors(self):
        self.assertEqual(A.target(self.cfg, "screen", 40, "ac"), 50)
        self.assertEqual(A.target(A.validate({"screen": {"curve": [[0, 0], [100, 0]]}}), "screen", 10, "ac"),
                         A.SCREEN_MIN)                                            # never black
        self.assertEqual(A.target(self.cfg, "keyboard", 80, "ac"), 0)            # keyboard may go off

    def test_one_curve_per_power_source(self):
        cfg = A.validate({"screen": {"curves": {"battery": [[0, 5], [100, 50]], "ac": [[0, 20], [100, 100]]}}})
        self.assertEqual(A.target(cfg, "screen", 50, A.profile_of(True)), 27.5)
        self.assertEqual(A.target(cfg, "screen", 50, A.profile_of(False)), 60)
        # the defaults are two independent lists, equal in value
        d = A.validate(None)["screen"]["curves"]
        self.assertEqual(d["battery"], d["ac"])
        self.assertIsNot(d["battery"], d["ac"])

    def test_old_single_curve_is_migrated(self):
        cfg = A.validate({"screen": {"curve": [[0, 5], [100, 90]]}})
        self.assertEqual(cfg["screen"]["curves"], {"battery": [[0, 5], [100, 90]], "ac": [[0, 5], [100, 90]]})

    def test_min_delta_is_measured_from_the_anchor(self):
        # a slow drift of 1 point per measurement: compared with the previous
        # measurement it would never move; compared with the anchor it does
        anchor, moves = 30.0, []
        for x in [30 + i for i in range(1, 13)]:
            if A.light_moved(x, anchor, 5):
                moves.append(x)
                anchor = x                     # this measurement set the level
        self.assertEqual(moves, [35, 40])
        self.assertTrue(A.light_moved(10, None, 5))       # nothing set yet
        self.assertTrue(A.light_moved(10, 10, 0))         # 0 = every measurement
        self.assertFalse(A.level_differs(50.2, 50))
        self.assertTrue(A.level_differs(51, 50))
        self.assertTrue(A.level_differs(30, None))

    def test_active(self):
        cfg = A.validate({"screen": {"when": "battery"}, "keyboard": {"enabled": False}})
        self.assertTrue(A.is_active(cfg, "screen", True))
        self.assertFalse(A.is_active(cfg, "screen", False))
        self.assertFalse(A.is_active(cfg, "keyboard", True))

    def test_ramp(self):
        vals, pause = A.ramp(100, 40, 2.0)
        self.assertEqual(len(vals), 40)                    # one write every 50 ms
        self.assertAlmostEqual(pause * len(vals), 2.0)
        self.assertEqual(vals[-1], 40)
        self.assertTrue(all(a > b for a, b in zip(vals, vals[1:])))
        kb, pause = A.ramp(20, 26, 2.0, quantum=1.0)       # the keyboard: whole percents
        self.assertEqual(len(kb), 6)
        self.assertAlmostEqual(pause, 2.0 / 6)
        self.assertEqual(A.ramp(50, 70, 0), ([70], 0.0))   # 0 s = at once
        self.assertEqual(A.ramp(30, 30.4, 2.0, 1.0), ([30.4], 0.0))

    def test_new_settings_validated(self):
        cfg = A.validate({"transition_s": 99, "min_delta": -3})
        self.assertEqual(cfg["transition_s"], A.TRANSITION_RANGE[1])
        self.assertEqual(cfg["min_delta"], 0)
        self.assertEqual(A.validate(None)["transition_s"], 2.0)
        self.assertEqual(A.validate(None)["min_delta"], 5)


class PausesTest(unittest.TestCase):
    def test_timer(self):
        p = A.Pauses()
        p.pause("screen", 1000, 40, "a mano")
        self.assertTrue(p.paused("screen") and not p.paused("keyboard"))
        self.assertIsNone(p.due("screen", 1000 + 3 * 3600 - 1, 41, 3, 30))
        self.assertEqual(p.due("screen", 1000 + 3 * 3600, None, 3, 30), "timer")    # even without a photo

    def test_strong_light_change(self):
        p = A.Pauses()
        p.pause("keyboard", 0, 40, "a mano")
        self.assertIsNone(p.due("keyboard", 60, 65, 3, 30))
        self.assertIn("luce cambiata", p.due("keyboard", 90, 72, 3, 30))
        self.assertIn("luce cambiata", p.due("keyboard", 90, 9, 3, 30))             # darker counts too

    def test_unknown_start_is_the_first_measurement(self):
        p = A.Pauses()
        p.pause("screen", 0, None, "dal pannello")
        self.assertIsNone(p.due("screen", 30, 20, 3, 30))                           # becomes the start
        self.assertIsNone(p.due("screen", 60, 45, 3, 30))
        self.assertIsNotNone(p.due("screen", 90, 51, 3, 30))

    def test_100_means_timer_only(self):
        p = A.Pauses()
        p.pause("screen", 0, 0, "a mano")
        self.assertIsNone(p.due("screen", 60, 100, 3, 100))

    def test_roundtrip_and_requests(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.json"
            p = A.Pauses()
            p.pause("screen", 5.0, 33.0, "a mano")
            p.save(f)
            q = A.Pauses.load(f)
            self.assertEqual(q.d["screen"], {"since": 5.0, "x": 33.0, "why": "a mano"})
            self.assertIsNone(q.d["keyboard"])
            r = Path(d) / "r.json"
            A.write_request("pausa", ["screen", "keyboard"], r)
            self.assertEqual(A.read_request(r), {"azione": "pausa", "uscite": ["screen", "keyboard"]})
            self.assertIsNone(A.read_request(r))                                   # consumed
            A.write_request("boh", ["screen"], r)
            self.assertIsNone(A.read_request(r))


class DarkMemoryTest(unittest.TestCase):
    F = A.LONG_FROM

    def test_cycle(self):
        m = A.DarkMemory()
        self.assertFalse(m.short_only(0, None, self.F))              # nothing known: full shot
        # the room turns dark: a long shot (planned or retried) becomes the reference
        self.assertEqual(m.settle(0, False, False, 0.3, 1500, self.F), (0.3, "lunga"))
        # next shots are one-frame checks while it stays dark
        self.assertTrue(m.short_only(30, 1500, self.F))
        self.assertEqual(m.settle(30, True, True, 0.9, 330, self.F), (0.3, "buio confermato"))
        # ...until the reference is DARK_REFRESH_S old
        self.assertFalse(m.short_only(A.DARK_REFRESH_S + 1, 1500, self.F))
        self.assertEqual(m.settle(601, False, False, 0.25, 1600, self.F), (0.25, "lunga"))
        self.assertTrue(m.short_only(630, 1600, self.F))

    def test_light_comes_on(self):
        m = A.DarkMemory()
        m.settle(0, False, False, 0.3, 1500, self.F)
        # the one-frame check sees light: that reading is used and the memory ends
        self.assertEqual(m.settle(30, True, False, 2.0, 330, self.F), (2.0, "breve"))
        self.assertFalse(m.short_only(60, 1500, self.F))

    def test_bright_room_never_caps(self):
        m = A.DarkMemory()
        self.assertEqual(m.settle(0, False, False, 3.0, 330, self.F), (3.0, "breve"))
        self.assertFalse(m.short_only(30, 330, self.F))
        # a lit room just above one frame period (40 ms) is not "dark"
        self.assertEqual(m.settle(30, False, False, 1.0, 405, self.F), (1.0, "breve"))
        self.assertFalse(m.short_only(60, 405, self.F))


class LightModelTest(unittest.TestCase):
    def test_ioctl_numbers(self):
        # the values in linux/videodev2.h on x86_64
        self.assertEqual(C.VIDIOC_S_FMT, 0xC0D05605)
        self.assertEqual(C.VIDIOC_REQBUFS, 0xC0145608)
        self.assertEqual(C.VIDIOC_QUERYBUF, 0xC0585609)
        self.assertEqual(C.VIDIOC_QBUF, 0xC058560F)
        self.assertEqual(C.VIDIOC_DQBUF, 0xC0585611)
        self.assertEqual(C.VIDIOC_STREAMON, 0x40045612)
        self.assertEqual(C.VIDIOC_S_CTRL, 0xC008561C)
        self.assertEqual(C.VIDIOC_QUERYCTRL, 0xC0445624)

    def test_reference_scene_is_one(self):
        for v, e in C.RESPONSE:
            self.assertAlmostEqual(C.light(C.BLACK + v, e), 1.0, places=6)

    def test_other_scene_is_consistent_across_exposures(self):
        # first measurement of 19/11 (another light): exposure ms -> mean - 16
        run = [(32, 2.22), (50, 5.40), (80, 11.47), (125, 20.5), (200, 34.15), (250, 41.63), (400, 59.16)]
        est = [C.light(C.BLACK + v, e) for e, v in run]
        self.assertLess(max(est) / min(est), 1.08)

    def test_monotonic(self):
        vals = [C.light(C.BLACK + v, 100) for v in (0, 0.1, 1, 5, 20, 60, 94, 120, 200, 239)]
        self.assertTrue(all(a < b for a, b in zip(vals, vals[1:])))

    def test_ambient_scale(self):
        self.assertEqual(C.ambient(0.05, 0.05, 200), 0)
        self.assertEqual(C.ambient(200, 0.05, 200), 100)
        self.assertEqual(C.ambient(1e-9, 0.05, 200), 0)
        self.assertEqual(C.ambient(1e9, 0.05, 200), 100)
        mid = C.ambient((0.05 * 200) ** 0.5, 0.05, 200)
        self.assertAlmostEqual(mid, 50, places=6)          # log scale

    def test_exposure_steering(self):
        lo, hi = 48, 2500
        ok = C.Frame(mean=C.BLACK + 40, saturated=0, exposure=1000, t=0)
        self.assertIsNone(C.next_exposure(ok, lo, hi))
        self.assertIsNone(C.next_exposure(C.Frame(C.BLACK + 1.2, 0, 1000, 0), lo, hi))  # low but usable
        dark = C.Frame(mean=C.BLACK + 0.3, saturated=0, exposure=500, t=0)
        self.assertGreater(C.next_exposure(dark, lo, hi), 500)
        self.assertIsNone(C.next_exposure(C.Frame(C.BLACK + 0.3, 0, hi, 0), lo, hi))   # range ends
        clipped = C.Frame(mean=240, saturated=0.4, exposure=1000, t=0)
        self.assertLessEqual(C.next_exposure(clipped, lo, hi), 500)
        self.assertIsNone(C.next_exposure(C.Frame(240, 0.9, lo, 0), lo, hi))

    def test_next_shot_is_as_short_as_reliable(self):
        lo, hi = 48, 2500
        # a dim room (19/11 evening): 250 ms gave signal 8, next time ~120 ms will do
        dim = C.Frame(mean=C.BLACK + 8.0, saturated=0, exposure=2500, t=0)
        e = C.plan_exposure(dim, lo, hi)
        self.assertTrue(1000 <= e <= 1400, e)
        # ...and the light estimate does not depend on which exposure is used
        # (measured: 100 ms → signal 1.28, 250 ms → signal 8.03, same scene)
        self.assertAlmostEqual(C.light(C.BLACK + 1.28, 100.0) / C.light(C.BLACK + 8.03, 250.0), 1.0, delta=0.05)
        # the reference room at 250 ms (signal 37): about one frame period is enough
        ref = C.Frame(mean=C.BLACK + 37.0, saturated=0, exposure=2500, t=0)
        self.assertTrue(C.FRAME_FLOOR <= C.plan_exposure(ref, lo, hi) <= 400)
        # a bright scene never goes below one frame period (it saves nothing)...
        bright = C.Frame(mean=C.BLACK + 60, saturated=0, exposure=330, t=0)
        self.assertEqual(C.plan_exposure(bright, lo, hi), C.FRAME_FLOOR)
        # ...unless the frame period itself would clip
        glare = C.Frame(mean=C.BLACK + 150, saturated=0.0, exposure=330, t=0)
        self.assertLess(C.plan_exposure(glare, lo, hi), C.FRAME_FLOOR)
        # dark rooms use the maximum, black frames restart from it
        self.assertEqual(C.plan_exposure(C.Frame(C.BLACK + 1.5, 0, 2500, 0), lo, hi), hi)
        self.assertEqual(C.plan_exposure(C.Frame(C.BLACK, 0, 500, 0), lo, hi), hi)

    def test_luma_stats(self):
        data = bytes([16, 128, 116, 128] * 100)      # Y alternates 16 / 116
        mean, sat = C.luma_stats(data)
        self.assertAlmostEqual(mean, 66)
        self.assertEqual(sat, 0)

    def test_only_side_bands_count(self):
        w, h = 16, 2
        row = [30] * 4 + [250] * 8 + [50] * 4          # bright middle (you), darker sides
        ys = row * h
        data = bytes(b for y in ys for b in (y, 128))
        mean, sat = C.luma_stats(data, w, h)
        self.assertAlmostEqual(mean, 40)                # (30 + 50) / 2: the middle is ignored
        self.assertEqual(sat, 0)


class OpenWatchTest(unittest.TestCase):
    """inotify works the same on a regular file as on a device node."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.w = C.OpenWatch([self.tmp.name])

    def tearDown(self):
        self.w.close()
        os.unlink(self.tmp.name)

    def drain(self):
        time.sleep(0.05)
        return self.w.read()

    def test_stranger_seen(self):
        self.assertFalse(self.drain())
        with open(self.tmp.name):
            self.assertTrue(self.drain())
        self.assertFalse(self.drain())            # a close is not an open
        self.assertGreater(self.w.last_other_open, 0)

    def test_own_open_ignored(self):
        self.w.own_open()
        fd = os.open(self.tmp.name, os.O_RDONLY)
        self.assertFalse(self.drain())
        os.close(fd)
        self.assertEqual(self.w.last_other_open, 0)

    def test_stranger_after_own_open(self):
        self.w.own_open()
        fd = os.open(self.tmp.name, os.O_RDONLY)
        self.w.read()                             # what the claim does right after its open
        with open(self.tmp.name):
            self.assertTrue(self.drain())
        os.close(fd)

    def test_failed_own_open(self):
        self.w.own_open()
        self.w.own_open_failed()
        with open(self.tmp.name):
            self.assertTrue(self.drain())         # not swallowed by the cancelled announcement

    def test_holders(self):
        self.assertEqual(C.holders([self.tmp.name]), 0)    # our own pid never counts


class TrayAppletTest(unittest.TestCase):
    CFG = """[Containments][248][Applets][318]
immutability=1
plugin=org.kde.plasma.brightness

[Containments][248][General]
extraItems=org.kde.plasma.vault,org.kde.plasma.brightness,org.kde.plasma.battery
knownItems=org.kde.plasma.brightness,org.kde.plasma.clipboard
shownItems=org.kde.plasma.battery

[Containments][9][General]
panelWidgets=[{"name":"org.kde.plasma.brightness","inTray":true}]
"""

    def test_install_and_back(self):
        new, n = trayapplet.retarget(self.CFG, trayapplet.ORIG, trayapplet.OURS)
        self.assertIn("plugin=org.mircogiorgi.brightness\n", new)
        self.assertIn("extraItems=org.kde.plasma.vault,org.mircogiorgi.brightness,org.kde.plasma.battery", new)
        # the original stays known, or plasmashell would add it back as new
        self.assertIn("knownItems=org.kde.plasma.brightness,org.kde.plasma.clipboard,org.mircogiorgi.brightness", new)
        self.assertIn('"name":"org.kde.plasma.brightness"', new)        # other widgets' caches untouched
        back, _ = trayapplet.retarget(new, trayapplet.OURS, trayapplet.ORIG)
        self.assertIn("plugin=org.kde.plasma.brightness\n", back)
        self.assertIn("extraItems=org.kde.plasma.vault,org.kde.plasma.brightness,org.kde.plasma.battery", back)

    def test_duplicate_original_is_dropped(self):
        # what plasmashell did once: the twin installed and the original re-added next to it
        cfg = self.CFG.replace("plugin=org.kde.plasma.brightness", "plugin=org.mircogiorgi.brightness") + (
            "\n[Containments][248][Applets][224]\nimmutability=1\nplugin=org.kde.plasma.brightness\n"
            "\n[Containments][248][Applets][224][Configuration]\nfoo=1\n")
        cfg = cfg.replace("extraItems=org.kde.plasma.vault,org.kde.plasma.brightness",
                          "extraItems=org.kde.plasma.vault,org.mircogiorgi.brightness,org.kde.plasma.brightness")
        new, _ = trayapplet.retarget(cfg, trayapplet.ORIG, trayapplet.OURS)
        self.assertNotIn("[Applets][224]", new)
        self.assertNotIn("plugin=org.kde.plasma.brightness", new)
        self.assertIn("extraItems=org.kde.plasma.vault,org.mircogiorgi.brightness,org.kde.plasma.battery", new)
        self.assertIn("[Containments][248][Applets][318]", new)

    def test_nested_tray_items(self):
        cfg = ("[Containments][214][Applets][224][Applets][251]\nplugin=org.mircogiorgi.brightness\n\n"
               "[Containments][214][Applets][224][Applets][6308]\nplugin=org.kde.plasma.brightness\n\n"
               "[Containments][214][Applets][224][Applets][6308][Configuration]\na=1\n\n"
               "[Containments][214][Applets][224][General]\n"
               "extraItems=org.mircogiorgi.brightness,org.kde.plasma.brightness\n"
               "knownItems=org.mircogiorgi.brightness,org.kde.plasma.brightness\n")
        new, _ = trayapplet.retarget(cfg, trayapplet.ORIG, trayapplet.OURS)
        self.assertNotIn("[6308]", new)
        self.assertIn("[Applets][251]", new)
        self.assertIn("extraItems=org.mircogiorgi.brightness\n", new)
        self.assertIn("knownItems=org.mircogiorgi.brightness,org.kde.plasma.brightness\n", new)

    def test_package_is_complete(self):
        ui = trayapplet.SRC / "contents/ui"
        for f in ("main.qml", "PopupDialog.qml", "AdaptiveItem.qml", "BrightnessItem.qml"):
            self.assertTrue((ui / f).exists(), f)
        meta = json.loads((trayapplet.SRC / "metadata.json").read_text())
        self.assertEqual(meta["KPlugin"]["Id"], trayapplet.OURS)
        self.assertIn("AdaptiveItem", (ui / "PopupDialog.qml").read_text())
        self.assertNotIn("i18n(", (ui / "main.qml").read_text())          # all through the original domain


class StatusTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s" / "status.json"
            self.assertIsNone(A.read_status(p))
            A.write_status({"state": "ok", "reading": {"x": 40.0}}, p)
            st = A.read_status(p)
            self.assertEqual(st["state"], "ok")
            self.assertEqual(st["pid"], os.getpid())
            self.assertIn("time", st)


@unittest.skipUnless(os.environ.get("OMEN_HUB_CAMERA_TEST") == "1", "needs the camera (LED)")
class CameraTest(unittest.TestCase):
    def test_measure_restores_controls(self):
        dev = C.find_device()
        r = C.measure(dev, 250)
        self.assertGreater(r.lum, 0)
        self.assertLess(r.on_s, 3)
        s = C._Session(dev, None)
        s.claim()
        try:
            self.assertEqual(s.saved[C.CID_EXPOSURE_AUTO], C.EXPOSURE_APERTURE_PRIORITY)
        finally:
            s.close()

    def test_abort_is_immediate(self):
        dev = C.find_device()
        r, w = os.pipe()
        threading.Timer(0.15, lambda: os.write(w, b"x")).start()
        t0 = time.monotonic()
        with self.assertRaises(C.Aborted):
            C.measure(dev, 1000, start_exposure=10000, abort_fd=r)   # a 1 s exposure, dropped
        self.assertLess(time.monotonic() - t0, 0.6)
        os.close(r)
        os.close(w)

    def test_busy_when_someone_else_streams(self):
        dev = C.find_device()
        other = C._Session(dev, None)
        other.claim()
        try:
            with self.assertRaises(C.Busy):
                C.measure(dev, 250)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
