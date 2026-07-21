import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mtmux import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env = patch.dict("mtmux.config.os.environ", {"MTMUX_CONFIG_DIR": self.tempdir.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_config(self, text):
        path = Path(self.tempdir.name) / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_fresh_config_contains_default_prefix(self):
        cfg, wrapper = config.ensure_config()

        self.assertEqual(cfg.read_text(), 'hosts = []\nprefix = "C-s"\nsidebar_width = 40\nstatus_timeout = 5\npersistent_ssh = true\n')
        self.assertNotIn("prefix", wrapper.read_text())
        self.assertNotIn("send-prefix", wrapper.read_text())
        self.assertIn("set -g mouse on", wrapper.read_text())

    def test_existing_wrapper_is_preserved(self):
        wrapper = Path(self.tempdir.name) / "wrapper.tmux.conf"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("set -g mouse off\n")

        config.ensure_config()

        self.assertEqual(wrapper.read_text(), "set -g mouse off\n")

    def test_missing_prefix_uses_default(self):
        self.write_config("hosts = []\n")

        self.assertEqual(config.load_prefix(), "C-s")

    def test_custom_prefix_loads(self):
        self.write_config('hosts = []\nprefix = "C-g"\n')

        self.assertEqual(config.load_prefix(), "C-g")

    def test_invalid_prefix_values_fail_clearly(self):
        for value in ("42", '""', '"C x"', '"C-\\t"'):
            with self.subTest(value=value):
                self.write_config(f"hosts = []\nprefix = {value}\n")
                with self.assertRaisesRegex(SystemExit, "prefix must be a non-empty, printable, whitespace-free string"):
                    config.load_prefix()

    def test_sidebar_width_defaults_to_40_and_accepts_custom_value(self):
        self.write_config("hosts = []\n")
        self.assertEqual(config.load_sidebar_width(), 40)

        self.write_config("sidebar_width = 52\n")
        self.assertEqual(config.load_sidebar_width(), 52)

    def test_invalid_sidebar_width_fails_clearly(self):
        for value in ("0", "-1", '"40"', "true"):
            with self.subTest(value=value):
                self.write_config(f"sidebar_width = {value}\n")
                with self.assertRaisesRegex(SystemExit, "sidebar_width must be a positive integer"):
                    config.load_sidebar_width()

    def test_status_timeout_defaults_to_5_and_accepts_custom_value(self):
        self.write_config("hosts = []\n")
        self.assertEqual(config.load_status_timeout(), 5)

        self.write_config("status_timeout = 12\n")
        self.assertEqual(config.load_status_timeout(), 12)

    def test_invalid_status_timeout_fails_clearly(self):
        for value in ("0", "-1", '"5"', "true"):
            with self.subTest(value=value):
                self.write_config(f"status_timeout = {value}\n")
                with self.assertRaisesRegex(SystemExit, "status_timeout must be a positive integer"):
                    config.load_status_timeout()

    def test_persistent_ssh_defaults_enabled_and_accepts_booleans(self):
        self.write_config("hosts = []\n")
        self.assertTrue(config.load_persistent_ssh())

        self.write_config("persistent_ssh = true\n")
        self.assertTrue(config.load_persistent_ssh())

        self.write_config("persistent_ssh = false\n")
        self.assertFalse(config.load_persistent_ssh())

    def test_invalid_persistent_ssh_fails_clearly(self):
        for value in ('"true"', "1", "[]"):
            with self.subTest(value=value):
                self.write_config(f"persistent_ssh = {value}\n")
                with self.assertRaisesRegex(SystemExit, "persistent_ssh must be a boolean"):
                    config.load_persistent_ssh()

    def test_option_like_hosts_are_rejected(self):
        for host in ("-V", "-F", "--help"):
            with self.subTest(host=host):
                self.write_config(f'hosts = ["{host}"]\n')
                with self.assertRaisesRegex(SystemExit, rf"Invalid config .*Invalid host: {host!r}"):
                    config.load_hosts()

    def test_missing_sessions_file_loads_empty(self):
        self.assertEqual(config.load_sessions(), [])

    def test_sessions_preserve_order_ignore_blanks_and_deduplicate(self):
        stars = Path(self.tempdir.name) / "sessions"
        stars.write_text("\nssh:dev:notes\nlocal:work\nssh:dev:notes\n\n")

        self.assertEqual(
            config.load_sessions(),
            [config.parse_target("ssh:dev:notes"), config.parse_target("local:work")],
        )

    def test_invalid_session_reports_file_context(self):
        stars = Path(self.tempdir.name) / "sessions"
        stars.write_text("bad target\n")

        with self.assertRaisesRegex(SystemExit, rf"Invalid favorite in {stars}"):
            config.load_sessions()

    def test_save_sessions_preserves_supplied_order(self):
        favorites = [config.parse_target("ssh:dev:work"), config.parse_target("local:notes")]

        config.save_sessions(favorites)

        self.assertEqual((Path(self.tempdir.name) / "sessions").read_text(), "ssh:dev:work\nlocal:notes\n")


if __name__ == "__main__":
    unittest.main()
