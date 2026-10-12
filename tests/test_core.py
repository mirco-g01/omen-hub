"""Core tests. Run with: python -m unittest discover -s tests

The daemon test starts omen-hubd as the current user against a scratch tree
(OMEN_HUB_ROOT) in dry-run mode, so systemctl is never called for real.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from omen_hub import cpuconf, fanconf, profiles  # noqa: E402

REAL_CONF = """# Impostazioni globali
[settings]
battery_notification_threshold = 20

[battery]
governor = powersave
energy_performance_preference = power
turbo = never
# senza questa riga a batteria restava 'performance'
platform_profile = balanced
# Limitiamo la CPU a 1.2 GHz
scaling_max_freq = 1200000

[charger]
governor = powersave
energy_performance_preference = balance_performance
platform_profile = performance
# 'auto' accendeva il turbo solo con >20% di CPU totale
turbo = always
# minimi
scaling_min_freq = 1400000
"""


class CpuConfTest(unittest.TestCase):
    def test_roundtrip_keeps_comments_and_unmanaged(self):
        sec = cpuconf.parse(REAL_CONF)
        ac = cpuconf.section_to_profile(sec["charger"])
        bat = cpuconf.section_to_profile(sec["battery"])
        self.assertEqual(ac["turbo"], "always")
        self.assertEqual(ac["min_freq"], 1400)
        self.assertEqual(bat["max_freq"], 1200)
        out = cpuconf.render(REAL_CONF, ac, bat)
        self.assertEqual(cpuconf.parse(out), cpuconf.parse(REAL_CONF))
        for line in REAL_CONF.splitlines():
            if line.startswith("#"):
                self.assertIn(line, out)
        self.assertIn("battery_notification_threshold = 20", out)

    def test_change_remove_and_add(self):
        sec = cpuconf.parse(REAL_CONF)
        bat = cpuconf.section_to_profile(sec["battery"])
        bat.update(max_freq=None, min_freq=800, turbo="auto")
        ac = cpuconf.section_to_profile(sec["charger"])
        out = cpuconf.render(REAL_CONF, ac, bat)
        b = cpuconf.parse(out)["battery"]
        self.assertNotIn("scaling_max_freq", b)
        self.assertEqual(b["scaling_min_freq"], "800000")
        self.assertEqual(b["turbo"], "auto")
        # added key lands inside [battery], not after [charger]
        self.assertLess(out.index("scaling_min_freq = 800000"), out.index("[charger]"))

    def test_missing_section_is_created(self):
        p = profiles.PRESETS["Bilanciato"]
        out = cpuconf.render("[settings]\nx = 1\n", p, p)
        sec = cpuconf.parse(out)
        self.assertEqual(sec["charger"]["turbo"], "auto")
        self.assertEqual(sec["battery"]["platform_profile"], "balanced")


class FanConfTest(unittest.TestCase):
    base = {"mode": "curve", "curves": {"Default": [[30, 0], [90, 100]]},
            "active_curve": "Default", "curve": [[30, 0], [90, 100]]}

    def test_save_activate_syncs_legacy_curve(self):
        new = fanconf.apply_changes(self.base, {"save_curve": {
            "name": "Quiet", "points": [[80, 100], [40, 0]], "activate": True}})
        self.assertEqual(new["active_curve"], "Quiet")
        self.assertEqual(new["curve"], [[40.0, 0.0], [80.0, 100.0]])
        self.assertEqual(self.base["active_curve"], "Default")   # input untouched

    def test_rename_delete(self):
        c = fanconf.apply_changes(self.base, {"save_curve": {"name": "B", "points": [[30, 0], [60, 50]]}})
        c = fanconf.apply_changes(c, {"rename_curve": {"old": "Default", "new": "A"}})
        self.assertEqual(c["active_curve"], "A")
        c = fanconf.apply_changes(c, {"delete_curve": "A"})
        self.assertEqual(c["active_curve"], "B")
        with self.assertRaises(ValueError):
            fanconf.apply_changes(c, {"delete_curve": "B"})

    def test_rejects_bad_values(self):
        for ch in ({"mode": "turbo"}, {"cleaner_duration": 200}, {"cleaner_speed": 5},
                   {"save_curve": {"name": "x", "points": [[30, 0]]}},
                   {"save_curve": {"name": "../x", "points": [[30, 0], [40, 10]]}},
                   {"whatever": 1}):
            with self.assertRaises(ValueError, msg=str(ch)):
                fanconf.apply_changes(self.base, ch)

    def test_curve_value(self):
        pts = [[30, 0], [50, 20], [90, 100]]
        self.assertEqual(fanconf.curve_value(pts, 20), 0)
        self.assertAlmostEqual(fanconf.curve_value(pts, 70), 60)
        self.assertEqual(fanconf.curve_value(pts, 70, smooth=False), 20)


class ProfilesTest(unittest.TestCase):
    def test_seed_imports_current_config(self):
        store = profiles.seed_from_cpufreq(REAL_CONF)
        self.assertEqual(store["assign"], {"ac": "Rete (attuale)", "battery": "Batteria (attuale)"})
        self.assertEqual(store["profiles"]["Rete (attuale)"]["turbo"], "always")
        v = profiles.validate_store(store, platform_choices=["low-power", "balanced", "performance"],
                                    curves=["Default"], freq_range=(800, 5500))
        # applying the seeded store must not change auto-cpufreq's behaviour
        ac = v["profiles"][v["assign"]["ac"]]
        bat = v["profiles"][v["assign"]["battery"]]
        self.assertEqual(cpuconf.parse(cpuconf.render(REAL_CONF, ac, bat)), cpuconf.parse(REAL_CONF))

    def test_validation(self):
        good = dict(profiles.PRESETS["Prestazioni"])
        with self.assertRaises(ValueError):
            profiles.validate_profile({**good, "turbo": "sometimes"})
        with self.assertRaises(ValueError):
            profiles.validate_profile({**good, "min_freq": 3000, "max_freq": 2000})
        with self.assertRaises(ValueError):
            profiles.validate_profile({**good, "max_freq": 9000}, freq_range=(800, 5500))
        with self.assertRaises(ValueError):
            profiles.validate_profile({**good, "fan": {"mode": "curve", "curve": "Nope"}},
                                      curves=["Default"])
        with self.assertRaises(ValueError):
            profiles.validate_store({"profiles": {"A": good}, "assign": {"ac": "A", "battery": "B"}})


class LimitsTest(unittest.TestCase):
    D = {"pl": [170, 170], "trl_p": [52, 52, 50, 50, 50, 50, 50, 50], "trl_e": [45] * 8}

    def test_pack_roundtrip(self):
        from omen_hub import limits
        v = 0x3232323232323434
        self.assertEqual(limits.unpack_ratios(v), [52, 52, 50, 50, 50, 50, 50, 50])
        self.assertEqual(limits.pack_ratios(limits.unpack_ratios(v)), v)

    def test_desired_never_above_firmware(self):
        from omen_hub import limits
        w = limits.desired({"pl1": 200, "pl2": 60, "ratio_p": 55, "ratio_e": 30}, self.D)
        self.assertEqual((w["pl1"], w["pl2"]), (170, 170))        # pl2 raised to pl1, capped
        self.assertEqual(w["trl_p"], [52, 52, 50, 50, 50, 50, 50, 50])
        self.assertEqual(w["trl_e"], [30] * 8)                          # int = every bin
        w = limits.desired({"ratio_p": [48, 48, 40, None, None, None, None, 30]}, self.D)
        self.assertEqual(w["trl_p"], [48, 48, 40, 40, 40, 40, 40, 30])     # kept non-increasing
        w = limits.desired({"ratio_e": [None, None, None, None, 40, None, 35, None]}, self.D)
        self.assertEqual(w["trl_e"], [45, 45, 45, 45, 40, 40, 35, 35])
        w = limits.desired({}, self.D)
        self.assertEqual((w["pl1"], w["trl_e"]), (170, [45] * 8))

    def test_profile_validation(self):
        good = dict(profiles.PRESETS["Bilanciato"])
        p = profiles.validate_profile({**good, "pl1": 45, "pl2": 65, "gpu_clock": 1200, "ratio_p": 30, "ratio_e": 0})
        self.assertEqual((p["pl1"], p["pl2"], p["gpu_clock"], p["ratio_p"], p["ratio_e"]),
                         (45, 65, 1200, [30] * 8, None))
        p = profiles.validate_profile({**good, "ratio_e": [45, 45, 44, 44, 42, 40, 38, 38]})
        self.assertEqual(p["ratio_e"], [45, 45, 44, 44, 42, 40, 38, 38])
        for bad in ({"pl1": 80, "pl2": 50}, {"pl1": 5}, {"ratio_p": [40] * 3}, {"ratio_e": 99}, {"gpu_clock": 100}):
            with self.assertRaises(ValueError, msg=str(bad)):
                profiles.validate_profile({**good, **bad})


class DaemonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "etc/omen-fan-control").mkdir(parents=True)
        (root / "etc/auto-cpufreq.conf").write_text(REAL_CONF)
        (root / "etc/omen-fan-control/config.json").write_text(json.dumps(FanConfTest.base))
        sleep = root / "usr/lib/systemd/system-sleep"
        sleep.mkdir(parents=True)
        hook = sleep / "free-vram-on-suspend.sh"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        me = os.getuid()
        (root / "etc/omen-hub").mkdir(parents=True)
        (root / "etc/omen-hub/hubd.json").write_text(json.dumps({"allowed_uids": [me]}))
        cls.sock = str(root / "hub.sock")
        cls.root = root
        env = dict(os.environ, OMEN_HUB_ROOT=str(root), OMEN_HUB_DRY_RUN="1",
                   OMEN_HUB_SOCKET=cls.sock, PYTHONPATH=str(HERE))
        cls.proc = subprocess.Popen([sys.executable, "-m", "omen_hub.daemon"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(50):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.1)
        os.environ["OMEN_HUB_SOCKET"] = cls.sock

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def send(self, req):
        from omen_hub import protocol
        return protocol.send(req, self.sock)

    def test_ping_and_unknown(self):
        from omen_hub import protocol
        self.assertTrue(self.send({"cmd": "ping"})["dry_run"])
        with self.assertRaises(protocol.HubError):
            self.send({"cmd": "exec", "argv": ["sh"]})
        with self.assertRaises(protocol.HubError):
            self.send({"cmd": "unit", "unit": "sshd.service", "action": "start"})
        self.assertTrue(self.send({"cmd": "unit", "unit": "omen-fan-control.service",
                                   "action": "restart"})["ok"])

    def test_profiles_roundtrip(self):
        store = self.send({"cmd": "profiles.get"})["store"]
        store["assign"] = {"ac": "Rete (attuale)", "battery": "Silenzioso"}
        r = self.send({"cmd": "profiles.save", "store": store})
        self.assertTrue(r["changed"])
        conf = cpuconf.parse((self.root / "etc/auto-cpufreq.conf").read_text())
        self.assertEqual(conf["battery"]["platform_profile"], "low-power")
        self.assertNotIn("scaling_max_freq", conf["battery"])
        self.assertEqual(conf["charger"]["turbo"], "always")      # untouched
        self.assertTrue(list((self.root / "var/lib/omen-hub/backup").glob("auto-cpufreq.conf.*")))
        self.assertTrue((self.root / "etc/omen-hub/profiles.json").exists())

    def test_profile_fan_override_is_undone(self):
        """Massime (fans max) then Silenzioso (fans untouched) must give the
        fans back to what the user had: the bug of 12/10."""
        cfg_path = self.root / "etc/omen-fan-control/config.json"
        self.send({"cmd": "fan.set", "changes": {"mode": "curve"}})
        store = self.send({"cmd": "profiles.get"})["store"]
        src = "ac" if Path("/sys/class/power_supply/AC/online").read_text().strip() == "1" else "battery"
        store["assign"][src] = "Massime"
        self.send({"cmd": "profiles.save", "store": store})
        self.assertEqual(json.loads(cfg_path.read_text())["mode"], "max")
        self.assertTrue((self.root / "var/lib/omen-hub/fan-override.json").exists())
        store["assign"][src] = "Silenzioso"
        r = self.send({"cmd": "profiles.save", "store": store})
        self.assertEqual(json.loads(cfg_path.read_text())["mode"], "curve", r)
        self.assertFalse((self.root / "var/lib/omen-hub/fan-override.json").exists())
        # a user choice while overridden becomes the new base
        store["assign"][src] = "Massime"
        self.send({"cmd": "profiles.save", "store": store})
        self.send({"cmd": "fan.set", "changes": {"mode": "auto"}})
        store["assign"][src] = "Bilanciato"
        self.send({"cmd": "profiles.save", "store": store})
        self.assertEqual(json.loads(cfg_path.read_text())["mode"], "auto")

    def test_no_epp_with_performance_governor(self):
        p = dict(profiles.PRESETS["Prestazioni"], governor="performance", epp="performance")
        self.assertIsNone(cpuconf.profile_to_values(p)["energy_performance_preference"])

    def test_fan_set_and_clean(self):
        r = self.send({"cmd": "fan.set", "changes": {"mode": "auto", "cleaner_duration": 40}})
        cfg = json.loads((self.root / "etc/omen-fan-control/config.json").read_text())
        self.assertEqual((cfg["mode"], cfg["cleaner_duration"]), ("auto", 40))
        from omen_hub import protocol
        with self.assertRaises(protocol.HubError):
            self.send({"cmd": "fan.set", "changes": {"manual_pwm": 999}})
        self.send({"cmd": "fan.clean", "action": "run", "duration": 20})
        req = json.loads((self.root / "etc/omen-fan-control/cleaner.request").read_text())
        self.assertEqual(req["duration"], 20)

    def test_hook_park_and_restore(self):
        hook = self.root / "usr/lib/systemd/system-sleep/free-vram-on-suspend.sh"
        parked = self.root / "var/lib/omen-hub/disabled-hooks/free-vram-on-suspend.sh"
        self.send({"cmd": "component", "id": "hook-vram", "action": "uninstall"})
        self.assertFalse(hook.exists())
        self.assertTrue(parked.exists())
        self.assertFalse(os.access(parked, os.X_OK))
        self.send({"cmd": "component", "id": "hook-vram", "action": "install"})
        self.assertTrue(os.access(hook, os.X_OK))
        self.assertFalse(parked.exists())


if __name__ == "__main__":
    unittest.main()


class GroupTest(unittest.TestCase):
    """The PartOf drop-ins, written and removed on a scratch tree."""

    def run_hubd(self, root, flag):
        env = dict(os.environ, OMEN_HUB_ROOT=str(root), OMEN_HUB_DRY_RUN="1", PYTHONPATH=str(HERE))
        return subprocess.run([sys.executable, "-m", "omen_hub.daemon", flag], env=env,
                              capture_output=True, text=True, timeout=20)

    def test_dropins_roundtrip(self):
        from omen_hub import group
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # a drop-in of someone else's must survive the removal
            other = root / "etc/systemd/system/omen-fxd.service.d/10-mine.conf"
            other.parent.mkdir(parents=True)
            other.write_text("[Service]\nNice=1\n")
            r = self.run_hubd(root, "--setup-group")
            self.assertEqual(r.returncode, 0, r.stderr)
            fan = root / "etc/systemd/system/omen-fan-control.service.d" / group.DROPIN
            self.assertIn("PartOf=omen-hub.target", fan.read_text())
            mute = root / "etc/systemd/user/omen-mute-monitor.service.d" / group.DROPIN
            self.assertIn("PartOf=omen-hub-user.target", mute.read_text())
            # never part of the group: the helper itself, hooks, desktop extras
            for unit in ("omen-hubd.service", "ds4-desk.service", "nvidia-suspend.service"):
                self.assertFalse((root / f"etc/systemd/system/{unit}.d").exists(), unit)
                self.assertFalse((root / f"etc/systemd/user/{unit}.d").exists(), unit)
            r = self.run_hubd(root, "--remove-group")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(fan.exists() or fan.parent.exists())
            self.assertFalse(mute.exists())
            self.assertTrue(other.exists())
