"""Pure workspace navigation and path-aware ordered-letter search."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .discovery import SUPPORTED_TASK_STATES
from .workspace import Agent, Identity, Session, Window, Workspace

SCOPES = ("All", "Sessions", "Windows", "Agents")
FILTER_STATES = tuple(sorted(SUPPORTED_TASK_STATES | {"idle", "unknown"}))


@dataclass(frozen=True)
class Row:
    key: Identity
    kind: Literal["session", "window", "agent"]
    label: str
    depth: int
    has_children: bool
    expanded: bool
    current: bool
    highlights: frozenset[int]
    path: str
    session: Session
    window: Window | None = None
    agent: Agent | None = None

    @property
    def available(self) -> bool:
        return self.session.available


def _match(text: str, term: str) -> tuple[tuple[int, int], frozenset[int]] | None:
    """Rank exact, contiguous, then shortest-span subsequences, in folded text."""
    folded = text.casefold()
    positions = [i for i, char in enumerate(text) for _ in char.casefold()]
    start = folded.find(term)
    if start >= 0:
        rank = (0 if folded == term else 1, 0)
        return rank, frozenset(positions[start:start + len(term)])
    best = None
    for start, char in enumerate(folded):
        if char != term[0]:
            continue
        matched = [start]
        for char in term[1:]:
            index = folded.find(char, matched[-1] + 1)
            if index < 0:
                break
            matched.append(index)
        else:
            rank = (2, matched[-1] - start + 1 - len(term))
            if best is None or rank < best[0]:
                best = rank, frozenset(positions[i] for i in matched)
    return best


class Finder:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.query = ""
        self.scope = "All"
        self.states: frozenset[str] = frozenset()
        self.has_bell = False
        self.selected: Identity | None = None
        self.rows: list[Row] = []
        self._selected_path = ""
        self._expansions: dict[tuple[str, bool], set[Identity]] = {("All", False): set()}
        self._known_branches: dict[tuple[str, bool], set[Identity]] = {}
        self._rebuild(choose=True)

    @property
    def selected_row(self) -> Row | None:
        return next((row for row in self.rows if row.key == self.selected), None)

    @property
    def unavailable(self) -> bool:
        row = self.selected_row
        return self.selected is not None and (row is None or not row.available)

    @property
    def selected_path(self) -> str:
        return self._selected_path

    def destination(self, key: Identity | None) -> Row | None:
        """Look up a destination even when its branch is collapsed."""
        return self._nodes.get(key)

    def set_query(self, text: str) -> None:
        if text == self.query:
            return
        self.query = text
        if self._mode != ("All", False):
            self._expansions.pop(self._mode, None)
        self._rebuild(choose=True)

    def set_scope(self, scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"Unknown finder scope: {scope}")
        if scope != self.scope:
            self.scope = scope
            if self._mode != ("All", False):
                self._expansions.pop(self._mode, None)
            self._rebuild(choose=True)

    def set_filters(self, states: set[str] | frozenset[str], has_bell: bool) -> None:
        states = frozenset(states)
        if states == self.states and has_bell == self.has_bell:
            return
        self.states = states
        self.has_bell = has_bell
        if self._mode != ("All", False):
            self._expansions.pop(self._mode, None)
        self._rebuild(choose=True)

    def cycle_scope(self) -> None:
        self.set_scope(SCOPES[(SCOPES.index(self.scope) + 1) % len(SCOPES)])

    def move(self, delta: int) -> None:
        if not self.rows:
            return
        index = next((i for i, row in enumerate(self.rows) if row.key == self.selected), None)
        index = (0 if delta >= 0 else len(self.rows) - 1) if index is None else max(
            0, min(len(self.rows) - 1, index + delta))
        self.select(self.rows[index].key)

    def select(self, key: Identity) -> None:
        if any(row.key == key for row in self.rows):
            self.selected = key
            self._cache_path()

    def toggle(self, key: Identity | None = None) -> None:
        key = self.selected if key is None else key
        row = next((row for row in self.rows if row.key == key), None)
        if row is None or not row.has_children:
            return
        expanded = self._expansions[self._mode]
        if key in expanded:
            expanded.remove(key)
            if self.selected and self.selected[:len(key)] == key:
                self.selected = key
        else:
            expanded.add(key)
        self._rebuild()

    def update(self, workspace: Workspace) -> None:
        selected = self.destination(self.selected)
        if (selected is not None and selected.session.pending
                and selected.session.session_id.startswith("missing:")):
            # A pending source has not supplied an identity yet. Resolve that
            # initial selection, but never substitute a confirmed vanished ID.
            discovered = next((session for session in workspace.sessions
                               if session.target == selected.session.target and session.available), None)
            if discovered is not None:
                self.selected = discovered.key
        self.workspace = workspace
        self._rebuild(choose=self.selected is None)

    @property
    def _mode(self) -> tuple[str, bool]:
        return self.scope, bool(self.query.split() or self.states or self.has_bell)

    def _cache_path(self) -> None:
        if self.selected in self._nodes:
            self._selected_path = self._nodes[self.selected].path
        elif self.selected is None:
            self._selected_path = ""

    def _rebuild(self, choose: bool = False) -> None:
        nodes: dict[Identity, Row] = {}
        children: dict[Identity | None, list[Identity]] = {None: []}
        parents: dict[Identity, Identity | None] = {}
        fields: dict[Identity, list[tuple[Identity, str, int]]] = {}

        def add(key, kind, label, session, window=None, agent=None, parent=None, own_fields=()):
            depth = {"session": 0, "window": 1, "agent": 2}[kind]
            path = (nodes[parent].path + " / " if parent else "") + label
            current = self.workspace.current
            nodes[key] = Row(key, kind, label, depth, False, False,
                             bool(current and current[:len(key)] == key),
                             frozenset(), path, session, window, agent)
            parents[key] = parent
            children.setdefault(parent, []).append(key)
            children[key] = []
            fields[key] = fields.get(parent, []) + [(key, text, offset) for text, offset in own_fields]

        for session in self.workspace.sessions:
            host = session.target.host or "local"
            name = session.target.session
            add(session.key, "session", f"{name} [{host}]", session,
                own_fields=((name, 0), (host, len(name) + 2)))
            for window in sorted(session.windows, key=lambda window: window.index):
                key = session.key + (window.window_id,)
                prefix = f"{window.index}: "
                add(key, "window", prefix + window.name, session, window, parent=session.key,
                    own_fields=((window.name, len(prefix)),))
                for agent in window.agents:
                    prefix = f"{agent.pane_index}: "
                    add(key + (agent.pane_target.pane_id, agent.agent_id), "agent",
                        f"{prefix}{agent.name} [{agent.state}]", session, window, agent, key,
                        ((agent.name, len(prefix)),))
        self._nodes = nodes
        eligible = set(nodes)
        if self.states or self.has_bell:
            eligible = set()
            for key, row in nodes.items():
                # A session bell alone includes even branches without agents.
                session_bell = self.has_bell and row.session.bell
                if not self.states and session_bell:
                    eligible.add(key)
                elif (row.agent is not None
                      and (not self.states or row.agent.state in self.states)
                      and (not self.has_bell or session_bell or row.agent.alert)):
                    eligible.add(key)
            for key in tuple(eligible):
                parent = parents[key]
                while parent is not None:
                    eligible.add(parent)
                    parent = parents[parent]
        terms = self.query.casefold().split()
        destinations = set()
        scores = {}
        highlights: dict[Identity, set[int]] = {}
        kind = {"Sessions": "session", "Windows": "window", "Agents": "agent"}.get(self.scope)
        for key, row in nodes.items():
            if key not in eligible or (kind and row.kind != kind):
                continue
            matches = []
            for term in terms:
                choices = []
                for owner, text, offset in fields[key]:
                    match = _match(text, term)
                    if match is not None:
                        rank, positions = match
                        choices.append((rank, owner, offset, positions))
                if not choices:
                    break
                matches.append(min(choices, key=lambda choice: choice[0]))
            else:
                destinations.add(key)
                scores[key] = (sum(rank[0] == 2 for rank, *_ in matches),
                               sum(rank[0] == 1 for rank, *_ in matches),
                               sum(rank[1] for rank, *_ in matches))
                for _, owner, offset, positions in matches:
                    highlights.setdefault(owner, set()).update(offset + i for i in positions)

        included = set(destinations)
        # Keep a live selection openable if text or activity stops matching.
        if not choose and self.selected in nodes:
            included.add(self.selected)
            scores.setdefault(self.selected, (len(terms) + 1, 0, 0))
        for key in tuple(included):
            parent = parents[key]
            while parent is not None:
                included.add(parent)
                parent = parents[parent]

        # A branch takes its strongest destination's rank, but stays a tree.
        branch_scores = dict(scores)
        for key in reversed(nodes):
            if key in included and parents[key] is not None:
                parent = parents[key]
                score = branch_scores[key]
                branch_scores[parent] = min(branch_scores.get(parent, score), score)
        expandable = {key for key in included if any(child in included for child in children[key])}
        if self._mode not in self._expansions:
            self._expansions[self._mode] = set(expandable)
        elif self._mode != ("All", False):
            self._expansions[self._mode].update(expandable - self._known_branches[self._mode])
        self._known_branches[self._mode] = expandable
        expanded = self._expansions[self._mode]
        if not choose and self.selected in nodes:
            parent = parents[self.selected]
            while parent is not None:
                expanded.add(parent)
                parent = parents[parent]
        result = []
        ordered_destinations = []

        def visit(parent):
            keys = [key for key in children[parent] if key in included]
            if terms:
                keys.sort(key=lambda key: branch_scores[key])
            for key in keys:
                row = replace(nodes[key], has_children=key in expandable, expanded=key in expanded,
                              highlights=frozenset(highlights.get(key, ())))
                result.append(row)
                if key in destinations:
                    ordered_destinations.append(key)
                if key in expanded:
                    visit(key)

        visit(None)
        self.rows = result
        if choose:
            self.selected = min(ordered_destinations, key=lambda key: scores[key], default=None)
            if self._mode == ("All", False) and self.workspace.current:
                current_session = self.workspace.current[:4]
                if current_session in ordered_destinations:
                    self.selected = current_session
        self._cache_path()
