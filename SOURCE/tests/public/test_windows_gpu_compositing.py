"""Windows keeps Chromium GPU compositing; other platforms keep --disable-gpu."""

import unittest

from tests.public.test_linux_sandbox_opt_in import load_startup_flags


def flags(platform, environ=None):
    return load_startup_flags(platform=platform, environ=environ)["QTWEBENGINE_CHROMIUM_FLAGS"].split()


class WindowsGpuCompositingTests(unittest.TestCase):
    def test_windows_default_does_not_disable_gpu(self):
        # --disable-gpu made the 1.0.5 UI rasterize on the CPU (~107% of a core idle).
        self.assertNotIn("--disable-gpu", flags("win32"))

    def test_windows_keeps_its_other_stability_flags(self):
        result = flags("win32")
        self.assertIn("--autoplay-policy=no-user-gesture-required", result)
        self.assertIn("--disable-features=AudioServiceOutOfProcess", result)

    def test_escape_hatch_restores_disable_gpu_once(self):
        self.assertEqual(flags("win32", {"VIOLA_DISABLE_GPU": "1"}).count("--disable-gpu"), 1)
        self.assertNotIn("--disable-gpu", flags("win32", {"VIOLA_DISABLE_GPU": "0"}))

    def test_other_platforms_unchanged(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                self.assertEqual(flags(platform).count("--disable-gpu"), 1)


if __name__ == "__main__":
    unittest.main()
