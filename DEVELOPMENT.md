# Development guide

This guide covers local development and manual integration testing for letee. See the [README](https://github.com/julsemaan/letee/blob/main/README.md) for user-facing installation and usage instructions.

## Set up

Install letee in editable mode with its development tools:

```sh
make dev-install
```

## Verify changes

Run the checks that match your change:

```sh
make test          # unit tests
make lint          # Ruff
make coverage      # branch coverage (85% minimum)
make build-check   # build distributions and validate metadata
make check         # all quality gates
```

## Session coordination

`letee.operations` owns server-scoped session transactions. CLI commands and sidebar workers submit intent there rather than acquire `sessions_lock` themselves. Reload tracking, validate automatic work, prepare attachments, and publish launches within one transaction. Never nest lock acquisitions or wait for a transaction on the curses thread. Status synchronization and startup-marker cleanup run in workers too.

The coordination rules are:

- Intentional kill, removal, and rename invalidate queued restoration. Automatic work rechecks persisted launch identity, per-target revision, and tracking under the lock. Renaming A to B and back to A still invalidates previously queued work for A. Explicit navigation may select an intentionally removed target again.
- Every attachment launch gets a new persisted identity, even for the same target. Delayed overlays, status samples, and startup cleanup cannot modify a newer launch.
- A completion releases only its own pending request, even if its launch is obsolete. Obsolete results cannot update current UI state or clear newer pending work.
- Attachment startup retains its deadline and discovery-freshness checks. A cached discovery sample cannot confirm a newly launched attachment. `_awaiting_discovery` remains necessary because pane respawn confirms launch, not session creation.
- Killing the active session retains the expected-pane-death handshake. Successful kill or rename persists tracking even if subsequent pane updates fail.

Keep regression scenarios for queued restoration versus kill/remove/rename, same-target relaunch versus delayed results, slow preparation, slot lookup under the lock, and CLI create remaining untracked. Run `make test`, not host E2E tests.

The coordination migration stays in this PR because extracting the partially migrated callers would leave restoration safeguards incomplete. A future prerequisite extraction should move operations, persistence, and all CLI/sidebar callers together, with the coordination regressions; leave automatic restoration and discovery-specific policy in the feature PR. Do not consolidate CLI and sidebar startup paths without proving their deadline and freshness behavior equivalent.

## End-to-end tests

WARNING: These tests are not working (yet). Only use `make test` for now.

End-to-end tests interact with tmux and must run in Docker:

```sh
make test-e2e-docker
```

Do not invoke `pytest` directly or run end-to-end tests against host tmux. Docker is required; do not replace the Docker guard with mocks or host-side workarounds.

## Manual SSH latency testing

Use `tools/ssh_latency_proxy.py` to test letee against a slow SSH connection without root access or global network shaping. Add a separate alias so normal host connections remain unaffected:

```sshconfig
Host dev-slow
    HostName dev.example.com
    User me
    ProxyCommand python3 /absolute/path/tools/ssh_latency_proxy.py --delay-ms 300 %h %p
```

Configure letee to use the delayed alias:

```toml
hosts = ["dev-slow"]
persistent_ssh = false
```

`--delay-ms` adds one-way delay, so `300ms` approximates `600ms` RTT. Set `persistent_ssh = false` so letee's OpenSSH multiplexing does not bypass the proxy after the first connection. Run a direct smoke test with:

```sh
ssh dev-slow
```

### Reconnecting SSH sessions

Use a separate alias and marker file to disconnect active `ProxyCommand` connections. While the marker exists, new proxy connections fail too. This does not produce TCP `connection refused`: the proxy exits before connecting, so OpenSSH reports that the remote connection closed during key exchange.

```sshconfig
Host dev-reconnect
    HostName dev.example.com
    User me
    ProxyCommand python3 /absolute/path/tools/ssh_latency_proxy.py --disconnect-while-file /tmp/letee-dev-reconnect.outage %h %p
```

Configure letee to use the alias with persistent SSH enabled:

```toml
hosts = ["dev-reconnect"]
persistent_ssh = true
```

Start letee, then run this loop in another terminal:

```sh
outage=/tmp/letee-dev-reconnect.outage
rm -f "$outage"
for cycle in 1 2 3 4 5; do
    touch "$outage"
    sleep 3
    rm -f "$outage"
    sleep 8
done
```

The right pane should reconnect after each cycle without another sidebar click.

### Pausing SSH traffic

Use `--timeout-while-file` to pause forwarding in both directions without closing the SSH connection. Removing the marker resumes forwarding if SSH has not given up.

```sshconfig
Host dev-timeout
    HostName dev.example.com
    User me
    ProxyCommand python3 /absolute/path/tools/ssh_latency_proxy.py --timeout-while-file /tmp/letee-dev-timeout.outage %h %p
```

You can combine both modes in one `ProxyCommand`. Use separate marker files. The disconnect marker takes precedence: it rejects a new connection at startup and closes an active connection even while the timeout marker exists.

```sshconfig
Host dev-reconnect-timeout
    HostName dev.example.com
    User me
    ProxyCommand python3 /absolute/path/tools/ssh_latency_proxy.py --disconnect-while-file /tmp/letee-dev-disconnect.outage --timeout-while-file /tmp/letee-dev-timeout.outage %h %p
```

## Diagnostic coverage

Enable and collect logs as described in [usage](docs/usage.md#debug-logging). Events describe decisions, not terminal contents. Pair short IDs with `run_id` and `server`. `event_id` identifies a record; `operation_id`/`parent_operation_id` describe nested work. Run-qualified `error_id` values link repeated observations of an exception object, including each entry in `causes`, even through `raise ... from None`. UTC timestamps help compare processes; monotonic timestamps and durations describe local ordering. File order is not a cross-process causal order.

### Mechanism, event and test inventory

These are representative unit checks, not proof that every branch emits an event. Test names below are methods in `tests/test_<module>.py`. Operation names appear on `operation_start`/`operation_end`, with duration and outcome on completion.

| Mechanism | Events or operation names | Representative test |
| --- | --- | --- |
| Sink lifecycle and exception chains | `error`, `operation_start`/`operation_end`; sink warnings go to stderr | diagnostics: `test_error_chain_ids_link_translation_without_messages_or_source`, `test_serialization_flush_and_close_failures_are_safe`, `test_setup_failures_close_descriptor_and_warn_without_details`, `test_recording_and_error_capture_failures_never_replace_application_error` |
| Queue bounds and unexpected threads | `dropped_records` on subsequent records; `error` with `operation="thread"`, followed by the original thread hook | diagnostics: `test_slow_sink_bounds_flush_shutdown_and_queue_growth`, `test_unexpected_thread_capture_delegates_to_original_hook`, `test_warning_failure_does_not_escape` |
| Config, names and persisted sessions | `error` with reason/handling; `config.sessions_missing` | config: `test_validation_branches_report_specific_safe_exit_reasons`, `test_timing_numeric_overflow_logs_safe_fallback_and_exit`, `test_filesystem_failures_are_logged_and_reraised_unchanged`, `test_invalid_favorite_reports_safe_line_number`; names: `test_rejections_report_only_safe_reason_codes` |
| Bundled tmux selection and invocation | `tmux.command`, `tmux.bundle_fallback`, `error` for timeout, execution or unchecked nonzero status | tmux: `test_platform_fallbacks_report_safe_reasons`, `test_subprocess_failures_preserve_behavior_and_report_decisions`, `test_unchecked_nonzero_status_is_returned_and_reported` |
| CLI errors and server probes | `cli.server_tmux`/`cli.legacy_tmux`, `error` with `operation="cli"` or probe name; CLI flushes before returning or propagating | main: `test_cli_reported_errors_flush_diagnostics_without_private_exception_payloads`, `test_cli_propagation_flushes_and_retains_original_exception_privately`, `test_server_probe_and_legacy_cleanup_exceptions_are_private_fallbacks` |
| SSH agent, authentication and grouping | `sessions.agent_*`, `sessions.authentication`, `sessions.ssh_config`, `sessions.group_hosts`, `sessions.probe_hosts`, `ssh_preparation*` | sessions: `test_agent_directory_and_lock_failures_capture_fallback`, `test_lock_acquisition_failure_closes_open_file_and_keeps_fallback_observable`, `test_agent_start_failure_still_probes_for_concurrent_agent`, `test_parallel_authentication_retains_action_context`; cockpit: `test_authentication_group_probe_and_retry_have_distinct_origins` |
| Session mutation and local fallbacks | `sessions.command`, `sessions.create`/`kill`/`rename`/`kill_agent`, `sessions.overlay_*`, `error` with stage | sessions: `test_remaining_local_handlers_capture_only_safe_error_metadata`, `test_pane_parse_failures_are_observable`, `test_unlock_failure_preserves_error_and_closes_lock` |
| Remote agent termination and overlay | `sessions.kill_agent.remote` stage/category/query/signal counts; `sessions.overlay_source.remote` ignored status | sessions: `test_remote_helper_failures_preserve_messages_and_emit_safe_stage_counts`, `test_remote_failures_log_metadata_without_sensitive_error_text`, `test_remote_overlay_reports_ignored_failure_and_preserves_create_status` |
| Discovery parsing and bounded reads | `discovery.panes_parsed`, `discovery.agents_parsed`, `discovery.local_agents_read`, `discovery.source_result`, `discovery.output_read` | discovery: `test_invalid_timestamps_are_counted_without_values`, `test_local_read_error_has_safe_frames`, `test_remote_reader_emits_bounded_failure_summary`, `test_remote_metadata_is_allowlisted_and_capped`, `test_diagnostic_trailers_do_not_reduce_original_payload_budget`, `test_remote_reader_status_distinguishes_missing_python_and_failure` |
| Discovery requests, retry and cleanup | `discovery.request_*`, `discovery.scheduled`/`rescheduled`, `discovery.poll_state`; operations `discovery.master_check`/`process_cleanup` | discovery: `test_precheck_failure_and_cancellation_finish_attempt_with_context`, `test_tempfile_failure_finishes_attempt_and_closes_first_file`, `test_cleanup_failure_propagates_under_request_context`, `test_idle_state_changes_and_heartbeat_are_not_per_tick` |
| Status worker and stale-result rejection | `status_requested`, `status_worker_*`, `status_result`, `status_refresh_requested`/`status_discard_requested`; request/sample/snapshot IDs and generation | sidebar: `test_status_unexpected_future_failure_is_correlated_and_propagated`, `test_failed_status_submission_preserves_snapshot_and_request_trace`, `test_status_sample_trace_accepts_and_discards_without_changing_snapshot` |
| Keyboard/mouse to asynchronous effects | `input_received`, mouse decisions, `effect_requested`/`submitted`/`worker_*`/`result`/`applied`/`error`; input/action IDs | sidebar: `test_mouse_press_trace_correlates_receive_map_submit_and_completion`, `test_queued_navigation_actions_keep_distinct_ids`, `test_action_context_reaches_cockpit_switch` |
| Entries, rendering and idle progress | `entries_built`, `sidebar_entry_draw`, `sidebar_render`, `sidebar_heartbeat`; nested operations `sidebar_draw`/`sidebar_refresh`; entry/render IDs | sidebar: `test_accepted_status_links_discovery_requests_to_visible_entries`, `test_draw_and_refresh_failures_retry_with_inherited_render_correlation`, `test_unchanged_render_is_deduplicated_but_heartbeat_repeats` |
| Layout repair and terminal fallbacks | `layout_*`, `error` with handling/operation | cockpit: `test_layout_parse_and_tty_failures_are_ignored_privately`, `test_layout_signal_failures_abort_repair_privately`; sidebar: `test_layout_repair_handled_and_unexpected_failures_are_diagnosed`, `test_fallback_handlers_capture_types_without_exception_text` |
| Automatic recovery and effect failures | `restoration_requested`/`deferred`/`backoff`/`result`/`reset`, `effect_*`, correlated `error` | sidebar: `test_background_recovery_without_input_links_failure_backoff_and_retry`, `test_effect_worker_failure_preserves_context_through_collection`, `test_restoration_wrapped_timeout_records_cause_and_overlay_fallback`, `test_restoration_deferral_heartbeat_and_launch_reset_do_not_change_retry_equality` |
| Right-pane switch and attachment lifetime | `switch_*`, `attachment_requested`/`started`/`exited`; switch/attachment IDs, pane PID as exit generation | cockpit: `test_attachment_lifecycle_uses_unique_identity_and_omits_shell_command`, `test_pane_death_diagnostics_capture_identity_before_native_recovery`; main: `test_attachment_exit_callback_records_captured_state_without_tmux_queries` |

Discovery request IDs do not affect snapshot equality, checked by `test_snapshot_correlation_does_not_affect_equality` and `test_repeated_identical_results_do_not_change_with_new_request_ids`. Status and discovery request IDs identify different layers. `status_worker_completed` and `status_result` carry a `discovery_requests` map keyed by `local` and remote host, with `None` for pending sources. Accepted request/sample/snapshot IDs continue into `entries_built`, render events and `sidebar_entry_draw`. Draw and refresh errors inherit that trace plus entry/render IDs; refresh is a child of the draw operation. Do not infer complete parent-operation propagation across every worker.

### Exception audit and limits

`test_background_remote_recovery_reaches_screen_without_input_or_switching_active_local` follows an unavailable remote through recovery, accepted sampling and drawing while a local session remains active. `test_expected_exit_and_future_cancellation_are_not_failed_operations` and `test_successful_help_exit_is_not_reported_as_failure` distinguish normal exits and cancellation from failures. Remote discovery trailers use a separate 512-byte allowance, so diagnostic metadata does not reduce the existing 1 MiB payload limit.

No `suppress` remains in `letee/*.py`. Handlers report errors directly or through enclosing operations, aggregate parser rejections/read failures, or annotate cleanup decisions such as `term_wait_timed_out`, `term_group_missing` and `kill_group_missing`. The embedded remote reader reports counts, not per-file exceptions. Both remote kill-helper handlers now emit allowlisted stage/category and query/signal counts; missing or invalid metadata is reported separately. Unexpected thread exceptions are captured when the default logger is enabled, then delegated to the previous `threading.excepthook`.

Additional audit checks cover synchronous discovery timeouts, master-check exceptions and missing process groups in `test_synchronous_timeouts_return_unavailable_with_correlated_safe_events`, `test_master_check_exceptions_log_safe_inconclusive_fallback` and `test_term_missing_group_is_ignored_and_cleanup_still_reaps`. Logger cleanup and expected queue waits are checked by `test_startup_cleanup_failure_warns_once_and_leaves_logging_disabled`, `test_writer_empty_queue_wait_is_expected_and_does_not_warn` and `test_exception_reference_eviction_is_bounded_and_close_releases_references`.

The inventory groups handler boundaries rather than listing every rendering or navigation branch. Tests exercise a reduced queue limit and drain timeout, not the production constants. Attachment tests validate generated hooks, generation guards, diagnostic-only failures and callback parsing, not live tmux hook delivery during rapid respawns. There is no stable automated end-to-end reconnect/diagnostics proof; use the manual SSH scenarios above.

The append-only sink has a 4096-item queue. Enqueue never waits for capacity: excess records are dropped and later accepted records include the cumulative `dropped_records` count. `flush()` and shutdown each wait at most one second for draining or joining; a full queue can return immediately. A blocked writer can outlive shutdown as a daemon. Sink failures and capacity/time limits share one stderr warning per logger, so later failures may be silent; a failed warning is swallowed to avoid recursion. There is no rotation, total file-size bound or cross-process ordering guarantee. Crashes, dropped records and sink failures can lose events; flush is not an fsync guarantee. Initialization failure disables that logger until its configuration changes or the process restarts.

Error capture retains at most 64 exception-object references for ID reuse, cleared on close; an evicted object can receive a new ID. Each record limits chains to eight exceptions and the last 24 frames per exception, using basenames without messages, source text or locals. These bounds do not limit arbitrary event fields or bytes retained by an individual exception.

## Preparing a release

Use GitHub UI to create release PR:

1. Open **Actions → Prepare release**.
2. Select `main` as workflow branch and click **Run workflow**.
3. Leave **Version** blank for next patch version, or enter explicit `MAJOR.MINOR.PATCH` version.
4. Click **Run workflow**.

Workflow creates `release/v<version>`, commits `pyproject.toml` and `letee/__init__.py`, pushes branch, and opens PR to `main`.

Enable **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests** so workflow can open PRs with built-in `GITHUB_TOKEN`.

Merging release PR starts build, PyPI publication, and GitHub release automatically.
