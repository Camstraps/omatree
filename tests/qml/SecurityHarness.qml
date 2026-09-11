import QtQuick
import Quickshell
import Quickshell.Io
import "FrontendSafety.js" as FrontendSafety

ShellRoot {
  id: root
  property string phase: ""
  property int termCount: 0
  property int killCount: 0

  function require(condition, message) {
    if (!condition) {
      console.log("OMATREE_SECURITY_FAIL " + message)
      Qt.quit()
      return false
    }
    return true
  }

  function validDirectory() {
    return {
      path: "/root", parentPath: null, name: "root", bytes: 0,
      directFilesBytes: 0, childDirectoryCount: 0, warningCount: 0
    }
  }

  function testLimits() {
    if (!require(FrontendSafety.queueLimitError(65536, 0, 0, 65536, 4096, 16777216) === "", "exact line bytes")) return false
    if (!require(FrontendSafety.queueLimitError(65537, 0, 0, 65536, 4096, 16777216) !== "", "oversized line")) return false
    if (!require(FrontendSafety.queueLimitError(1, 4095, 0, 65536, 4096, 16777216) === "", "exact line count")) return false
    if (!require(FrontendSafety.queueLimitError(1, 4096, 0, 65536, 4096, 16777216) !== "", "line count overflow")) return false
    if (!require(FrontendSafety.queueLimitError(1, 0, 16777215, 65536, 4096, 16777216) === "", "exact queue bytes")) return false
    if (!require(FrontendSafety.queueLimitError(2, 0, 16777215, 65536, 4096, 16777216) !== "", "queue byte overflow")) return false
    if (!require(!FrontendSafety.outputLimitExceeded(1, 4194303, 4194304), "exact output bytes")) return false
    if (!require(FrontendSafety.outputLimitExceeded(2, 4194303, 4194304), "output byte overflow")) return false
    if (!require(FrontendSafety.directoryLimitError(validDirectory(), 499999, 500000, 4096) === "", "exact directory count")) return false
    if (!require(FrontendSafety.directoryLimitError(validDirectory(), 500000, 500000, 4096) !== "", "directory overflow")) return false
    var invalid = validDirectory()
    invalid.bytes = NaN
    return require(FrontendSafety.directoryLimitError(invalid, 0, 500000, 4096) !== "", "non-finite number")
  }

  function startResponsiveTermination() {
    phase = "responsive"
    process.command = ["/usr/bin/python3", "-c", "import time; time.sleep(10)"]
    process.running = true
  }

  function startIgnoredTermination() {
    phase = "ignored"
    process.command = ["/usr/bin/python3", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(10)"]
    process.running = true
  }

  Component.onCompleted: {
    if (testLimits()) startResponsiveTermination()
  }

  Process {
    id: process
    stdout: SplitParser {
      onRead: function(line) {
        if (root.phase === "ignored" && String(line).trim() === "ready") {
          root.termCount++
          process.signal(15)
          killTimer.restart()
        }
      }
    }
    onStarted: {
      if (root.phase === "responsive") {
        root.termCount++
        process.signal(15)
        killTimer.restart()
      }
    }
    onExited: {
      killTimer.stop()
      if (root.phase === "responsive") {
        if (!root.require(root.termCount === 1 && root.killCount === 0, "normal exit cancellation")) return
        root.startIgnoredTermination()
      } else if (root.phase === "ignored") {
        if (!root.require(root.termCount === 2 && root.killCount === 1, "kill escalation")) return
        console.log("OMATREE_SECURITY_PASS")
        Qt.quit()
      }
    }
  }

  Timer {
    id: killTimer
    interval: 200
    onTriggered: {
      if (process.running) {
        root.killCount++
        process.signal(9)
      }
    }
  }
}
