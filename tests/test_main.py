import unittest
from unittest.mock import patch

from mtmux.__main__ import main
from mtmux.discovery import SessionSnapshot, SourceSnapshot
from mtmux.names import Target


class MainTest(unittest.TestCase):
    def test_switch_session_uses_persisted_favorite_order(self):
        favorites = [
            Target("ssh", "alpha", "dev"),
            Target("local", "zeta"),
            Target("local", "alpha"),
        ]
        target = Target("local", "zeta")
        with (
            patch("mtmux.__main__.load_sessions", return_value=favorites),
            patch("mtmux.__main__.sessions.attach_command", return_value="attach") as attach_command,
            patch("mtmux.__main__.cockpit.switch") as switch,
        ):
            main(["switch-session", "2"])

        attach_command.assert_called_once_with(target)
        switch.assert_called_once_with(target, "attach")

    def test_switch_session_rejects_empty_slot_without_switching(self):
        with (
            patch("mtmux.__main__.load_sessions", return_value=[Target("local", "work")]),
            patch("mtmux.__main__.cockpit.switch") as switch,
        ):
            with self.assertRaisesRegex(SystemExit, "^No session in slot 2$"):
                main(["switch-session", "2"])

        switch.assert_not_called()

    def test_switch_session_rejects_slots_outside_one_to_nine(self):
        for slot in ("0", "10", "x"):
            with self.subTest(slot=slot), patch("mtmux.__main__.cockpit.switch") as switch:
                with self.assertRaises(SystemExit):
                    main(["switch-session", slot])
                switch.assert_not_called()

    def test_create_ssh_rejects_option_like_hosts(self):
        for host in ("-V", "-F", "--help"):
            with self.subTest(host=host):
                with patch("mtmux.__main__.sessions.create") as create:
                    with self.assertRaisesRegex(SystemExit, rf"Invalid host: {host!r}"):
                        main(["create", "ssh", "--", host, "work"])
                    create.assert_not_called()

    def test_create_local_keeps_option_like_session_support(self):
        target = Target("local", "-V")
        with (
            patch("mtmux.__main__.sessions.create") as create,
            patch("mtmux.__main__.sessions.attach_command", return_value="attach") as attach_command,
            patch("mtmux.__main__.cockpit.switch") as switch,
        ):
            main(["create", "local", "--", "-V"])

        create.assert_called_once_with(target)
        attach_command.assert_called_once_with(target)
        switch.assert_called_once_with(target, "attach")

    def test_list_uses_session_snapshot_and_displays_local_errors(self):
        snapshot = SessionSnapshot(
            SourceSnapshot(False, (), frozenset(), "permission denied"),
            {
                "dev": SourceSnapshot(True, (Target("ssh", "work", "dev"),), frozenset()),
                "off": SourceSnapshot(False, (), frozenset(), "offline"),
            },
        )
        with patch("mtmux.__main__.discover", return_value=snapshot), patch("builtins.print") as print_:
            main(["list"])

        self.assertEqual(
            [call.args[0] for call in print_.call_args_list],
            ["local unavailable: permission denied", "ssh:dev:work", "ssh:off unavailable"],
        )

    def test_failed_create_never_switches(self):
        with (
            patch("mtmux.__main__.sessions.create", side_effect=SystemExit("create failed")),
            patch("mtmux.__main__.cockpit.switch") as switch,
        ):
            with self.assertRaisesRegex(SystemExit, "create failed"):
                main(["create", "local", "work"])

        switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
