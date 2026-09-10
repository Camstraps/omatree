import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "TreeModel.js" as TreeModel

BarWidget {
  id: root
  moduleName: "io.github.camstraps.omatree"

  property int displayMode: 0
  property var filesystem: null
  property string discoveryError: ""

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
    discovery.command = ["python3", helperPath, "discover"]
    discovery.running = true
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
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyDiscovery(text) }
    onExited: function(code) {
      if (code !== 0) root.discoveryError = "Filesystem discovery failed"
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
