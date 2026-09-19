import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]


class QmlScanPipelineTests(unittest.TestCase):
    def run_quickshell_fixture(self, fixture, marker, timeout=60, broker_tree=False):
        quickshell = shutil.which("quickshell")
        if quickshell is None:
            self.skipTest("quickshell is not installed")

        with tempfile.TemporaryDirectory(prefix="omatree-qml-runtime-") as runtime:
            config_dir = Path(runtime) / "config"
            config_dir.mkdir()
            shutil.copy2(REPOSITORY / "tests/qml" / fixture, config_dir)
            shutil.copy2(REPOSITORY / "TreeModel.js", config_dir)
            shutil.copy2(REPOSITORY / "FrontendSafety.js", config_dir)
            shutil.copy2(REPOSITORY / "BrowserState.js", config_dir)
            shutil.copy2(REPOSITORY / "SearchState.js", config_dir)
            env = os.environ.copy()
            if broker_tree:
                scan_root = Path(runtime) / "broker-tree"
                scan_root.mkdir()
                for index in range(2000):
                    (scan_root / f"directory-{index:04d}").mkdir()
                for index in range(10):
                    (scan_root / "directory-0000" / f"nested-{index:02d}").mkdir()
            env.pop("DISPLAY", None)
            env.update({
                "QT_QPA_PLATFORM": "offscreen",
                "QT_QPA_PLATFORMTHEME": "",
                "QT_STYLE_OVERRIDE": "Fusion",
                "XDG_RUNTIME_DIR": runtime,
                "XDG_CACHE_HOME": str(Path(runtime) / "cache"),
                "OMATREE_TEST_REPOSITORY": str(REPOSITORY),
            })
            if broker_tree:
                env["OMATREE_TEST_SCAN_PATH"] = str(scan_root)
            process = subprocess.Popen(
                [quickshell, "-p", str(config_dir / fixture)],
                cwd=REPOSITORY, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            output = []
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            passed = False
            try:
                while time.monotonic() < deadline:
                    for key, _ in selector.select(timeout=0.25):
                        line = key.fileobj.readline()
                        if not line:
                            continue
                        output.append(line)
                        if marker in line:
                            passed = True
                            break
                    if passed or process.poll() is not None:
                        break
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                selector.close()
                if process.stdout is not None:
                    process.stdout.close()
            self.assertTrue(passed, "".join(output))
            return "".join(output)

    def test_bar_widget_manifest_and_safety_contract(self):
        manifest = json.loads((REPOSITORY / "manifest.json").read_text(encoding="utf-8"))
        widget = (REPOSITORY / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("panel", manifest["kinds"])
        self.assertIn("bar-widget", manifest["kinds"])
        self.assertEqual(manifest["entryPoints"]["barWidget"], "BarWidget.qml")
        self.assertEqual(manifest["barWidget"]["defaultSection"], "right")
        self.assertIn('bar.shell.summon(moduleName, JSON.stringify(payload))', widget)
        self.assertIn('mountpoint: String(filesystem.mountpoint)', widget)
        self.assertIn("displayMode = (displayMode + 1) % 4", widget)
        self.assertIn("totalBytes > 0", widget)
        self.assertIn('["/usr/bin/python3", helperPath, "discover"]', widget)
        self.assertNotIn('"scan"', widget)
        self.assertNotIn("requestScan", widget)

    def test_panel_uses_a_runnable_broker_queue_timer(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn(
            "Timer { id: brokerDrainTimer; interval: 1; repeat: true;",
            panel,
        )
        self.assertNotIn("Timer { id: brokerDrainTimer; interval: 0;", panel)

    def test_completed_tree_navigation_never_requests_another_scan(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        toggle = panel.split("function toggleNode(path)", 1)[1].split(
            "function retryNode(path)", 1
        )[0]
        self.assertNotIn("requestScan", toggle)
        self.assertNotIn('sendBrokerRequest("scanStart"', toggle)
        self.assertIn('sendBrokerRequest("children"', toggle)
        self.assertNotIn("scanProcess", panel)
        self.assertEqual(panel.count("Process {\n    id: brokerProcess"), 1)

    def test_search_and_reveal_never_request_scans(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertNotIn("Object.keys(treeCache)", panel)
        self.assertNotIn("TreeModel.searchMatches", panel)
        self.assertIn('placeholderText: "Search directories"', panel)
        self.assertIn('sendBrokerRequest("search"', panel)
        search = panel.split("function scheduleSearch()", 1)[1].split(
            "function copySelectedPath()", 1
        )[0]
        self.assertNotIn("requestScan", search)

    def test_actions_use_safe_argument_arrays(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn(
            '["/usr/bin/python3", helperPath, "discover"]', panel
        )
        self.assertIn('brokerProcess.command = [brokerPath]', panel)
        self.assertNotIn('helperPath, "scan"', panel)
        self.assertIn('copyProcess.command = ["/usr/bin/wl-copy"]', panel)
        self.assertIn(
            'Quickshell.execDetached(["/usr/bin/xdg-open", selectedTreePath])',
            panel,
        )
        self.assertNotIn('"bash", "-c"', panel)

    def test_refresh_and_expansion_only_have_one_scan_entrypoint(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        toggle = panel.split("function toggleNode(path)", 1)[1].split(
            "function retryNode(path)", 1
        )[0]
        choose = panel.split("function chooseSearchResult(path)", 1)[1].split(
            "function copySelectedPath()", 1
        )[0]
        self.assertNotIn("requestScan", toggle + choose)
        self.assertEqual(panel.count('sendBrokerRequest("scanStart"'), 1)

    def test_refresh_has_one_filesystem_scan_entrypoint(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        selection = panel.split("function selectFilesystem(index)", 1)[1].split(
            "function requestSnapshotOpen(path)", 1
        )[0]
        self.assertIn("requestSnapshotOpen(mountpoint)", selection)
        self.assertNotIn("requestScan", selection)
        self.assertIn('text: root.scanning ? "Rescanning…" : "Rescan"', panel)

    def test_panel_uses_persistent_snapshot_open_and_typed_bounded_rows(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        browser = (REPOSITORY / "BrowserState.js").read_text(encoding="utf-8")
        self.assertIn('sendBrokerRequest("snapshotOpen"', panel)
        self.assertIn('nodeKind: row.kind || "directory"', panel)
        self.assertIn('selected.kind === "file"', panel)
        self.assertIn('kind: source.kind || "directory"', browser)
        self.assertEqual(panel.count('sendBrokerRequest("scanStart"'), 1)

    def test_panel_open_and_bar_summon_do_not_directly_request_scan(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        widget = (REPOSITORY / "BarWidget.qml").read_text(encoding="utf-8")
        open_body = panel.split("function open(payloadJson)", 1)[1].split(
            "function close()", 1
        )[0]
        self.assertIn("summonMountpoint(payloadJson", open_body)
        self.assertIn("activeBackendAvailable", open_body)
        self.assertIn("requestSnapshotOpen", open_body)
        self.assertNotIn("requestScan", open_body)
        self.assertIn("JSON.stringify(payload)", widget)

        handler = panel.split("function handleBrokerLine(rawLine)", 1)[1].split(
            "function requestActivationCommit()", 1
        )[0]
        missing = handler.split('message.type === "snapshotMissing"', 1)[1].split(
            'message.type !== "snapshotAvailable"', 1
        )[0]
        self.assertIn("requestScan(treeRootPath)", missing)
        self.assertIn('onClicked: root.requestScan(root.treeRootPath)', panel)

    def test_reopen_during_broker_shutdown_restarts_without_direct_scan(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        exited = panel.split("id: brokerProcess", 1)[1].split(
            "Timer { id: brokerDrainTimer", 1
        )[0]
        self.assertIn("if (root.opened)", exited)
        self.assertIn("root.pendingSnapshotOpen", exited)
        self.assertIn("root.startBroker()", exited)
        self.assertNotIn("requestScan", exited)

    def test_stale_generation_is_rejected_before_activation_staging(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        handler = panel.split("function handleBrokerLine(rawLine)", 1)[1].split(
            "function requestActivationCommit()", 1
        )[0]
        self.assertIn("message.generationId !== expected.generationId", handler)
        self.assertIn("pendingActivation.token !== expected.activationToken", handler)

    def test_bounded_broker_queue_and_failure_contract(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn("maxBrokerQueueLines: 256", panel)
        self.assertIn("maxBrokerQueueBytes: 4 * 1024 * 1024", panel)
        self.assertIn("maxBrokerLineBytes: 1024 * 1024", panel)
        self.assertIn("maxBrokerRows: 64", panel)
        self.assertIn("maxPathBytes: 4096", panel)
        enqueue = panel.split("function enqueueBrokerLine(line)", 1)[1].split(
            "function drainBrokerLines()", 1
        )[0]
        self.assertLess(enqueue.index("brokerQueueLimitError"), enqueue.index("brokerLineQueue.push"))
        failure = panel.split("function failBroker(message)", 1)[1].split(
            "function resetDiscoveryOutput()", 1
        )[0]
        self.assertIn("clearBrokerQueue()", failure)
        self.assertIn("clearBrokerRequests()", failure)
        self.assertIn("activeBackendAvailable = false", failure)

    def test_process_deadline_and_signal_contracts(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        widget = (REPOSITORY / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("brokerProcess.signal(15)", panel)
        self.assertIn("brokerProcess.signal(9)", panel)
        self.assertIn("60 * 60 * 1000 + 10000", panel)
        self.assertIn("interval: 15000", panel)
        self.assertIn("interval: 2000", panel)
        self.assertIn("discoveryProcess.signal(15)", panel)
        self.assertIn("discoveryProcess.signal(9)", panel)
        self.assertIn("discovery.signal(15)", widget)
        self.assertIn("discovery.signal(9)", widget)
        self.assertIn("interval: 15000", widget)
        self.assertIn("interval: 2000", widget)
        self.assertNotIn("StdioCollector", panel)
        self.assertNotIn("StdioCollector", widget)
        bar_failure = widget.split("function failDiscovery(message)", 1)[1].split(
            "function appendDiscoveryOutput", 1
        )[0]
        self.assertNotIn("filesystem =", bar_failure)

    def test_panel_has_no_full_tree_scan_or_staging_path(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        for forbidden in (
            "stageDirectory", "finalizeTree", "activeDirectoryCache",
            "activePendingChildren", 'message.type === "directory"',
            "Object.keys(treeCache)", "treeCache",
        ):
            self.assertNotIn(forbidden, panel)
        self.assertIn('sendBrokerRequest("metadata"', panel)
        self.assertIn('sendBrokerRequest("children"', panel)
        self.assertIn("message.result.rows.length > maxBrokerRows", panel)

    def test_atomic_activation_waits_for_bounded_queries_and_commit_ack(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        activated = panel.split('if (message.type === "snapshotActivated")', 1)[1].split(
            'if (message.type === "queryResult"', 1
        )[0]
        self.assertIn('sendBrokerRequest("metadata"', activated)
        self.assertIn('sendBrokerRequest("children"', activated)
        self.assertNotIn("activeGenerationId =", activated)
        commit = panel.split("function commitInitialView()", 1)[1].split(
            "function launchPendingScan()", 1
        )[0]
        self.assertIn("browserState = nextState", commit)
        self.assertIn("rebuildVisibleWindow", commit)
        self.assertIn("activeGenerationId = activation.generationId", commit)

    def test_broker_identity_crash_and_memory_high_water_contract(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        handler = panel.split("function handleBrokerLine(rawLine)", 1)[1].split(
            "function requestActivationCommit()", 1
        )[0]
        self.assertIn("if (!expected) return", handler)
        self.assertIn("message.operation !== expected.operation", handler)
        self.assertIn("message.result.rows.length > maxBrokerRows", panel)
        self.assertIn("failBroker(envelopeError)", handler)
        exited = panel.split("onExited: function(exitCode)", 2)[2].split("}", 1)[0]
        self.assertIn("activeBackendAvailable = false", exited)
        self.assertNotIn("treeRows.clear()", exited)
        self.assertIn("maxDiagnosticBytes: 64 * 1024", panel)
        for metric in (
            "brokerQueueLinesHighWater", "brokerQueueBytesHighWater",
            "brokerRequestsHighWater", "activationRowsHighWater",
            "activeRowsHighWater", "generationIdsHighWater",
            "warningRowsHighWater",
            "cachedDirectoryRowsHighWater", "cachedPagesHighWater",
            "visibleRowsHighWater", "expansionStatesHighWater",
            "breadcrumbRowsHighWater", "metadataRowsHighWater",
        ):
            self.assertIn(metric, panel)

    def test_security_boundaries_and_escalation_offscreen(self):
        self.run_quickshell_fixture(
            "SecurityHarness.qml", "OMATREE_SECURITY_PASS", timeout=10
        )

    def test_split_parser_output_reaches_visible_tree_model(self):
        self.run_quickshell_fixture(
            "ScanPipeline.qml", "OMATREE_PIPELINE_PASS"
        )

    def test_actual_broker_bounded_activation_pipeline_offscreen(self):
        self.run_quickshell_fixture(
            "BrokerPipeline.qml", "OMATREE_BROKER_PIPELINE_PASS",
            timeout=30, broker_tree=True,
        )

    def test_actual_qml_bounded_browser_state_offscreen(self):
        output = self.run_quickshell_fixture(
            "BrowserStateHarness.qml", "OMATREE_BROWSER_STATE_PASS", timeout=30
        )
        self.assertRegex(output, r"rows=\d+ pages=24 expansions=256 visible=512 breadcrumbs=128 metadata=1 flat500kMs=\d+")

    def test_stage5_hard_limits_and_broker_only_navigation(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        browser = (REPOSITORY / "BrowserState.js").read_text(encoding="utf-8")
        for value in (
            "childPageRows: 64", "maxCachedDirectoryRows: 2048",
            "maxCachedPages: 24", "maxVisibleRows: 512",
            "maxExpansionStates: 256", "maxBreadcrumbRows: 128",
            "maxMetadataCacheRows: 256",
        ):
            self.assertIn(value, panel)
        self.assertIn('sendBrokerRequest("children"', panel)
        self.assertIn('sendBrokerRequest("ancestors"', panel)
        self.assertNotIn('helperPath, "scan"', panel)
        self.assertIn("while (state.rowCount + needed > state.limits.maxRows)", browser)
        self.assertIn("while (state.pageCount >= state.limits.maxPages)", browser)
        self.assertIn("while (state.expansionCount >= state.limits.maxExpansions)", browser)
        self.assertIn("output.length < state.limits.maxVisible", browser)

    def test_stage6_search_is_broker_backed_and_bounded(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn('sendBrokerRequest("search"', panel)
        self.assertIn("maxSearchResultRows: 128", panel)
        self.assertIn("maxSearchPages: 2", panel)
        self.assertIn("maxSearchQueryBytes: 1024", panel)
        self.assertNotIn("Object.keys(treeCache)", panel)

    def test_stage5_navigation_has_no_scanner_or_filesystem_fallback(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        start = panel.index("function selectTreePath")
        end = panel.index("function retryNode")
        navigation = panel[start:end]
        self.assertNotIn("requestScan", navigation)
        self.assertNotIn("helperPath", navigation)
        self.assertNotIn("scanProcess", navigation)
        self.assertNotIn("scandir", navigation)
        self.assertIn('sendBrokerRequest("children"', navigation)
        self.assertIn('sendBrokerRequest("ancestors"', navigation)
        self.assertIn("BrowserState.previousPage", navigation)

    def test_generation_swap_releases_old_frontend_state_reference(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        commit = panel.split("function commitInitialView()", 1)[1].split(
            "function launchPendingScan()", 1
        )[0]
        self.assertIn("var nextState = BrowserState.create", commit)
        self.assertEqual(commit.count("browserState = nextState"), 1)
        self.assertNotIn("oldState", commit)
        self.assertNotIn("previousState", commit)

    def test_actual_qml_bounded_search_state_offscreen(self):
        output = self.run_quickshell_fixture(
            "SearchStateHarness.qml", "OMATREE_SEARCH_STATE_PASS", timeout=60
        )
        self.assertRegex(output, r"rows=0 pages=0 sessions=1 match500kMs=\d+")

    def test_stage6_direct_reveal_and_static_architecture(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        search = (REPOSITORY / "SearchState.js").read_text(encoding="utf-8")
        self.assertIn('sendBrokerRequest("childrenAt"', panel)
        self.assertIn('sendBrokerRequest("ancestors"', panel)
        self.assertNotIn('helperPath, "scan"', panel)
        self.assertNotIn("Object.keys(treeCache)", panel)
        self.assertNotIn("fetchall", panel)
        self.assertIn("state.pages.length >= state.limits.maxPages", search)
        self.assertIn("rows.length > state.limits.pageRows", search)
        handler = panel.split("function handleBrokerLine(rawLine)", 1)[1].split(
            "function requestActivationCommit()", 1
        )[0]
        self.assertIn("message.requestId !== searchRequestId", handler)
        self.assertIn("message.generationId !== activeGenerationId", handler)
        self.assertIn("expected.activationToken !== searchState.sessionId", handler)
        schedule = panel.split("function scheduleSearch()", 1)[1].split(
            "function chooseSearchResult", 1
        )[0]
        self.assertLess(schedule.index("clearSearchState(false)"),
                        schedule.index("searchDebounce.restart()"))


if __name__ == "__main__":
    unittest.main()
