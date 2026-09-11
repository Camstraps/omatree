import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "TreeModel.js" as TreeModel
import "FrontendSafety.js" as FrontendSafety

Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false
  property bool discoveryLoading: false
  property string discoveryError: ""
  property string discoveryStderr: ""
  property string discoveryStdout: ""
  property int discoveryStdoutBytes: 0
  property int discoveryStderrBytes: 0
  property bool discoveryFailed: false
  property bool discoveryTerminationRequested: false
  property bool discoveryRefreshPending: false
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
  property var activeDirectoryCache: ({})
  property var activePendingChildren: ({})
  property int activeDirectoryCount: 0
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
  property int scanQueuedLineCount: 0
  property int scanQueuedBytes: 0
  property bool scanAcceptingRecords: false
  property bool scanTerminationRequested: false
  property string scanTerminationRequestId: ""
  property int scanStderrBytes: 0
  property var pendingScan: null
  property int progressEntries: 0
  property double progressBytes: 0
  property double scanStartedAt: 0
  property double scanElapsedSeconds: 0
  property int snapshotDirectoryCount: 0
  property double snapshotDurationMs: 0
  property string snapshotState: "idle"
  property string searchQuery: ""
  property var searchKeys: []
  property int searchIndex: 0
  property var searchHeap: []
  property int searchMatchCount: 0
  property bool searchRunning: false
  property int searchResultLimit: 500
  property string actionFeedback: ""
  property int copyStdoutBytes: 0
  property int copyStderrBytes: 0
  property bool copyTerminationRequested: false

  readonly property int maxQueuedScanLines: 4096
  readonly property int maxQueuedScanBytes: 16 * 1024 * 1024
  readonly property int maxEventBytes: 64 * 1024
  readonly property int maxStagedDirectories: 500000
  readonly property int maxPathBytes: 4096
  readonly property int maxDiscoveryStdoutBytes: 4 * 1024 * 1024
  readonly property int maxDiagnosticBytes: 64 * 1024

  readonly property string pluginId: (manifest && manifest.id)
    ? String(manifest.id) : "io.github.camstraps.omatree"
  readonly property url helperUrl: Qt.resolvedUrl("helper/discover.py")
  readonly property string helperPath: decodeURIComponent(String(helperUrl).replace(/^file:\/\//, ""))
  readonly property var selectedFilesystem:
    selectedFilesystemIndex >= 0 && selectedFilesystemIndex < filesystemModel.count
      ? filesystemModel.get(selectedFilesystemIndex) : null
  readonly property bool scanning: activeRequestId !== "" || scanProcess.running

  function utf8Bytes(value) {
    return FrontendSafety.utf8Bytes(value, maxEventBytes + 1)
  }

  function clearScanQueue() {
    scanDrainTimer.stop()
    scanLineQueue = []
    scanLineQueueIndex = 0
    scanQueuedLineCount = 0
    scanQueuedBytes = 0
  }

  function requestScanTermination() {
    if (scanTerminationRequested || !scanProcess.running) return
    scanTerminationRequested = true
    scanTerminationRequestId = activeRequestId
    scanProcess.signal(15)
    scanKillTimer.restart()
  }

  function failActiveScan(message) {
    if (activeRequestId === "" || !scanAcceptingRecords) return
    activeProtocolError = String(message || "Directory scan failed.")
    activeComplete = null
    scanAcceptingRecords = false
    activeDirectoryCache = ({})
    activePendingChildren = ({})
    activeDirectoryCount = 0
    clearScanQueue()
    requestScanTermination()
    if (!scanProcess.running) {
      scanProcessExited = true
      maybeSettleScan()
    }
  }

  function resetDiscoveryOutput() {
    discoveryStdout = ""
    discoveryStderr = ""
    discoveryStdoutBytes = 0
    discoveryStderrBytes = 0
    discoveryFailed = false
  }

  function requestDiscoveryTermination() {
    if (discoveryTerminationRequested || !discoveryProcess.running) return
    discoveryTerminationRequested = true
    discoveryProcess.signal(15)
    discoveryKillTimer.restart()
  }

  function failDiscovery(message) {
    discoveryFailed = true
    discoveryError = String(message || "Filesystem discovery failed.")
    discoveryStdout = ""
    requestDiscoveryTermination()
  }

  function appendDiscoveryOutput(raw, isError) {
    if (discoveryFailed) return
    var value = String(raw || "")
    var bytes = utf8Bytes(value) + 1
    var current = isError ? discoveryStderrBytes : discoveryStdoutBytes
    var limit = isError ? maxDiagnosticBytes : maxDiscoveryStdoutBytes
    if (FrontendSafety.outputLimitExceeded(bytes, current, limit)) {
      failDiscovery("Filesystem discovery exceeded its output limit.")
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

  function appendScanStderr(raw) {
    if (activeRequestId === "" || !scanAcceptingRecords) return
    var value = String(raw || "")
    var bytes = utf8Bytes(value) + 1
    if (FrontendSafety.outputLimitExceeded(
          bytes, scanStderrBytes, maxDiagnosticBytes)) {
      activeStderr = ""
      scanStderrBytes = 0
      failActiveScan("Directory scanner exceeded its diagnostic output limit.")
      return
    }
    activeStderr += value + "\n"
    scanStderrBytes += bytes
  }

  function boundCopyOutput(raw, isError) {
    var bytes = utf8Bytes(raw) + 1
    var current = isError ? copyStderrBytes : copyStdoutBytes
    if (FrontendSafety.outputLimitExceeded(
          bytes, current, maxDiagnosticBytes)) {
      if (!copyTerminationRequested && copyProcess.running) {
        copyTerminationRequested = true
        copyProcess.signal(15)
        copyKillTimer.restart()
      }
      return
    }
    if (isError) copyStderrBytes += bytes
    else copyStdoutBytes += bytes
  }

  function launchDiscovery() {
    discoveryRefreshPending = false
    discoveryTerminationRequested = false
    resetDiscoveryOutput()
    discoveryLoading = true
    discoveryError = ""
    discoveryProcess.command = ["/usr/bin/python3", helperPath, "discover"]
    discoveryProcess.running = true
    discoveryDeadlineTimer.restart()
  }

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
    if (discoveryProcess.running) {
      discoveryRefreshPending = true
      requestDiscoveryTermination()
    } else launchDiscovery()
  }

  function clearTree() {
    treeCache = ({})
    treeRootPath = ""
    selectedTreePath = ""
    selectedTreeIndex = -1
    progressEntries = 0
    progressBytes = 0
    treeRows.clear()
    breadcrumbModel.clear()
    cancelSearch()
    snapshotDirectoryCount = 0
    snapshotDurationMs = 0
    snapshotState = "idle"
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
    if (searchQuery.trim() !== "") return
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
        errorText: node.error,
        hasChildren: node.childDirectoryCount > 0,
        percentParent: TreeModel.percentageOfParent(treeCache, node.path,
          selectedFilesystem ? selectedFilesystem.totalBytes : node.bytes),
        contextPath: node.path, searchResult: false
      })
      if (node.path === selectedTreePath) selectedTreeIndex = i
    }
    if (selectedTreeIndex < 0 && treeRows.count > 0) {
      selectedTreeIndex = 0
      selectedTreePath = treeRows.get(0).nodePath
    }
    rebuildBreadcrumb()
  }

  function selectTreePath(path, reveal) {
    if (!treeCache[path]) return
    selectedTreePath = path
    if (reveal) TreeModel.revealPath(treeCache, path)
    rebuildTreeRows()
    if (selectedTreeIndex >= 0) treeView.positionViewAtIndex(selectedTreeIndex, ListView.Contain)
    rebuildBreadcrumb()
  }

  function rebuildBreadcrumb() {
    breadcrumbModel.clear()
    var paths = TreeModel.ancestorPaths(treeCache, selectedTreePath)
    for (var i = 0; i < paths.length; i++) {
      var node = treeCache[paths[i]]
      breadcrumbModel.append({ crumbName: node ? node.name : paths[i], crumbPath: paths[i] })
    }
  }

  function cancelSearch() {
    searchTimer.stop()
    searchDebounce.stop()
    searchKeys = []
    searchHeap = []
    searchIndex = 0
    searchMatchCount = 0
    searchRunning = false
  }

  function scheduleSearch() {
    cancelSearch()
    if (searchQuery.trim() === "") { rebuildTreeRows(); return }
    treeRows.clear()
    searchDebounce.restart()
  }

  function beginSearch() {
    var query = searchQuery.trim().toLocaleLowerCase()
    if (query === "") { rebuildTreeRows(); return }
    searchKeys = Object.keys(treeCache)
    searchIndex = 0
    searchHeap = []
    searchMatchCount = 0
    searchRunning = true
    searchTimer.start()
  }

  function searchBatch() {
    var query = searchQuery.trim().toLocaleLowerCase()
    if (query === "") { cancelSearch(); rebuildTreeRows(); return }
    var end = Math.min(searchKeys.length, searchIndex + 1500)
    while (searchIndex < end) {
      var node = treeCache[searchKeys[searchIndex++]]
      if (node && node.name.toLocaleLowerCase().indexOf(query) !== -1) {
        searchMatchCount++
        TreeModel.offerSearchMatch(searchHeap, node, searchResultLimit)
      }
    }
    if (searchIndex < searchKeys.length) return
    searchTimer.stop()
    searchRunning = false
    var results = TreeModel.sortedSearchHeap(searchHeap)
    treeRows.clear()
    selectedTreeIndex = -1
    for (var i = 0; i < results.length; i++) {
      var item = results[i]
      treeRows.append({ nodeName: item.name, nodePath: item.path, nodeBytes: item.bytes,
        formattedSize: item.formattedSize, depth: 0, expanded: item.expanded,
        loading: false, loaded: true, warningCount: item.warningCount,
        warningText: item.warningText, errorText: item.error,
        hasChildren: item.childDirectoryCount > 0,
        percentParent: TreeModel.percentageOfParent(treeCache, item.path,
          selectedFilesystem ? selectedFilesystem.totalBytes : item.bytes),
        contextPath: item.path, searchResult: true })
    }
  }

  function chooseSearchResult(path) {
    searchField.text = ""
    searchQuery = ""
    cancelSearch()
    selectTreePath(path, true)
  }

  function copySelectedPath() {
    if (!selectedTreePath || copyProcess.running) return
    copyProcess.pathToCopy = selectedTreePath
    copyStdoutBytes = 0
    copyStderrBytes = 0
    copyTerminationRequested = false
    copyProcess.command = ["/usr/bin/wl-copy"]
    copyProcess.stdinEnabled = true
    copyProcess.running = true
  }

  function openSelectedFolder() {
    if (!selectedTreePath) return
    Quickshell.execDetached(["/usr/bin/xdg-open", selectedTreePath])
    actionFeedback = "Opened folder"
    feedbackTimer.restart()
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
    }
    rebuildBreadcrumb()
  }

  function retryNode(path) {
    var node = treeCache[path]
    if (!node) return
    node.error = ""
    node.warningText = ""
    requestScan(treeRootPath)
  }

  function requestScan(path) {
    var filesystem = selectedFilesystem
    var node = treeCache[path]
    if (!filesystem || !node || path !== treeRootPath) return
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
    activeDirectoryCache = ({})
    activePendingChildren = ({})
    activeDirectoryCount = 0
    activeWarningCount = 0
    activeFirstWarning = ""
    activeProtocolError = ""
    activeStderr = ""
    activeComplete = null
    activeCancelled = false
    activeExpectedStop = false
    activeExitCode = 0
    scanProcessExited = false
    clearScanQueue()
    scanAcceptingRecords = true
    scanTerminationRequested = false
    scanTerminationRequestId = ""
    scanStderrBytes = 0
    progressEntries = 0
    progressBytes = 0
    scanStartedAt = Date.now()
    scanElapsedSeconds = 0
    snapshotState = "scanning"
    node.loading = true
    node.error = ""
    node.requestId = activeRequestId
    rebuildTreeRows()
    scanProcess.command = [
      "/usr/bin/python3", helperPath, "scan",
      "--mountpoint", request.mountpoint,
      "--path", request.path,
      "--request-id", activeRequestId
    ]
    scanProcess.running = true
    scanDeadlineTimer.restart()
  }

  function cancelActiveScan() {
    if (activeRequestId === "" && !scanProcess.running) return
    activeExpectedStop = true
    activeComplete = null
    scanAcceptingRecords = false
    clearScanQueue()
    var node = treeCache[activePath]
    if (node && node.requestId === activeRequestId) {
      node.loading = false
      node.requestId = ""
      rebuildTreeRows()
    }
    if (scanProcess.running) requestScanTermination()
    else {
      scanProcessExited = true
      settleScan()
    }
  }

  function handleScanLine(rawLine) {
    if (!scanAcceptingRecords) return
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
    } else if (message.type === "directory") {
      var validationError = FrontendSafety.directoryLimitError(
        message, activeDirectoryCount, maxStagedDirectories, maxPathBytes)
      if (validationError !== "") { failActiveScan(validationError); return }
      var directoryError = TreeModel.stageDirectory(activeDirectoryCache, activePendingChildren, {
        name: String(message.name || message.path || "Directory"),
        path: String(message.path || ""),
        parentPath: message.parentPath === null ? null : String(message.parentPath || ""),
        bytes: Number(message.bytes || 0),
        directFilesBytes: Number(message.directFilesBytes || 0),
        childDirectoryCount: Number(message.childDirectoryCount || 0),
        warningCount: Number(message.warningCount || 0)
      })
      if (directoryError !== "") {
        failActiveScan(directoryError)
        return
      }
      else activeDirectoryCount++
    } else if (message.type === "complete") activeComplete = message
    else if (message.type === "cancelled") activeCancelled = true
    else if (message.type === "error") activeProtocolError = String(message.error || "Directory scan failed.")
  }

  function enqueueScanLine(line) {
    if (!scanAcceptingRecords || activeRequestId === "") return
    var value = String(line || "")
    var bytes = utf8Bytes(value) + 1
    var queueError = FrontendSafety.queueLimitError(
      bytes, scanQueuedLineCount, scanQueuedBytes,
      maxEventBytes, maxQueuedScanLines, maxQueuedScanBytes)
    if (queueError !== "") { failActiveScan(queueError); return }
    scanLineQueue.push({ text: value, bytes: bytes })
    scanQueuedLineCount++
    scanQueuedBytes += bytes
    if (!scanDrainTimer.running) scanDrainTimer.start()
  }

  function drainScanLines() {
    var count = Math.min(scanLineQueue.length, 100)
    var batch = scanLineQueue.splice(0, count)
    for (var i = 0; i < batch.length; i++) {
      scanQueuedLineCount--
      scanQueuedBytes -= batch[i].bytes
      handleScanLine(batch[i].text)
      if (!scanAcceptingRecords) break
    }
    scanLineQueueIndex = 0
    if (scanLineQueue.length === 0) {
      scanDrainTimer.stop()
      scanQueuedLineCount = 0
      scanQueuedBytes = 0
      maybeSettleScan()
    }
  }

  function maybeSettleScan() {
    if (!scanProcessExited || scanQueuedLineCount > 0 || scanDrainTimer.running) return
    // A successful helper run always ends with a complete record. Waiting for
    // that terminal record also handles onExited arriving before SplitParser.
    if (activeExpectedStop || activeCancelled || activeComplete
        || activeProtocolError !== "" || activeExitCode !== 0) settleScan()
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
        var expectedCount = Number(activeComplete.directoryCount || 0)
        var built = expectedCount === activeDirectoryCount
          ? TreeModel.finalizeTree(
              activeDirectoryCache, activePendingChildren,
              activeDirectoryCount, path, node.name)
          : { error: "Scanner returned an incomplete directory tree." }
        if (built.error !== "") {
          node.error = built.error
        } else {
          treeCache = built.cache
          var completedRoot = treeCache[path]
          completedRoot.expanded = true
          completedRoot.requestId = ""
          completedRoot.warningCount = Number(activeComplete.warningCount || activeWarningCount)
          completedRoot.warningText = completedRoot.warningCount > 0
            ? String(completedRoot.warningCount) + " path" + (completedRoot.warningCount === 1 ? "" : "s") + " could not be read"
            : ""
          snapshotDirectoryCount = activeDirectoryCount
          snapshotDurationMs = Number(activeComplete.durationMs || 0)
          snapshotState = "complete"
        }
      } else if (!activeExpectedStop && !activeCancelled) {
        node.error = activeProtocolError || activeStderr || "Directory scan failed."
        snapshotState = "failed"
      }
      rebuildTreeRows()
    }
    activeRequestId = ""
    activePath = ""
    activeGeneration = -1
    activeDirectoryCache = ({})
    activePendingChildren = ({})
    activeDirectoryCount = 0
    activeComplete = null
    scanAcceptingRecords = false
    scanDeadlineTimer.stop()
    scanKillTimer.stop()
    scanTerminationRequested = false
    scanTerminationRequestId = ""
    scanStderrBytes = 0
    clearScanQueue()
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
    rebuildBreadcrumb()
  }

  function expandSelected() {
    var node = treeCache[selectedTreePath]
    if (node && node.childDirectoryCount > 0 && !node.expanded) toggleNode(node.path)
  }

  function collapseSelected() {
    var node = treeCache[selectedTreePath]
    if (node && node.expanded) toggleNode(node.path)
    else if (node && node.parentPath) selectTreePath(node.parentPath, false)
  }

  ListModel { id: filesystemModel }
  ListModel { id: treeRows }
  ListModel { id: breadcrumbModel }

  Process {
    id: copyProcess
    property string pathToCopy: ""
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.boundCopyOutput(line, false) } }
    stderr: SplitParser { onRead: function(line) { root.boundCopyOutput(line, true) } }
    onStarted: { write(pathToCopy); stdinEnabled = false }
    onExited: function(code) {
      copyKillTimer.stop()
      copyTerminationRequested = false
      root.actionFeedback = code === 0 ? "Path copied" : "Could not copy path"
      feedbackTimer.restart()
      pathToCopy = ""
    }
  }

  Process {
    id: discoveryProcess
    stdout: SplitParser { onRead: function(line) { root.appendDiscoveryOutput(line, false) } }
    stderr: SplitParser { onRead: function(line) { root.appendDiscoveryOutput(line, true) } }
    onExited: function(exitCode) {
      discoveryDeadlineTimer.stop()
      discoveryKillTimer.stop()
      discoveryTerminationRequested = false
      root.discoveryLoading = false
      if (!root.discoveryRefreshPending && !root.discoveryFailed && exitCode === 0)
        root.applyDiscovery(root.discoveryStdout)
      else if (!root.discoveryRefreshPending && !root.discoveryFailed)
        root.discoveryError = root.discoveryStderr.trim() || "Filesystem discovery failed."
      root.discoveryStdout = ""
      root.discoveryStderr = ""
      root.discoveryStdoutBytes = 0
      root.discoveryStderrBytes = 0
      if (root.discoveryRefreshPending)
        Qt.callLater(function() { root.launchDiscovery() })
    }
  }

  Process {
    id: scanProcess
    stdout: SplitParser { onRead: function(line) { root.enqueueScanLine(line) } }
    stderr: SplitParser { onRead: function(line) { root.appendScanStderr(line) } }
    onExited: function(exitCode) {
      scanDeadlineTimer.stop()
      scanKillTimer.stop()
      scanTerminationRequested = false
      scanTerminationRequestId = ""
      root.activeExitCode = exitCode
      root.scanProcessExited = true
      root.maybeSettleScan()
    }
  }

  // Qt timers do not run with a zero interval in the installed Quickshell/Qt
  // combination. One millisecond retains batched UI updates without stalling.
  Timer { id: scanDrainTimer; interval: 1; repeat: true; onTriggered: root.drainScanLines() }
  Timer {
    id: scanDeadlineTimer
    interval: 60 * 60 * 1000
    onTriggered: root.failActiveScan("Directory scan timed out after 60 minutes.")
  }
  Timer {
    id: scanKillTimer
    interval: 2000
    onTriggered: {
      if (scanProcess.running && root.scanTerminationRequested
          && root.scanTerminationRequestId === root.activeRequestId)
        scanProcess.signal(9)
    }
  }
  Timer {
    id: discoveryDeadlineTimer
    interval: 15000
    onTriggered: root.failDiscovery("Filesystem discovery timed out after 15 seconds.")
  }
  Timer {
    id: discoveryKillTimer
    interval: 2000
    onTriggered: {
      if (discoveryProcess.running && root.discoveryTerminationRequested)
        discoveryProcess.signal(9)
    }
  }
  Timer {
    id: copyKillTimer
    interval: 2000
    onTriggered: {
      if (copyProcess.running && root.copyTerminationRequested) copyProcess.signal(9)
    }
  }
  Timer { interval: 250; repeat: true; running: root.scanning; onTriggered: root.scanElapsedSeconds = (Date.now() - root.scanStartedAt) / 1000 }
  Timer { id: searchDebounce; interval: 180; onTriggered: root.beginSearch() }
  Timer { id: searchTimer; interval: 1; repeat: true; onTriggered: root.searchBatch() }
  Timer { id: feedbackTimer; interval: 1800; onTriggered: root.actionFeedback = "" }

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
      width: Math.min(window.width - Style.space(48), Style.space(1240))
      height: Math.min(window.height - Style.space(48), Style.space(820))
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
          if (searchField.activeFocus) {
            if (event.key === Qt.Key_Escape) {
              searchField.text = ""
              searchField.focus = false
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
            else if (event.key === Qt.Key_Down) { root.moveTreeSelection(1); event.accepted = true }
            else if (event.key === Qt.Key_Up) { root.moveTreeSelection(-1); event.accepted = true }
            else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                     && root.selectedTreeIndex >= 0) {
              root.chooseSearchResult(treeRows.get(root.selectedTreeIndex).nodePath)
              event.accepted = true
            }
            return
          }
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
            Layout.preferredWidth: Math.min(Style.space(310), card.width * 0.31)
            Layout.minimumWidth: Style.space(230)
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
              delegate: BorderSurface {
                required property int index
                required property string displayName
                required property string mountpoint
                required property double totalBytes
                required property double usedBytes
                required property double usagePercent
                width: filesystemList.width
                height: Style.space(78)
                radius: Style.cornerRadius
                color: root.selectedFilesystemIndex === index || fsMouse.containsMouse
                  ? Style.hoverFillFor(Color.popups.text, Color.accent) : "transparent"
                borderSpec: Border.none()
                MouseArea { id: fsMouse; anchors.fill: parent; hoverEnabled: true; onClicked: root.selectFilesystem(index) }
                ColumnLayout {
                  anchors.fill: parent; anchors.margins: Style.space(8); spacing: 1
                  Text { Layout.fillWidth: true; text: displayName; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; font.bold: true; elide: Text.ElideRight }
                  Text { Layout.fillWidth: true; text: mountpoint; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; elide: Text.ElideMiddle }
                  RowLayout {
                    Layout.fillWidth: true; spacing: Style.space(6)
                    Text { text: TreeModel.formatBytes(usedBytes) + " / " + TreeModel.formatBytes(totalBytes); color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
                    Item { Layout.fillWidth: true }
                    Text { text: Number(usagePercent).toFixed(1) + "%"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
                  }
                  Rectangle {
                    Layout.fillWidth: true; height: Style.space(4); radius: height / 2
                    color: Style.normalFillFor(Color.popups.text, Color.accent)
                    Rectangle { width: parent.width * TreeModel.clampPercent(usagePercent) / 100; height: parent.height; radius: parent.radius; color: Color.accent; opacity: 0.75 }
                  }
                }
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
                Text { text: root.progressEntries.toLocaleString() + " entries · " + TreeModel.formatBytes(root.progressBytes) + " processed · " + root.scanElapsedSeconds.toFixed(1) + " s"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
              }
            }

            RowLayout {
              Layout.fillWidth: true
              visible: !root.scanning && root.snapshotState === "complete"
              Text { Layout.fillWidth: true; text: "Snapshot: " + root.snapshotDirectoryCount.toLocaleString() + " directories · " + (root.snapshotDurationMs / 1000).toFixed(1) + " s"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
              Text { visible: root.actionFeedback !== ""; text: root.actionFeedback; color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
            }

            RowLayout {
              Layout.fillWidth: true; spacing: Style.space(8)
              TextField {
                id: searchField
                Layout.fillWidth: true
                placeholderText: "Search directories in this snapshot…"
                foreground: Color.popups.text; accent: Color.accent
                enabled: root.snapshotState === "complete"
                onTextChanged: { root.searchQuery = text; root.scheduleSearch() }
                Keys.onEscapePressed: { text = ""; focus = false; keyCatcher.forceActiveFocus() }
              }
              PanelActionButton { iconText: "⧉"; tooltipText: "Copy selected path"; enabled: root.selectedTreePath !== ""; onClicked: root.copySelectedPath() }
              PanelActionButton { iconText: "↗"; tooltipText: "Open selected folder"; enabled: root.selectedTreePath !== ""; onClicked: root.openSelectedFolder() }
            }

            Flickable {
              Layout.fillWidth: true; Layout.preferredHeight: Style.space(28)
              contentWidth: breadcrumbRow.implicitWidth; contentHeight: height
              clip: true; boundsBehavior: Flickable.StopAtBounds
              Row {
                id: breadcrumbRow; height: parent.height; spacing: Style.space(4)
                Repeater {
                  model: breadcrumbModel
                  delegate: Row {
                    required property int index
                    required property string crumbName
                    required property string crumbPath
                    height: breadcrumbRow.height; spacing: Style.space(4)
                    Text { visible: index > 0; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
                    Button { anchors.verticalCenter: parent.verticalCenter; text: crumbName; foreground: Color.popups.text; onClicked: root.selectTreePath(crumbPath, true) }
                  }
                }
              }
              Component.onCompleted: contentX = Math.max(0, contentWidth - width)
              onContentWidthChanged: contentX = Math.max(0, contentWidth - width)
            }

            BorderSurface {
              Layout.fillWidth: true; Layout.fillHeight: true
              radius: Style.cornerRadius; color: "transparent"
              borderSpec: Border.flat(Color.popups.border, 1); clip: true
              ColumnLayout {
                anchors.fill: parent; anchors.margins: Style.space(6); spacing: 0
                RowLayout {
                  Layout.fillWidth: true; Layout.preferredHeight: Style.space(28); spacing: Style.space(8)
                  Text { Layout.fillWidth: true; text: root.searchQuery.trim() === "" ? "Name" : "Name / path"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                  Text { Layout.preferredWidth: Style.space(94); text: "Size"; horizontalAlignment: Text.AlignRight; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                  Text { Layout.preferredWidth: Style.space(64); text: "% Parent"; horizontalAlignment: Text.AlignRight; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: Color.popups.border; opacity: 0.3 }
              ListView {
                id: treeView
                Layout.fillWidth: true; Layout.fillHeight: true
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
                  required property bool hasChildren
                  required property double percentParent
                  required property string contextPath
                  required property bool searchResult
                  width: treeView.width
                  height: searchResult ? Style.space(48) : Style.space(34)
                  radius: Style.cornerRadius
                  color: root.selectedTreePath === nodePath ? Style.selectionFillFor(Color.popups.text, Color.accent)
                    : mouse.containsMouse ? Style.hoverFillFor(Color.popups.text, Color.accent) : "transparent"
                  borderSpec: Border.none()
                  Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: parent.width * percentParent / 100; color: Color.accent; opacity: 0.075; radius: parent.radius }
                  MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; z: 1; onClicked: searchResult ? root.chooseSearchResult(nodePath) : root.selectTreePath(nodePath, false); onDoubleClicked: if (!searchResult) root.toggleNode(nodePath) }
                  RowLayout {
                    id: rowContent; z: 2
                    anchors.fill: parent
                    anchors.leftMargin: Style.space(8) + (searchResult ? 0 : depth * Style.space(18))
                    anchors.rightMargin: Style.space(10)
                    spacing: Style.space(8)
                    Item {
                      visible: !searchResult; Layout.preferredWidth: Style.space(18); Layout.fillHeight: true; z: 3
                      Text { anchors.centerIn: parent; text: loading ? "◌" : hasChildren ? (expanded ? "▾" : "▸") : ""; color: errorText !== "" ? Color.urgent : Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
                      MouseArea { anchors.fill: parent; enabled: hasChildren; onClicked: function(mouse) { mouse.accepted = true; root.toggleNode(nodePath) } }
                    }
                    ColumnLayout {
                      Layout.fillWidth: true; spacing: 0
                      Text { Layout.fillWidth: true; text: nodeName; color: errorText !== "" ? Color.urgent : Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideRight }
                      Text { Layout.fillWidth: true; visible: searchResult; text: contextPath; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; elide: Text.ElideMiddle }
                    }
                    Text { visible: warningCount > 0; text: "⚠ " + warningCount; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
                    Button { visible: errorText !== ""; text: "Retry"; foreground: Color.urgent; onClicked: root.retryNode(nodePath) }
                    Text { Layout.preferredWidth: Style.space(94); text: loading ? "Scanning…" : formattedSize; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; horizontalAlignment: Text.AlignRight }
                    Text { Layout.preferredWidth: Style.space(64); text: percentParent.toFixed(1) + "%"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.body; horizontalAlignment: Text.AlignRight }
                  }
                  PanelToolTip { visible: mouse.containsMouse; text: errorText !== "" ? errorText : warningText !== "" ? warningText : nodePath }
                }
                Text {
                  anchors.centerIn: parent
                  visible: treeRows.count === 0 && !root.discoveryLoading
                  text: root.discoveryError !== "" ? root.discoveryError : root.searchRunning ? "Searching snapshot…" : root.searchQuery.trim() !== "" ? "No matching directories" : "No directory data"
                  color: root.discoveryError !== "" ? Color.urgent : Color.muted
                  font.family: Style.font.family; font.pixelSize: Style.font.body
                }
              }
              Text { Layout.fillWidth: true; visible: root.searchQuery.trim() !== "" && !root.searchRunning; text: root.searchMatchCount > root.searchResultLimit ? "Showing the largest " + root.searchResultLimit + " of " + root.searchMatchCount.toLocaleString() + " matches" : root.searchMatchCount.toLocaleString() + " matches"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; horizontalAlignment: Text.AlignRight }
              }
            }
          }
        }
      }
    }
  }
}
