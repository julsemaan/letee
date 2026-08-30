import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from letee import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env = patch.dict("letee.config.os.environ", {"LETEE_CONFIG_DIR": self.tempdir.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        config.set_server("default")

    def write_config(self, text):
        path = Path(self.tempdir.name) / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_fresh_config_contains_default_prefix(self):
        cfg, wrapper = config.ensure_config()

        self.assertEqual(cfg.read_text(), 'hosts = []\nprefix = "C-s"\nsidebar_width = 40\nstatus_timeout = 5\nagent_panel_resize_step = 5\npersistent_ssh = true\ntmux_config_overlay = true\n')
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

    def test_agent_panel_resize_step_defaults_to_5_and_accepts_range(self):
        self.write_config("hosts = []\n")
        self.assertEqual(config.load_agent_panel_resize_step(), 5)

        for value in (1, 5, 100):
            with self.subTest(value=value):
                self.write_config(f"agent_panel_resize_step = {value}\n")
                self.assertEqual(config.load_agent_panel_resize_step(), value)

    def test_invalid_agent_panel_resize_step_fails_clearly(self):
        for value in ("true", "false", '"5"', "0", "-1", "101"):
            with self.subTest(value=value):
                self.write_config(f"agent_panel_resize_step = {value}\n")
                with self.assertRaisesRegex(
                    SystemExit, "agent_panel_resize_step must be an integer from 1 through 100"
                ):
                    config.load_agent_panel_resize_step()

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

    def test_tmux_config_overlay_defaults_enabled_and_accepts_booleans(self):
        self.write_config("hosts = []\n")
        self.assertTrue(config.load_tmux_config_overlay())

        self.write_config("tmux_config_overlay = true\n")
        self.assertTrue(config.load_tmux_config_overlay())

        self.write_config("tmux_config_overlay = false\n")
        self.assertFalse(config.load_tmux_config_overlay())

    def test_invalid_tmux_config_overlay_fails_clearly(self):
        for value in ('"true"', "1", "[]", "{ a = 1 }"):
            with self.subTest(value=value):
                self.write_config(f"tmux_config_overlay = {value}\n")
                with self.assertRaisesRegex(SystemExit, "tmux_config_overlay must be a boolean"):
                    config.load_tmux_config_overlay()

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

    def test_replace_session_preserves_order_and_removes_duplicates(self):
        old = config.parse_target("local:old")
        renamed = config.parse_target("local:new")
        other = config.parse_target("ssh:dev:other")
        config.save_sessions([old, renamed, other, renamed])

        self.assertEqual(config.replace_session(old, renamed), [renamed, other])
        self.assertEqual(config.load_sessions(), [renamed, other])

    def test_replace_session_updates_only_current_server(self):
        old = config.parse_target("local:old")
        renamed = config.parse_target("local:new")
        config.save_sessions([old], server="named")
        config.set_server("default")
        config.save_sessions([old])

        config.replace_session(old, renamed)

        self.assertEqual(config.load_sessions(), [renamed])
        self.assertEqual(config.load_sessions(server="named"), [old])

    def test_named_server_sessions_are_isolated_under_servers(self):
        target = config.parse_target("local:work")

        config.set_server("work")
        self.assertEqual(config.load_sessions(), [])
        config.save_sessions([target])

        named_path = Path(self.tempdir.name) / "servers" / "work" / "sessions"
        self.assertEqual(named_path.read_text(), "local:work\n")
        config.set_server("default")
        self.assertEqual(config.load_sessions(), [])

    def test_server_argument_selects_sessions_without_changing_shared_config_paths(self):
        target = config.parse_target("local:personal")

        config.save_sessions([target], server="personal")

        self.assertEqual(config.load_sessions(server="personal"), [target])
        self.assertEqual(config.paths(), (
            Path(self.tempdir.name) / "config.toml",
            Path(self.tempdir.name) / "wrapper.tmux.conf",
        ))


class KeybindingConfigTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.env = patch.dict("letee.config.os.environ", {"LETEE_CONFIG_DIR": self.tempdir.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        config.set_server("default")

    def write_config(self, text):
        path = Path(self.tempdir.name) / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_keybindings_default_without_table(self):
        self.write_config("hosts = []\n")
        self.assertEqual(config.load_keybindings(), config.DEFAULT_KEYBINDINGS)
        self.assertEqual(config.load_sidebar_keybindings(), config.DEFAULT_SIDEBAR_KEYBINDINGS)

    def test_partial_override_merges_with_defaults(self):
        self.write_config('[keybindings]\nfocus_agents = "C-a"\n')
        result = config.load_keybindings()
        self.assertEqual(result["focus_agents"], "C-a")
        self.assertEqual(result["focus_sessions"], "prefix+s")
        self.assertEqual(len(result), len(config.DEFAULT_KEYBINDINGS))
        self.write_config('[sidebar_keybindings]\nrename = "R"\n')
        sresult = config.load_sidebar_keybindings()
        self.assertEqual(sresult["rename"], "R")
        self.assertEqual(sresult["remove"], "r")

    def test_prefix_form_is_accepted_and_preserved(self):
        self.write_config('[keybindings]\nfocus_agents = "prefix+C-a"\n')
        result = config.load_keybindings()
        self.assertEqual(result["focus_agents"], "prefix+C-a")
        self.write_config('[keybindings]\nfocus_agents = "prefix+a"\n')
        self.assertEqual(config.load_keybindings()["focus_agents"], "prefix+a")
        # plain without prefix is global binding
        self.write_config('[keybindings]\nfocus_agents = "C-a"\n')
        self.assertEqual(config.load_keybindings()["focus_agents"], "C-a")

    def test_outer_alias_normalizes_to_canonical_action(self):
        self.write_config('[keybindings]\nagents = "C-a"\n')

        result = config.load_keybindings()

        self.assertEqual(result["focus_agents"], "C-a")
        self.assertNotIn("agents", result)

    def test_sidebar_alias_normalizes_to_canonical_action(self):
        self.write_config('[sidebar_keybindings]\ndown = "d"\n')

        result = config.load_sidebar_keybindings()

        self.assertEqual(result["navigate_down"], "d")
        self.assertNotIn("down", result)

    def test_alias_and_canonical_action_cannot_both_be_configured(self):
        self.write_config('[keybindings]\nfocus_agents = "C-a"\nagents = "C-b"\n')

        with self.assertRaisesRegex(SystemExit, "duplicate keybindings action 'focus_agents'"):
            config.load_keybindings()

    def test_existing_config_without_tables_remains_valid(self):
        self.write_config('hosts = []\nprefix = "C-s"\nsidebar_width = 40\n')
        # should not raise
        self.assertEqual(config.load_keybindings()["quit"], "prefix+q")
        self.assertEqual(config.load_sidebar_keybindings()["navigate_down"], "j")

    def test_unknown_outer_action_fails(self):
        self.write_config('[keybindings]\nunknown_action = "a"\n')
        with self.assertRaisesRegex(SystemExit, "unknown"):
            config.load_keybindings()

    def test_unknown_sidebar_action_fails(self):
        self.write_config('[sidebar_keybindings]\nunknown = "a"\n')
        with self.assertRaisesRegex(SystemExit, "unknown"):
            config.load_sidebar_keybindings()

    def test_duplicate_outer_binding_fails(self):
        self.write_config('[keybindings]\nfocus_agents = "z"\nfocus_sessions = "z"\n')
        with self.assertRaisesRegex(SystemExit, "duplicate"):
            config.load_keybindings()

    def test_duplicate_sidebar_binding_fails(self):
        self.write_config('[sidebar_keybindings]\nrename = "x"\nremove = "x"\n')
        with self.assertRaisesRegex(SystemExit, "duplicate"):
            config.load_sidebar_keybindings()

    def test_reserved_numeric_slots_are_rejected(self):
        for slot in ("1", "5", "9"):
            with self.subTest(slot=slot):
                self.write_config(f'[keybindings]\nfocus_agents = "{slot}"\n')
                with self.assertRaisesRegex(SystemExit, "reserved"):
                    config.load_keybindings()

    def test_prefix_conflict_is_rejected(self):
        self.write_config('prefix = "C-a"\n[keybindings]\nfocus_agents = "C-a"\n')
        with self.assertRaisesRegex(SystemExit, "conflicts with prefix"):
            config.load_keybindings()
        # default that conflicts with new prefix also fails
        self.write_config('prefix = "a"\n')
        with self.assertRaisesRegex(SystemExit, "conflicts with prefix"):
            config.load_keybindings()

    def test_invalid_tmux_tokens_are_rejected(self):
        for token in ('"C-"', '""', '"C x"', '"invalid"'):
            with self.subTest(token=token):
                self.write_config(f"[keybindings]\nfocus_agents = {token}\n")
                with self.assertRaisesRegex(SystemExit, "tmux key token"):
                    config.load_keybindings()

    def test_sidebar_single_character_validation(self):
        for value in ('"ab"', '""', '" "', '"\\t"'):
            with self.subTest(value=value):
                self.write_config(f"[sidebar_keybindings]\nrename = {value}\n")
                with self.assertRaisesRegex(SystemExit, "single-character"):
                    config.load_sidebar_keybindings()
        # valid single chars pass
        self.write_config('[sidebar_keybindings]\nrename = "R"\n')
        self.assertEqual(config.load_sidebar_keybindings()["rename"], "R")

    def test_sidebar_reserved_slots_rejected(self):
        self.write_config('[sidebar_keybindings]\nrename = "1"\n')
        with self.assertRaisesRegex(SystemExit, "reserved"):
            config.load_sidebar_keybindings()


if __name__ == "__main__":
    unittest.main()
