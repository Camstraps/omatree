import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "TreeModel.js" as TreeModel
import "FrontendSafety.js" as FrontendSafety

BarWidget {
  id: root
  moduleName: "io.github.camstraps.omatree"

  property int displayMode: 0
  property var filesystem: null
  property string discoveryError: ""
  property string discoveryStdout: ""
  property string discoveryStderr: ""
  property int discoveryStdoutBytes: 0
  property int discoveryStderrBytes: 0
  property bool discoveryFailed: false
  property bool discoveryTerminationRequested: false

  readonly property int maxDiscoveryStdoutBytes: 4 * 1024 * 1024
  readonly property int maxDiscoveryStderrBytes: 64 * 1024
  readonly property int maxDiscoveryLineBytes: 64 * 1024

  readonly property url helperUrl: Qt.resolvedUrl("helper/discover.py")
  readonly property string helperPath: decodeURIComponent(String(helperUrl).replace(/^file:\/\//, ""))
  readonly property double totalBytes: filesystem ? Number(filesystem.totalBytes || 0) : 0
  readonly property double usedBytes: filesystem ? Number(filesystem.usedBytes || 0) : 0
  readonly property double availableBytes: filesystem ? Number(filesystem.availableBytes || 0) : 0
  readonly property double usedPercent: totalBytes > 0
    ? TreeModel.clampPercent(usedBytes * 100 / totalBytes) : 0
  readonly property string displayName: filesystem ? String(filesystem.displayName || filesystem.mountpoint) : "Disk"
  readonly property string modeText: {
    if (!filesystem) return "—"
    if (displayMode === 1) return TreeModel.formatBytes(usedBytes)
    if (displayMode === 2) return TreeModel.formatBytes(availableBytes)
    if (displayMode === 3) return displayName + " " + Math.round(usedPercent) + "%"
    return Math.round(usedPercent) + "%"
  }
  readonly property string details: filesystem
    ? displayName + "\nUsed: " + TreeModel.formatBytes(usedBytes) + " / " + TreeModel.formatBytes(totalBytes)
      + "\nFree: " + TreeModel.formatBytes(availableBytes) + "\n" + usedPercent.toFixed(1) + "%"
    : (discoveryError || "Filesystem capacity unavailable")

  function utf8Bytes(value) {
    return FrontendSafety.utf8Bytes(value, maxDiscoveryLineBytes + 1)
  }

  function resetDiscoveryOutput() {
    discoveryStdout = ""
    discoveryStderr = ""
    discoveryStdoutBytes = 0
    discoveryStderrBytes = 0
    discoveryFailed = false
  }

  function requestDiscoveryTermination() {
    if (discoveryTerminationRequested || !discovery.running) return
    discoveryTerminationRequested = true
    discovery.signal(15)
    discoveryKillTimer.restart()
  }

  function failDiscovery(message) {
    discoveryFailed = true
    discoveryError = String(message || "Filesystem discovery failed")
    discoveryStdout = ""
    requestDiscoveryTermination()
  }

  function appendDiscoveryOutput(raw, isError) {
    if (discoveryFailed) return
    var value = String(raw || "")
    var bytes = utf8Bytes(value) + 1
    var current = isError ? discoveryStderrBytes : discoveryStdoutBytes
    var limit = isError ? maxDiscoveryStderrBytes : maxDiscoveryStdoutBytes
    if (FrontendSafety.outputLimitExceeded(bytes, current, limit)) {
      failDiscovery("Filesystem discovery exceeded its output limit")
      return
    }
    if (isError) {
      discoveryStderr += value + "\n"
      discoveryStderrBytes += bytes
    } else {
      discoveryStdout += value + "\n"
      discoveryStdoutBytes += bytes
    }
  }

  function chooseFilesystem(filesystems, homePath) {
    var chosen = null
    var bestLength = -1
    for (var i = 0; i < filesystems.length; i++) {
      var candidate = filesystems[i]
      var mountpoint = String(candidate.mountpoint || "")
      var containsHome = mountpoint === "/"
        || homePath === mountpoint || homePath.indexOf(mountpoint + "/") === 0
      if (containsHome && mountpoint.length > bestLength) {
        chosen = candidate
        bestLength = mountpoint.length
      }
    }
    return chosen || (filesystems.length > 0 ? filesystems[0] : null)
  }

  function applyDiscovery(raw) {
    try {
      var payload = JSON.parse(String(raw || ""))
      if (!payload || payload.schemaVersion !== 1 || !Array.isArray(payload.filesystems))
        throw new Error("Unsupported discovery response")
      filesystem = chooseFilesystem(payload.filesystems, Quickshell.env("HOME"))
      discoveryError = filesystem ? "" : "No mounted filesystems"
    } catch (error) {
      discoveryError = "Filesystem discovery failed"
    }
  }

  function refresh() {
    if (discovery.running) return
    resetDiscoveryOutput()
    discoveryTerminationRequested = false
    discoveryError = ""
    discovery.command = ["/usr/bin/python3", helperPath, "discover"]
    discovery.running = true
    discoveryDeadlineTimer.restart()
  }

  function cycleMode() { displayMode = (displayMode + 1) % 4 }

  function summonPanel() {
    if (bar && bar.shell) bar.shell.summon(moduleName, "{}")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refresh()

  Process {
    id: discovery
    stdout: SplitParser { onRead: function(line) { root.appendDiscoveryOutput(line, false) } }
    stderr: SplitParser { onRead: function(line) { root.appendDiscoveryOutput(line, true) } }
    onExited: function(code) {
      discoveryDeadlineTimer.stop()
      discoveryKillTimer.stop()
      root.discoveryTerminationRequested = false
      if (!root.discoveryFailed && code === 0) root.applyDiscovery(root.discoveryStdout)
      else if (!root.discoveryFailed)
        root.discoveryError = root.discoveryStderr.trim() || "Filesystem discovery failed"
      root.discoveryStdout = ""
      root.discoveryStderr = ""
      root.discoveryStdoutBytes = 0
      root.discoveryStderrBytes = 0
    }
  }

  Timer {
    id: discoveryDeadlineTimer
    interval: 15000
    onTriggered: root.failDiscovery("Filesystem discovery timed out after 15 seconds")
  }
  Timer {
    id: discoveryKillTimer
    interval: 2000
    onTriggered: {
      if (discovery.running && root.discoveryTerminationRequested) discovery.signal(9)
    }
  }
  Timer { interval: 60000; repeat: true; running: true; onTriggered: root.refresh() }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰋊 " + root.modeText
    tooltipText: root.details
    onPressed: function(button) {
      if (button === Qt.RightButton) root.cycleMode()
      else if (button === Qt.LeftButton) root.summonPanel()
    }
  }
}
