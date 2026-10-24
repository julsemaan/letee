from dataclasses import FrozenInstanceError, replace
import unittest

from finder_fixtures import sample_workspace
from letee.finder import Finder, SCOPES
from letee.names import Target
from letee.workspace import Session, Window, Workspace


class FinderTest(unittest.TestCase):
    def setUp(self):
        self.workspace = sample_workspace()
        self.finder = Finder(self.workspace)

    def test_initial_tree_contract_and_current_session(self):
        rows = self.finder.rows
        self.assertEqual(SCOPES, ("All", "Sessions", "Windows", "Agents"))
        self.assertEqual([r.key for r in rows], [s.key for s in self.workspace.sessions])
        self.assertEqual(self.finder.selected, self.workspace.sessions[0].key)
        self.assertEqual(rows[0].label, "letee [local]")
        self.assertTrue(rows[0].has_children)
        self.assertFalse(rows[0].expanded)
        self.assertTrue(rows[0].available)
        self.assertTrue(rows[0].current)
        self.assertEqual(rows[0].highlights, frozenset())
        with self.assertRaises(FrozenInstanceError):
            rows[0].label = "changed"
        self.finder.toggle()
        self.assertEqual([r.label for r in self.finder.rows[:3]],
                         ["letee [local]", "0: editor", "1: fix/123"])
        self.finder.toggle(self.workspace.sessions[0].key + ("@2",))
        agent = self.finder.rows[3]
        self.assertEqual(agent.key, self.workspace.current)
        self.assertTrue(agent.current)
        self.assertEqual(agent.label, "1: Claude [input-required]")
        self.assertEqual(agent.depth, 2)
        self.assertIsNotNone(agent.agent)
        self.assertIn("letee [local]", agent.path)
        self.assertIn("1: fix/123", agent.path)

    def test_path_terms_in_either_order_and_abbreviations(self):
        for query in ("dev letee fix/123", "fix/123 letee dev", "dv lt f13"):
            with self.subTest(query=query):
                self.finder.set_query(query)
                self.assertEqual([r.label for r in self.finder.rows],
                                 ["letee [dev]", "0: fix/123", "1: Codex [working]"])
                self.assertEqual(self.finder.selected_row.kind, "window")
                self.assertEqual(self.finder.selected_row.session.target.host, "dev")
        self.finder.set_query("dev codex")
        self.assertEqual(self.finder.selected_row.kind, "agent")

    def test_terms_cannot_cross_fields_siblings_or_search_metadata(self):
        for query in ("editor Claude", "deploy Codex", "leteefix", "input-required", "@1", "edtorx"):
            with self.subTest(query=query):
                self.finder.set_query(query)
                self.assertEqual(self.finder.rows, [])
                self.assertIsNone(self.finder.selected_row)
        self.finder.set_scope("Windows")
        self.finder.set_query("0")
        self.assertEqual(self.finder.rows, [])

    def test_session_match_reveals_branch_and_extra_term_narrows(self):
        self.finder.set_query("tools")
        self.assertEqual([r.kind for r in self.finder.rows], ["session", "window", "agent"])
        self.assertEqual(self.finder.selected_row.kind, "session")
        self.finder.set_query("letee editor")
        self.assertEqual([r.label for r in self.finder.rows], ["letee [local]", "0: editor"])
        self.assertEqual(self.finder.selected_row.kind, "window")

    def test_exact_substring_gapped_ranking_and_tree_branch_sort(self):
        sessions = tuple(Session(Target("local", name), "$" + str(i), "/s", windows)
                         for i, (name, windows) in enumerate((
                             ("first", (Window("@1", 0, "f---ix"),)),
                             ("second", (Window("@2", 0, "prefix"), Window("@3", 1, "fix"))),
                             ("fix", (Window("@4", 0, "other"),)),
                         )))
        finder = Finder(Workspace(sessions))
        finder.set_query("fix")
        self.assertEqual(finder.selected, sessions[1].key + ("@3",))
        self.assertEqual([r.label for r in finder.rows if r.kind == "session"],
                         ["second [local]", "fix [local]", "first [local]"])
        self.assertEqual([r.label for r in finder.rows if r.session == sessions[1]],
                         ["second [local]", "1: fix", "0: prefix"])

    def test_equal_independent_matches_use_tree_order_but_stronger_child_wins(self):
        session = Session(Target("local", "fix-project"), "$1", "/s", (
            Window("@1", 0, "fix-window"),
        ))
        finder = Finder(Workspace((session,)))
        finder.set_query("fix")
        self.assertEqual(finder.selected, session.key)
        finder.update(Workspace((replace(session, windows=(Window("@1", 0, "fix"),)),)))
        self.assertEqual(finder.selected, session.key)
        finder.set_query("FIX ")
        self.assertEqual(finder.selected, session.key + ("@1",))

    def test_contiguous_occurrence_beats_early_gaps_and_highlights(self):
        session = replace(self.workspace.sessions[0], windows=(Window("@1", 0, "a---b ab"),))
        finder = Finder(Workspace((session,)))
        finder.set_query("AB")
        self.assertEqual(finder.selected_row.highlights, frozenset({9, 10}))
        self.assertEqual(finder.selected_row.label, "0: a---b ab")
        finder.set_query("aab")
        self.assertEqual(finder.selected_row.highlights, frozenset({3, 9, 10}))

    def test_unicode_casefold_maps_to_original_characters(self):
        session = replace(self.workspace.sessions[0], windows=(Window("@1", 0, "Straße"),))
        finder = Finder(Workspace((session,)))
        finder.set_query("STRASSE")
        self.assertEqual(finder.selected_row.highlights, frozenset(range(3, 9)))
        finder.set_query("ss")
        self.assertEqual(finder.selected_row.highlights, frozenset({7}))

    def test_scopes_have_navigable_ancestry_and_scope_specific_children(self):
        self.finder.set_scope("Sessions")
        self.assertEqual(len(self.finder.rows), 3)
        self.assertFalse(any(r.has_children for r in self.finder.rows))
        self.finder.cycle_scope()
        self.assertEqual(self.finder.scope, "Windows")
        self.assertEqual(len(self.finder.rows), 8)
        self.assertFalse(any(r.has_children for r in self.finder.rows if r.kind == "window"))
        self.finder.cycle_scope()
        self.assertEqual(self.finder.scope, "Agents")
        self.assertEqual([r.kind for r in self.finder.rows], ["session", "window", "agent"] * 3)
        self.assertEqual(self.finder.selected_row.kind, "agent")
        self.finder.move(-1)
        self.assertEqual(self.finder.selected_row.kind, "window")
        self.finder.cycle_scope()
        self.assertEqual(self.finder.scope, "All")
        with self.assertRaises(ValueError):
            self.finder.set_scope("invalid")

    def test_expansion_toggle_and_restoring_browsing(self):
        session_key = self.workspace.sessions[0].key
        self.finder.toggle(session_key)
        browsing = [r.key for r in self.finder.rows]
        self.finder.set_query("Claude")
        self.finder.toggle(session_key)
        self.assertNotIn(self.workspace.current, [r.key for r in self.finder.rows])
        self.finder.set_scope("Windows")
        self.finder.toggle(session_key)
        self.finder.set_scope("All")
        self.finder.set_query("")
        self.assertEqual([r.key for r in self.finder.rows], browsing)
        self.finder.set_scope("Agents")
        self.finder.set_scope("All")
        self.assertEqual([r.key for r in self.finder.rows], browsing)

    def test_update_keeps_identity_rename_availability_and_disappearance(self):
        self.finder.set_query("Claude")
        key = self.finder.selected
        old_path = self.finder.selected_path
        session = self.workspace.sessions[0]
        window = session.windows[1]
        renamed = replace(window, name="renamed", agents=(replace(window.agents[0], name="Claude-new"),))
        changed = replace(session, windows=(renamed,), available=False)
        self.finder.update(replace(self.workspace, sessions=(changed,)))
        self.assertEqual(self.finder.selected, key)
        self.assertEqual(self.finder.selected_row.agent.name, "Claude-new")
        self.assertTrue(self.finder.unavailable)
        self.assertNotEqual(self.finder.selected_path, old_path)
        cached = self.finder.selected_path
        self.finder.update(Workspace())
        self.assertEqual(self.finder.selected, key)
        self.assertIsNone(self.finder.selected_row)
        self.assertTrue(self.finder.unavailable)
        self.assertEqual(self.finder.selected_path, cached)
        self.finder.update(self.workspace)
        self.assertEqual(self.finder.selected, key)
        self.assertFalse(self.finder.unavailable)

    def test_explicit_actions_recover_missing_selection(self):
        self.finder.set_query("Claude")
        self.finder.update(replace(self.workspace, sessions=self.workspace.sessions[1:]))
        self.assertIsNone(self.finder.selected_row)
        self.finder.move(1)
        self.assertIsNotNone(self.finder.selected_row)
        self.finder.select(self.workspace.sessions[2].key)
        self.assertEqual(self.finder.selected, self.workspace.sessions[2].key)
        self.finder.set_query("fix/456")
        self.assertEqual(self.finder.selected_row.window.name, "fix/456")

    def test_duplicate_names_use_identity_and_normal_order_breaks_ties(self):
        self.finder.set_query("fix/123")
        windows = [r for r in self.finder.rows if r.kind == "window"]
        self.assertEqual(len(windows), 2)
        self.assertNotEqual(windows[0].key, windows[1].key)
        self.assertEqual(self.finder.selected, windows[0].key)
        self.assertEqual([r.label for r in self.finder.rows if r.kind == "session"],
                         ["letee [local]", "letee [dev]"])

    def test_new_matching_paths_are_revealed_on_update(self):
        session = self.workspace.sessions[0]
        initial = replace(session, windows=(Window("@9", 0, "Claude"),))
        finder = Finder(Workspace((initial,)))
        finder.set_query("Claude")
        added = replace(session, windows=initial.windows + (session.windows[1],))
        finder.update(Workspace((added,)))
        self.assertIn(self.workspace.current, [row.key for row in finder.rows])
        self.assertEqual(finder.selected, session.key + ("@9",))
        finder.toggle(session.key)
        finder.update(Workspace((added,)))
        self.assertEqual([row.key for row in finder.rows], [session.key])

    def test_first_live_data_selects_current_session_or_best_match(self):
        for query, expected_kind in (("", "session"), ("Codex", "agent")):
            finder = Finder(Workspace(loading=True))
            finder.set_query(query)
            finder.update(self.workspace)
            self.assertIsNotNone(finder.selected_row)
            self.assertEqual(finder.selected_row.kind, expected_kind)

    def test_clearing_query_reveals_windows_after_previous_filtered_collapse(self):
        self.finder.set_scope("Windows")
        self.finder.toggle(self.workspace.sessions[0].key)
        self.finder.set_query("Codex")
        self.finder.set_query("")
        self.assertEqual(len(self.finder.rows), 8)
        self.assertEqual(self.finder.selected_row.kind, "window")

    def test_pending_session_selection_resolves_first_discovered_identity(self):
        session = self.workspace.sessions[1]
        pending = replace(session, session_id="missing:letee", socket_path="", windows=(), available=False, pending=True)
        finder = Finder(Workspace((pending,), current=pending.key, loading=True))
        self.assertEqual(finder.selected, pending.key)
        finder.update(Workspace((session,)))
        self.assertEqual(finder.selected, session.key)
        self.assertFalse(finder.unavailable)
        self.assertFalse(finder.rows[0].expanded)
        # Confirmed missing sessions are different: a new session must not
        # silently replace a destination the user already saw disappear.
        finder = Finder(Workspace((replace(pending, pending=False),), loading=True))
        finder.update(Workspace((session,)))
        self.assertEqual(finder.selected, pending.key)
        self.assertTrue(finder.unavailable)

    def test_current_session_selection_when_not_first(self):
        current = self.workspace.sessions[1].key + ("@1",)
        workspace = replace(self.workspace, current=current)
        finder = Finder(workspace)
        self.assertEqual(finder.selected, current[:4])
        self.assertTrue(finder.rows[1].current)
        finder = Finder(Workspace(loading=True))
        finder.update(workspace)
        self.assertEqual(finder.selected, current[:4])

    def test_scope_changes_reveal_best_match_after_collapsing_filtered_branch(self):
        self.finder.set_query("fix/123")
        self.finder.toggle(self.workspace.sessions[0].key)
        self.finder.set_scope("Sessions")
        self.finder.set_scope("All")
        self.assertEqual(self.finder.selected, self.workspace.sessions[0].key + ("@2",))
        self.finder.set_scope("Windows")
        self.finder.toggle(self.workspace.sessions[0].key)
        self.finder.set_query("fix/456")
        self.assertEqual(self.finder.selected_row.window.name, "fix/456")

    def test_new_stronger_match_does_not_reselect_and_renamed_destination_stays_openable(self):
        self.finder.set_query("editor")
        selected = self.finder.selected
        session = self.workspace.sessions[0]
        changed = replace(session, windows=(replace(session.windows[0], name="terminal"),))
        self.finder.update(replace(self.workspace, sessions=(changed, self.workspace.sessions[1])))
        self.assertEqual(self.finder.selected, selected)
        self.assertFalse(self.finder.unavailable)
        self.assertEqual(self.finder.selected_row.window.name, "terminal")
        self.assertEqual(self.finder.destination(selected).window.name, "terminal")
        self.finder.set_scope("Windows")
        self.assertIsNone(self.finder.selected_row)

    def test_update_renamed_out_of_query_keeps_selection_and_refreshes_cached_path(self):
        self.finder.set_query("editor")
        selected = self.finder.selected
        session = self.workspace.sessions[0]
        renamed = replace(session, windows=(replace(session.windows[0], name="terminal"),))
        self.finder.update(Workspace((renamed,)))
        self.assertEqual(self.finder.selected, selected)
        self.assertEqual(self.finder.selected_row.window.name, "terminal")
        self.assertFalse(self.finder.unavailable)
        self.assertIn("terminal", self.finder.selected_path)
        self.finder.set_query("terminal")
        self.assertEqual(self.finder.selected, selected)
        self.assertIsNotNone(self.finder.selected_row)

    def test_empty_workspace_and_initial_fallback_and_window_order(self):
        finder = Finder(Workspace())
        self.assertEqual(finder.rows, [])
        self.assertIsNone(finder.selected)
        self.assertFalse(finder.unavailable)
        finder.move(1)
        finder.toggle()
        session = self.workspace.sessions[0]
        finder = Finder(Workspace((replace(session, windows=tuple(reversed(session.windows))),)))
        self.assertEqual(finder.selected, session.key)
        finder.toggle()
        self.assertEqual([r.window.index for r in finder.rows if r.window], [0, 1])


if __name__ == "__main__":
    unittest.main()
