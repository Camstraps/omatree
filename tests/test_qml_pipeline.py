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

    def test_split_parser_output_reaches_visible_tree_model(self):
        quickshell = shutil.which("quickshell")
        if quickshell is None:
            self.skipTest("quickshell is not installed")

        with tempfile.TemporaryDirectory(prefix="omatree-qml-runtime-") as runtime:
            config_dir = Path(runtime) / "config"
            config_dir.mkdir()
            shutil.copy2(REPOSITORY / "tests/qml/ScanPipeline.qml", config_dir)
            shutil.copy2(REPOSITORY / "TreeModel.js", config_dir)
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
                [quickshell, "-p", str(config_dir / "ScanPipeline.qml")],
                cwd=REPOSITORY,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output = []
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 60
            passed = False
            try:
                while time.monotonic() < deadline:
                    for key, _ in selector.select(timeout=0.25):
                        line = key.fileobj.readline()
                        if not line:
                            continue
                        output.append(line)
                        if "OMATREE_PIPELINE_PASS" in line:
                            passed = True
                            break
                    if passed or process.poll() is not None:
                        break
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

            self.assertTrue(passed, "".join(output))


if __name__ == "__main__":
    unittest.main()
