import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "TreeModel.js" as TreeModel

Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false
  property bool discoveryLoading: false
  property string discoveryError: ""
  property string discoveryStderr: ""
  property string preferredMountpoint: ""
  property int selectedFilesystemIndex: -1

  property var treeCache: ({})
  property string treeRootPath: ""
  property string selectedTreePath: ""
  property int selectedTreeIndex: -1
  property int treeGeneration: 0
  property int requestSerial: 0

  property string activeRequestId: ""
  property string activePath: ""
  property int activeGeneration: -1
  property var activeChildren: []
  property int activeWarningCount: 0
  property string activeFirstWarning: ""
  property string activeProtocolError: ""
  property string activeStderr: ""
  property var activeComplete: null
  property bool activeCancelled: false
  property bool activeExpectedStop: false
  property int activeExitCode: 0
  property bool scanProcessExited: false
  property var scanLineQueue: []
  property int scanLineQueueIndex: 0
  property var pendingScan: null
  property int progressEntries: 0
  property double progressBytes: 0

  readonly property string pluginId: (manifest && manifest.id)
    ? String(manifest.id) : "io.github.camstraps.omatree"
  readonly property url helperUrl: Qt.resolvedUrl("helper/discover.py")
  readonly property string helperPath: decodeURIComponent(String(helperUrl).replace(/^file:\/\//, ""))
  readonly property var selectedFilesystem:
    selectedFilesystemIndex >= 0 && selectedFilesystemIndex < filesystemModel.count
      ? filesystemModel.get(selectedFilesystemIndex) : null
  readonly property bool scanning: activeRequestId !== "" || scanProcess.running

  function open(payloadJson) {
    opened = true
    refresh()
    Qt.callLater(function() { if (opened) keyCatcher.forceActiveFocus() })
  }

  function close() {
    opened = false
    pendingScan = null
    cancelActiveScan()
  }

  function dismiss() {
    if (shell && typeof shell.hide === "function") shell.hide(pluginId)
    else close()
  }

  function refresh() {
    preferredMountpoint = selectedFilesystem ? selectedFilesystem.mountpoint : preferredMountpoint
    treeGeneration++
    pendingScan = null
    cancelActiveScan()
    clearTree()
    if (discoveryProcess.running) discoveryProcess.running = false
    discoveryLoading = true
    discoveryError = ""
    discoveryStderr = ""
    discoveryProcess.command = ["python3", helperPath, "discover"]
    discoveryProcess.running = true
  }

  function clearTree() {
    treeCache = ({})
    treeRootPath = ""
    selectedTreePath = ""
    selectedTreeIndex = -1
    progressEntries = 0
    progressBytes = 0
    treeRows.clear()
  }

  function applyDiscovery(raw) {
    var payload
    try { payload = JSON.parse(String(raw || "")) }
    catch (error) {
      discoveryError = "OmaTree received invalid filesystem discovery data."
      return
    }
    if (!payload || payload.schemaVersion !== 1 || !Array.isArray(payload.filesystems)) {
      discoveryError = String((payload && payload.error) || "Unsupported discovery response.")
      return
    }
    filesystemModel.clear()
    for (var i = 0; i < payload.filesystems.length; i++) filesystemModel.append(payload.filesystems[i])
    var selected = filesystemModel.count > 0 ? 0 : -1
    for (var j = 0; j < filesystemModel.count; j++) {
      if (filesystemModel.get(j).mountpoint === preferredMountpoint) { selected = j; break }
    }
    selectFilesystem(selected)
  }

  function selectFilesystem(index) {
    if (index < 0 || index >= filesystemModel.count) {
      selectedFilesystemIndex = -1
      clearTree()
      return
    }
    var mountpoint = filesystemModel.get(index).mountpoint
    if (selectedFilesystemIndex === index && treeRootPath === mountpoint) return
    treeGeneration++
    pendingScan = null
    cancelActiveScan()
    clearTree()
    selectedFilesystemIndex = index
    preferredMountpoint = mountpoint
    var node = TreeModel.createNode(filesystemModel.get(index).displayName, mountpoint, 0, 0)
    node.expanded = true
    treeCache[mountpoint] = node
    treeRootPath = mountpoint
    selectedTreePath = mountpoint
    rebuildTreeRows()
    requestScan(mountpoint)
  }

  function rebuildTreeRows() {
    var visible = TreeModel.visibleNodes(treeCache, treeRootPath)
    treeRows.clear()
    selectedTreeIndex = -1
    for (var i = 0; i < visible.length; i++) {
      var node = visible[i]
      treeRows.append({
        nodeName: node.name, nodePath: node.path, nodeBytes: node.bytes,
        formattedSize: node.formattedSize, depth: node.depth,
        expanded: node.expanded, loading: node.loading, loaded: node.loaded,
        warningCount: node.warningCount, warningText: node.warningText,
        errorText: node.error
      })
      if (node.path === selectedTreePath) selectedTreeIndex = i
    }
    if (selectedTreeIndex < 0 && treeRows.count > 0) {
      selectedTreeIndex = 0
      selectedTreePath = treeRows.get(0).nodePath
    }
  }

  function toggleNode(path) {
    var node = treeCache[path]
    if (!node) return
    selectedTreePath = path
    if (node.expanded) {
      node.expanded = false
      rebuildTreeRows()
    } else {
      node.expanded = true
      rebuildTreeRows()
      if (!node.loaded && !node.loading) requestScan(path)
    }
  }

  function retryNode(path) {
    var node = treeCache[path]
    if (!node) return
    node.error = ""
    node.warningText = ""
    requestScan(path)
  }

  function requestScan(path) {
    var filesystem = selectedFilesystem
    var node = treeCache[path]
    if (!filesystem || !node) return
    var request = { path: path, mountpoint: filesystem.mountpoint, generation: treeGeneration }
    if (scanProcess.running || activeRequestId !== "") {
      pendingScan = request
      cancelActiveScan()
    } else launchScan(request)
  }

  function launchScan(request) {
    if (!request || request.generation !== treeGeneration) return
    var node = treeCache[request.path]
    if (!node) return
    requestSerial++
    activeRequestId = String(treeGeneration) + "-" + String(Date.now()) + "-" + String(requestSerial)
    activePath = request.path
    activeGeneration = request.generation
    activeChildren = []
    activeWarningCount = 0
    activeFirstWarning = ""
    activeProtocolError = ""
    activeStderr = ""
    activeComplete = null
    activeCancelled = false
    activeExpectedStop = false
    activeExitCode = 0
    scanProcessExited = false
    scanLineQueue = []
    scanLineQueueIndex = 0
    progressEntries = 0
    progressBytes = 0
    node.loading = true
    node.error = ""
    node.requestId = activeRequestId
    rebuildTreeRows()
    scanProcess.command = [
      "python3", helperPath, "scan",
      "--mountpoint", request.mountpoint,
      "--path", request.path,
      "--request-id", activeRequestId
    ]
    scanProcess.running = true
  }

  function cancelActiveScan() {
    if (activeRequestId === "" && !scanProcess.running) return
    activeExpectedStop = true
    scanLineQueue = []
    scanLineQueueIndex = 0
    var node = treeCache[activePath]
    if (node && node.requestId === activeRequestId) {
      node.loading = false
      node.requestId = ""
      rebuildTreeRows()
    }
    if (scanProcess.running) scanProcess.running = false
    else settleScan()
  }

  function handleScanLine(rawLine) {
    var line = String(rawLine || "").trim()
    if (line === "") return
    var message
    try { message = JSON.parse(line) }
    catch (error) {
      if (activeRequestId !== "") activeProtocolError = "Scanner returned malformed data; this result was not cached."
      return
    }
    if (!message || String(message.requestId || "") !== activeRequestId) return
    if (activeGeneration !== treeGeneration) return
    if (message.type === "progress") {
      progressEntries = Number(message.entries || 0)
      progressBytes = Number(message.bytes || 0)
    } else if (message.type === "warning") {
      activeWarningCount++
      if (activeFirstWarning === "") activeFirstWarning = String(message.error || "Some paths could not be read.")
    } else if (message.type === "child") {
      activeChildren.push({
        name: String(message.name || message.path || "Directory"),
        path: String(message.path || ""),
        bytes: Number(message.bytes || 0)
      })
    } else if (message.type === "complete") activeComplete = message
    else if (message.type === "cancelled") activeCancelled = true
    else if (message.type === "error") activeProtocolError = String(message.error || "Directory scan failed.")
  }

  function enqueueScanLine(line) {
    scanLineQueue.push(String(line || ""))
    if (!scanDrainTimer.running) scanDrainTimer.start()
  }

  function drainScanLines() {
    var end = Math.min(scanLineQueue.length, scanLineQueueIndex + 100)
    while (scanLineQueueIndex < end) handleScanLine(scanLineQueue[scanLineQueueIndex++])
    if (scanLineQueueIndex >= scanLineQueue.length) {
      scanDrainTimer.stop()
      scanLineQueue = []
      scanLineQueueIndex = 0
      if (scanProcessExited) scanSettleTimer.restart()
    }
  }

  function settleScan() {
    if (activeRequestId === "") return
    var requestId = activeRequestId
    var path = activePath
    var generation = activeGeneration
    var node = treeCache[path]
    var validTarget = generation === treeGeneration && node && node.requestId === requestId
    if (validTarget) {
      node.loading = false
      node.requestId = ""
      if (!activeExpectedStop && !activeCancelled && activeComplete
          && activeProtocolError === "" && activeExitCode === 0) {
        var children = []
        for (var i = 0; i < activeChildren.length; i++) {
          var row = activeChildren[i]
          if (!row.path) continue
          var child = TreeModel.createNode(row.name, row.path, row.bytes, node.depth + 1)
          treeCache[row.path] = child
          children.push(row.path)
        }
        node.children = children
        node.bytes = Number(activeComplete.bytes || 0)
        node.formattedSize = TreeModel.formatBytes(node.bytes)
        node.loaded = true
        node.warningCount = Number(activeComplete.warningCount || activeWarningCount)
        node.warningText = node.warningCount > 0
          ? String(node.warningCount) + " path" + (node.warningCount === 1 ? "" : "s") + " could not be read"
          : ""
        node.error = ""
      } else if (!activeExpectedStop && !activeCancelled) {
        node.error = activeProtocolError || activeStderr || "Directory scan failed."
      }
      rebuildTreeRows()
    }
    activeRequestId = ""
    activePath = ""
    activeGeneration = -1
    activeChildren = []
    activeComplete = null
    scanLineQueue = []
    scanLineQueueIndex = 0
    progressEntries = 0
    progressBytes = 0
    var next = pendingScan
    pendingScan = null
    if (next && next.generation === treeGeneration) Qt.callLater(function() { launchScan(next) })
  }

  function moveTreeSelection(delta) {
    if (treeRows.count === 0) return
    var next = Math.max(0, Math.min(treeRows.count - 1, selectedTreeIndex + delta))
    selectedTreeIndex = next
    selectedTreePath = treeRows.get(next).nodePath
    treeView.positionViewAtIndex(next, ListView.Contain)
  }

  function expandSelected() {
    var node = treeCache[selectedTreePath]
    if (node && !node.expanded) toggleNode(node.path)
  }

  function collapseSelected() {
    var node = treeCache[selectedTreePath]
    if (node && node.expanded) toggleNode(node.path)
  }

  ListModel { id: filesystemModel }
  ListModel { id: treeRows }

  Process {
    id: discoveryProcess
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyDiscovery(text) }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.discoveryStderr = String(text || "").trim()
        if (root.discoveryError !== "" && root.discoveryStderr !== "") root.discoveryError = root.discoveryStderr
      }
    }
    onExited: function(exitCode) {
      root.discoveryLoading = false
      if (exitCode !== 0) root.discoveryError = root.discoveryStderr || "Filesystem discovery failed."
    }
  }

  Process {
    id: scanProcess
    stdout: SplitParser { onRead: function(line) { root.enqueueScanLine(line) } }
    stderr: StdioCollector { waitForEnd: true; onStreamFinished: root.activeStderr = String(text || "").trim() }
    onExited: function(exitCode) {
      root.activeExitCode = exitCode
      root.scanProcessExited = true
      if (root.scanLineQueue.length === 0) scanSettleTimer.restart()
    }
  }

  Timer { id: scanDrainTimer; interval: 0; repeat: true; onTriggered: root.drainScanLines() }
  Timer { id: scanSettleTimer; interval: 50; repeat: false; onTriggered: root.settleScan() }

  PanelWindow {
    id: window
    visible: root.opened
    anchors { top: true; right: true; bottom: true; left: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "omatree"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim
      MouseArea { anchors.fill: parent; onClicked: root.dismiss() }
    }

    BorderSurface {
      id: card
      anchors.centerIn: parent
      width: Math.min(window.width - Style.space(48), Style.space(1080))
      height: Math.min(window.height - Style.space(48), Style.space(700))
      radius: Style.cornerRadius
      color: Color.popups.background
      borderSpec: Border.flat(Color.popups.border, 1)
      padding: Style.space(26)
      MouseArea { anchors.fill: parent; onClicked: function(mouse) { mouse.accepted = true } }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) root.dismiss()
          else if (event.key === Qt.Key_Down || event.key === Qt.Key_J) root.moveTreeSelection(1)
          else if (event.key === Qt.Key_Up || event.key === Qt.Key_K) root.moveTreeSelection(-1)
          else if (event.key === Qt.Key_Right || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.expandSelected()
          else if (event.key === Qt.Key_Left) root.collapseSelected()
          else return
          event.accepted = true
        }
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: Style.space(16)

        RowLayout {
          Layout.fillWidth: true
          Text { Layout.fillWidth: true; text: "OmaTree"; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.title; font.bold: true }
          Button { text: root.discoveryLoading ? "Refreshing…" : "Refresh"; enabled: !root.discoveryLoading; focusable: true; foreground: Color.popups.text; onClicked: root.refresh() }
          Button { text: "Close"; focusable: true; foreground: Color.popups.text; onClicked: root.dismiss() }
        }
        Rectangle { Layout.fillWidth: true; height: 1; color: Color.popups.border; opacity: 0.35 }

        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          spacing: Style.space(16)

          BorderSurface {
            Layout.preferredWidth: Style.space(310)
            Layout.fillHeight: true
            radius: Style.cornerRadius
            color: Style.normalFillFor(Color.popups.text, Color.accent)
            borderSpec: Border.flat(Color.popups.border, 1)
            clip: true
            ListView {
              id: filesystemList
              anchors.fill: parent
              anchors.margins: Style.space(8)
              model: filesystemModel
              clip: true
              spacing: Style.space(4)
              currentIndex: root.selectedFilesystemIndex
              section.property: "groupName"
              section.criteria: ViewSection.FullString
              section.delegate: Text {
                required property string section
                width: filesystemList.width
                topPadding: Style.space(12); bottomPadding: Style.space(6); leftPadding: Style.space(8)
                text: section; color: Color.muted
                font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true
                elide: Text.ElideRight
              }
              delegate: Button {
                required property int index
                required property string displayName
                required property string mountpoint
                required property double totalBytes
                width: filesystemList.width
                text: displayName + "  ·  " + TreeModel.formatBytes(totalBytes) + "\n" + mountpoint
                leftAlign: true
                selected: root.selectedFilesystemIndex === index
                foreground: Color.popups.text
                onClicked: root.selectFilesystem(index)
              }
            }
          }

          ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Style.space(12)
            Text {
              Layout.fillWidth: true
              text: root.discoveryError !== "" ? "Discovery error" : root.selectedFilesystem ? root.selectedFilesystem.displayName : "No filesystems found"
              color: root.discoveryError !== "" ? Color.urgent : Color.popups.text
              font.family: Style.font.family; font.pixelSize: Style.font.heading; font.bold: true
              elide: Text.ElideRight
            }
            Text {
              Layout.fillWidth: true
              text: root.discoveryError !== "" ? root.discoveryError : root.selectedFilesystem ? root.selectedFilesystem.mountpoint : ""
              color: root.discoveryError !== "" ? Color.urgent : Color.muted
              font.family: Style.font.family; font.pixelSize: Style.font.body
              elide: Text.ElideMiddle
            }
            RowLayout {
              Layout.fillWidth: true
              visible: !!root.selectedFilesystem && root.discoveryError === ""
              Text { text: root.selectedFilesystem ? Number(root.selectedFilesystem.usagePercent).toFixed(1) + "% used" : ""; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; font.bold: true }
              Item { Layout.fillWidth: true }
              Text { text: root.selectedFilesystem ? TreeModel.formatBytes(root.selectedFilesystem.usedBytes) + " / " + TreeModel.formatBytes(root.selectedFilesystem.totalBytes) : ""; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
              Text { text: root.selectedFilesystem ? "· " + TreeModel.formatBytes(root.selectedFilesystem.availableBytes) + " available" : ""; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.body }
            }
            Rectangle {
              Layout.fillWidth: true; visible: !!root.selectedFilesystem && root.discoveryError === ""
              height: Style.space(10); radius: height / 2
              color: Style.normalFillFor(Color.popups.text, Color.accent)
              Rectangle {
                width: parent.width * Math.max(0, Math.min(100, root.selectedFilesystem ? Number(root.selectedFilesystem.usagePercent) : 0)) / 100
                height: parent.height; radius: parent.radius; color: Color.accent
              }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: Color.popups.border; opacity: 0.25 }
            RowLayout {
              Layout.fillWidth: true
              visible: root.scanning
              spacing: Style.space(8)
              BusyIndicator { running: root.scanning; implicitWidth: Style.space(20); implicitHeight: Style.space(20) }
              ColumnLayout {
                Layout.fillWidth: true; spacing: 0
                Text { Layout.fillWidth: true; text: "Scanning " + root.activePath + "…"; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideMiddle }
                Text { text: root.progressEntries.toLocaleString() + " entries · " + TreeModel.formatBytes(root.progressBytes) + " processed"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
              }
            }

            BorderSurface {
              Layout.fillWidth: true; Layout.fillHeight: true
              radius: Style.cornerRadius; color: "transparent"
              borderSpec: Border.flat(Color.popups.border, 1); clip: true
              ListView {
                id: treeView
                anchors.fill: parent; anchors.margins: Style.space(6)
                model: treeRows; clip: true; spacing: Style.space(2)
                currentIndex: root.selectedTreeIndex
                delegate: BorderSurface {
                  required property int index
                  required property string nodeName
                  required property string nodePath
                  required property double nodeBytes
                  required property string formattedSize
                  required property int depth
                  required property bool expanded
                  required property bool loading
                  required property bool loaded
                  required property int warningCount
                  required property string warningText
                  required property string errorText
                  width: treeView.width
                  height: Math.max(Style.space(42), rowContent.implicitHeight + Style.space(10))
                  radius: Style.cornerRadius
                  color: mouse.containsMouse || root.selectedTreePath === nodePath ? Style.hoverFillFor(Color.popups.text, Color.accent) : "transparent"
                  borderSpec: Border.none()
                  MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; z: 0; onClicked: root.toggleNode(nodePath) }
                  RowLayout {
                    id: rowContent; z: 1
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(8) + depth * Style.space(20)
                    anchors.rightMargin: Style.space(10)
                    spacing: Style.space(8)
                    Text { text: loading ? "◌" : expanded ? "▾" : "▸"; color: errorText !== "" ? Color.urgent : Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
                    Text { Layout.fillWidth: true; text: nodeName; color: errorText !== "" ? Color.urgent : Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideMiddle }
                    Text { visible: warningCount > 0; text: "⚠ " + warningCount; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
                    Button { visible: errorText !== ""; text: "Retry"; foreground: Color.urgent; onClicked: root.retryNode(nodePath) }
                    Text { text: loading ? "Scanning…" : formattedSize; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.body; horizontalAlignment: Text.AlignRight }
                  }
                  ToolTip { visible: mouse.containsMouse && (warningText !== "" || errorText !== ""); text: errorText !== "" ? errorText : warningText; delay: 400 }
                }
                Text {
                  anchors.centerIn: parent
                  visible: treeRows.count === 0 && !root.discoveryLoading
                  text: root.discoveryError !== "" ? root.discoveryError : "No directory data"
                  color: root.discoveryError !== "" ? Color.urgent : Color.muted
                  font.family: Style.font.family; font.pixelSize: Style.font.body
                }
              }
            }
          }
        }
      }
    }
  }
}
