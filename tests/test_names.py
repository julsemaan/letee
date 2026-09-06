import unittest

from letee.names import INNER_SERVER_SOCKET, PaneTarget, Target, normalize_server, server_socket, validate_server


class ServerNameTest(unittest.TestCase):
    def test_default_and_named_servers_map_to_outer_socket_names(self):
        self.assertEqual(normalize_server(None), "default")
        self.assertEqual(server_socket("default"), "letee")
        self.assertEqual(server_socket("work"), "letee-work")
        self.assertEqual(INNER_SERVER_SOCKET, "letee.inner")

    def test_server_names_use_safe_name_format(self):
        for value in ("work", "personal_1", "prod.dev-2"):
            self.assertEqual(validate_server(value), value)
        for value in ("", "bad name", "/tmp/socket", "-V", "x" * 65):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "Invalid server"):
                validate_server(value)


class TargetTest(unittest.TestCase):
    def test_accepts_valid_local_and_ssh_targets(self):
        self.assertEqual(Target("local", "-V").format(), "local:-V")
        self.assertEqual(Target("ssh", "work", "dev").format(), "ssh:dev:work")

    def test_rejects_invalid_construction(self):
        cases = (
            (("other", "work", None), "Invalid target kind"),
            (("local", "bad name", None), "Invalid session"),
            (("local", "work", "dev"), "Local target must not have host"),
            (("ssh", "work", None), "Invalid host"),
            (("ssh", "work", "-V"), "Invalid host"),
        )
        for args, message in cases:
            with self.subTest(args=args), self.assertRaisesRegex(SystemExit, message):
                Target(*args)


class PaneTargetTest(unittest.TestCase):
    def test_accepts_valid_local_and_ssh_panes_by_value(self):
        local = PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux.sock")
        self.assertEqual(local, PaneTarget(Target("local", "work"), "@1", "%2", "/tmp/tmux.sock"))
        self.assertEqual(
            PaneTarget(Target("ssh", "work", "dev"), "@3", "%4", "/run/tmux/socket"),
            PaneTarget(Target("ssh", "work", "dev"), "@3", "%4", "/run/tmux/socket"),
        )

    def test_window_name_does_not_change_identity(self):
        target = Target("local", "work")
        pane = PaneTarget(target, "@1", "%2", "/tmp/tmux.sock", "editor")
        renamed = PaneTarget(target, "@1", "%2", "/tmp/tmux.sock", "shell")

        self.assertEqual(pane, renamed)
        self.assertEqual(hash(pane), hash(renamed))

    def test_rejects_invalid_ids_and_empty_socket(self):
        target = Target("local", "work")
        for args, message in (
            ((target, "1", "%2", "/tmp/tmux"), "Invalid window ID"),
            ((target, "@1", "2", "/tmp/tmux"), "Invalid pane ID"),
            ((target, "@1", "%2", ""), "Invalid socket path"),
        ):
            with self.subTest(args=args), self.assertRaisesRegex(SystemExit, message):
                PaneTarget(*args)


if __name__ == "__main__":
    unittest.main()
