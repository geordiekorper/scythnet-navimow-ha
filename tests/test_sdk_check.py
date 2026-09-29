"""The start-up check that the mower_sdk imported is navimow-sdk-community."""
import importlib.metadata
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.exceptions import ConfigEntryError

from custom_components.navimow import async_setup_entry, sdk_check

ROOT = Path(__file__).resolve().parents[1]
SITE_INIT = "/site-packages/mower_sdk/__init__.py"


def lookup(version):
    def version_of(name):
        assert name == "navimow-sdk-community"
        if version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return version
    return version_of


def module(version, path=SITE_INIT):
    return SimpleNamespace(__version__=version, __file__=path)


class CheckTest(unittest.TestCase):
    def test_the_installed_distribution_passes(self):
        self.assertIsNone(sdk_check.check(lookup("0.2.0a3"), lambda: SITE_INIT, module("0.2.0a3")))

    def test_this_environment_passes(self):
        self.assertIsNone(sdk_check.PROBLEM)
        self.assertIsNone(sdk_check.check())

    def test_upstream_files_over_the_distribution_say_uninstall_both(self):
        problem = sdk_check.check(lookup("0.2.0a3"), lambda: SITE_INIT, module("0.1.0"))
        self.assertIn("navimow-sdk-community 0.2.0a3 is installed", problem)
        self.assertIn(f"the mower_sdk package imported is version 0.1.0 from {SITE_INIT}", problem)
        self.assertIn("Uninstall navimow-sdk and navimow-sdk-community", problem)

    def test_a_copy_elsewhere_on_the_path_is_named_for_removal(self):
        problem = sdk_check.check(
            lookup("0.2.0a3"), lambda: SITE_INIT, module("0.2.0a2", "/config/deps/mower_sdk/__init__.py")
        )
        self.assertIn("version 0.2.0a2 from /config/deps/mower_sdk/__init__.py", problem)
        self.assertIn("Remove or correct the copy in /config/deps/mower_sdk first", problem)
        self.assertNotIn("Uninstall", problem)

    def test_a_missing_distribution_is_a_problem(self):
        problem = sdk_check.check(lookup(None), lambda: None, module("0.1.0"))
        self.assertTrue(problem.startswith("navimow-sdk-community is not installed, but"))
        self.assertIn("Remove or correct the copy", problem)


class SetupRefusesTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_raises_config_entry_error_with_the_message(self):
        with patch.object(sdk_check, "PROBLEM", "the facts"):
            with self.assertRaises(ConfigEntryError) as raised:
                await async_setup_entry(SimpleNamespace(data={}), SimpleNamespace(entry_id="e1"))
        self.assertEqual(str(raised.exception), "the facts")


# The upstream package's surface, as far as these modules import it.
UPSTREAM_STUB = {
    "__init__.py": '__version__ = "0.1.0"\n',
    "api.py": "class MowerAPI:\n    pass\n",
    "errors.py": "class MowerAPIError(Exception):\n    pass\n",
    "sdk.py": "class NavimowSDK:\n    pass\n",
    "models.py": "class MowerCommand:\n    pass\n\nclass DeviceStateMessage:\n    pass\n",
}


class UpstreamFilesTest(unittest.TestCase):
    def test_the_modules_home_assistant_imports_first_load_with_upstreams_files(self):
        """With upstream's mower_sdk on the path, the package, config_flow, auth
        and const import, and the check reports the mismatch instead of an
        ImportError stopping everything before setup."""
        with tempfile.TemporaryDirectory() as temp:
            stub = Path(temp) / "mower_sdk"
            stub.mkdir()
            for name, text in UPSTREAM_STUB.items():
                (stub / name).write_text(text)
            script = textwrap.dedent(f"""
                import sys
                sys.path[:0] = [{temp!r}, {str(ROOT)!r}]
                import custom_components.navimow as navimow
                import custom_components.navimow.config_flow
                import custom_components.navimow.auth
                import custom_components.navimow.const
                assert navimow.sdk_check.PROBLEM is not None
                assert "imported is version 0.1.0" in navimow.sdk_check.PROBLEM
                assert "NavimowCoordinator" not in vars(navimow)
                print("ok")
            """)
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=temp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")
