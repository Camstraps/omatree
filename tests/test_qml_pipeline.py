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
    def run_quickshell_fixture(self, fixture, marker, timeout=60):
        quickshell = shutil.which("quickshell")
        if quickshell is None:
            self.skipTest("quickshell is not installed")

        with tempfile.TemporaryDirectory(prefix="omatree-qml-runtime-") as runtime:
            config_dir = Path(runtime) / "config"
            config_dir.mkdir()
            shutil.copy2(REPOSITORY / "tests/qml" / fixture, config_dir)
            shutil.copy2(REPOSITORY / "TreeModel.js", config_dir)
            shutil.copy2(REPOSITORY / "FrontendSafety.js", config_dir)
            env = os.environ.copy()
            env.pop("DISPLAY", None)
            env.update({
                "QT_QPA_PLATFORM": "offscreen",
                "QT_QPA_PLATFORMTHEME": "",
                "QT_STYLE_OVERRIDE": "Fusion",
                "XDG_RUNTIME_DIR": runtime,
                "OMATREE_TEST_REPOSITORY": str(REPOSITORY),
            })
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

    def test_bar_widget_manifest_and_safety_contract(self):
        manifest = json.loads((REPOSITORY / "manifest.json").read_text(encoding="utf-8"))
        widget = (REPOSITORY / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("panel", manifest["kinds"])
        self.assertIn("bar-widget", manifest["kinds"])
        self.assertEqual(manifest["entryPoints"]["barWidget"], "BarWidget.qml")
        self.assertEqual(manifest["barWidget"]["defaultSection"], "right")
        self.assertIn('bar.shell.summon(moduleName, "{}")', widget)
        self.assertIn("displayMode = (displayMode + 1) % 4", widget)
        self.assertIn("totalBytes > 0", widget)
        self.assertIn('["/usr/bin/python3", helperPath, "discover"]', widget)
        self.assertNotIn('"scan"', widget)
        self.assertNotIn("requestScan", widget)

    def test_panel_uses_a_runnable_queue_timer(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn(
            "Timer { id: scanDrainTimer; interval: 1; repeat: true;",
            panel,
        )
        self.assertNotIn(
            "Timer { id: scanDrainTimer; interval: 0;",
            panel,
        )

    def test_completed_tree_navigation_never_requests_another_scan(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        toggle = panel.split("function toggleNode(path)", 1)[1].split(
            "function retryNode(path)", 1
        )[0]
        self.assertNotIn("requestScan", toggle)
        self.assertIn("if (!filesystem || !node || path !== treeRootPath) return", panel)
        self.assertIn("if (activeGeneration !== treeGeneration) return", panel)
        self.assertEqual(panel.count("Process {\n    id: scanProcess"), 1)

    def test_search_and_reveal_never_request_scans(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        search = panel.split("function beginSearch()", 1)[1].split(
            "function copySelectedPath()", 1
        )[0]
        self.assertNotIn("requestScan", search)
        self.assertNotIn("scanProcess", search)
        self.assertIn("TreeModel.revealPath(treeCache, path)", panel)
        self.assertIn("searchResultLimit: 500", panel)

    def test_actions_use_safe_argument_arrays(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn(
            '["/usr/bin/python3", helperPath, "discover"]', panel
        )
        self.assertIn(
            '"/usr/bin/python3", helperPath, "scan"', panel
        )
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
        self.assertEqual(panel.count("requestScan(mountpoint)"), 1)

    def test_refresh_has_one_filesystem_scan_entrypoint(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        selection = panel.split("function selectFilesystem(index)", 1)[1].split(
            "function rebuildTreeRows()", 1
        )[0]
        self.assertEqual(selection.count("requestScan(mountpoint)"), 1)

    def test_stale_generation_is_rejected_before_staging(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        handler = panel.split("function handleScanLine(rawLine)", 1)[1].split(
            "function enqueueScanLine(line)", 1
        )[0]
        generation_guard = handler.index("if (activeGeneration !== treeGeneration) return")
        directory_stage = handler.index("TreeModel.stageDirectory")
        self.assertLess(generation_guard, directory_stage)

    def test_bounded_queue_and_failure_contract(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn("maxQueuedScanLines: 4096", panel)
        self.assertIn("maxQueuedScanBytes: 16 * 1024 * 1024", panel)
        self.assertIn("maxEventBytes: 64 * 1024", panel)
        self.assertIn("maxStagedDirectories: 500000", panel)
        self.assertIn("maxPathBytes: 4096", panel)
        enqueue = panel.split("function enqueueScanLine(line)", 1)[1].split(
            "function drainScanLines()", 1
        )[0]
        self.assertLess(enqueue.index("queueLimitError"), enqueue.index("scanLineQueue.push"))
        failure = panel.split("function failActiveScan(message)", 1)[1].split(
            "function resetDiscoveryOutput()", 1
        )[0]
        self.assertIn("activeComplete = null", failure)
        self.assertIn("scanAcceptingRecords = false", failure)
        self.assertIn("clearScanQueue()", failure)
        deadline = panel.split("id: scanDeadlineTimer", 1)[1].split("Timer {", 1)[0]
        self.assertIn("failActiveScan", deadline)
        settle = panel.split("function settleScan()", 1)[1].split(
            "function moveTreeSelection", 1
        )[0]
        self.assertIn("clearScanQueue()", settle)
        launch = panel.split("function launchScan(request)", 1)[1].split(
            "function cancelActiveScan()", 1
        )[0]
        cancel = panel.split("function cancelActiveScan()", 1)[1].split(
            "function handleScanLine", 1
        )[0]
        close = panel.split("function close()", 1)[1].split(
            "function dismiss()", 1
        )[0]
        self.assertIn("clearScanQueue()", launch)
        self.assertIn("clearScanQueue()", cancel)
        self.assertIn("cancelActiveScan()", close)

    def test_process_deadline_and_signal_contracts(self):
        panel = (REPOSITORY / "Panel.qml").read_text(encoding="utf-8")
        widget = (REPOSITORY / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("scanProcess.signal(15)", panel)
        self.assertIn("scanProcess.signal(9)", panel)
        self.assertIn("interval: 60 * 60 * 1000", panel)
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

    def test_security_boundaries_and_escalation_offscreen(self):
        self.run_quickshell_fixture(
            "SecurityHarness.qml", "OMATREE_SECURITY_PASS", timeout=10
        )

    def test_split_parser_output_reaches_visible_tree_model(self):
        self.run_quickshell_fixture(
            "ScanPipeline.qml", "OMATREE_PIPELINE_PASS"
        )


if __name__ == "__main__":
    unittest.main()
