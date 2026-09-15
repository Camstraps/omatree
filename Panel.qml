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
import "BrowserState.js" as BrowserState
import "SearchState.js" as SearchState

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

  property string treeRootPath: ""
  property string selectedTreePath: ""
  property int selectedTreeIndex: -1
  property int treeGeneration: 0
  property int requestSerial: 0
  property bool brokerReady: false
  property bool brokerExpectedStop: false
  property bool brokerTerminationRequested: false
  property string brokerError: ""
  property string brokerStderr: ""
  property int brokerStderrBytes: 0
  property var brokerLineQueue: []
  property int brokerQueuedLineCount: 0
  property int brokerQueuedBytes: 0
  property var brokerRequests: ({})
  property int brokerRequestCount: 0
  property string scanRequestId: ""
  property string stagingGenerationId: ""
  property bool scanCancellationRequested: false
  property var pendingActivation: null
  property string activeGenerationId: ""
  property bool activeBackendAvailable: false
  property var activeRootMetadata: null
  property var browserState: null
  property var pendingNavigation: ({})
  property string breadcrumbTarget: ""
  property bool breadcrumbRequestRunning: false
  property var activeScanWarnings: []
  property var pendingScan: null
  property int progressEntries: 0
  property double progressBytes: 0
  property double scanStartedAt: 0
  property double scanElapsedSeconds: 0
  property int snapshotDirectoryCount: 0
  property double snapshotDurationMs: 0
  property string snapshotState: "idle"
  property string searchQuery: ""
  property bool searchRunning: false
  property var searchState: null
  property int searchSerial: 0
  property string searchRequestId: ""
  property int searchRows: 0
  property int searchPages: 0
  property int searchOutstanding: 0
  property int searchSessionsRetained: 0
  property int searchRowsHighWater: 0
  property int searchPagesHighWater: 0
  property int searchOutstandingHighWater: 0
  property int searchSessionsHighWater: 0
  property var revealState: null
  property string browserViewRootPath: ""
  property string actionFeedback: ""
  property int copyStdoutBytes: 0
  property int copyStderrBytes: 0
  property bool copyTerminationRequested: false

  readonly property int brokerProtocolVersion: 1
  readonly property int maxBrokerLineBytes: 1024 * 1024
  readonly property int maxBrokerQueueLines: 256
  readonly property int maxBrokerQueueBytes: 4 * 1024 * 1024
  readonly property int maxBrokerRequests: 4
  readonly property int maxBrokerRows: 64
  readonly property int childPageRows: 64
  readonly property int maxCachedDirectoryRows: 2048
  readonly property int maxCachedPages: 24
  readonly property int maxVisibleRows: 512
  readonly property int maxExpansionStates: 256
  readonly property int maxBreadcrumbRows: 128
  readonly property int maxMetadataCacheRows: 256
  readonly property int searchPageRows: 64
  readonly property int maxSearchResultRows: 128
  readonly property int maxSearchPages: 2
  readonly property int maxSearchQueryBytes: 1024
  readonly property int maxPathBytes: 4096
  readonly property int maxRequestIdBytes: 128
  readonly property int maxGenerationIdBytes: 128
  readonly property int maxDiscoveryStdoutBytes: 4 * 1024 * 1024
  readonly property int maxDiagnosticBytes: 64 * 1024

  // Test-visible logical high-water marks. These count only bounded broker
  // transport/model state, never total scanned directories.
  property int brokerQueueLinesHighWater: 0
  property int brokerQueueBytesHighWater: 0
  property int brokerRequestsHighWater: 0
  property int activationRowsHighWater: 0
  property int activeRowsHighWater: 0
  property int generationIdsHighWater: 0
  property int warningRowsHighWater: 0
  property int cachedDirectoryRowsHighWater: 0
  property int cachedPagesHighWater: 0
  property int visibleRowsHighWater: 0
  property int expansionStatesHighWater: 0
  property int breadcrumbRowsHighWater: 0
  property int metadataRowsHighWater: 0
  property int cachedDirectoryRows: 0
  property int cachedPages: 0
  property int visibleRows: 0
  property int expansionStates: 0
  property int breadcrumbRows: 0
  property int metadataRows: 0

  readonly property string pluginId: (manifest && manifest.id)
    ? String(manifest.id) : "io.github.camstraps.omatree"
  readonly property url helperUrl: Qt.resolvedUrl("helper/discover.py")
  readonly property string helperPath: decodeURIComponent(String(helperUrl).replace(/^file:\/\//, ""))
  readonly property url brokerUrl: Qt.resolvedUrl("bin/omatree-broker")
  readonly property string brokerPath: decodeURIComponent(String(brokerUrl).replace(/^file:\/\//, ""))
  readonly property var selectedFilesystem:
    selectedFilesystemIndex >= 0 && selectedFilesystemIndex < filesystemModel.count
      ? filesystemModel.get(selectedFilesystemIndex) : null
  readonly property bool scanning: scanRequestId !== ""

  function utf8Bytes(value) {
    return FrontendSafety.utf8Bytes(value, maxBrokerLineBytes + 1)
  }

  function clearBrokerQueue() {
    brokerDrainTimer.stop()
    brokerLineQueue = []
    brokerQueuedLineCount = 0
    brokerQueuedBytes = 0
  }

  function clearBrokerRequests() {
    brokerRequests = ({})
    brokerRequestCount = 0
    pendingNavigation = ({})
    breadcrumbRequestRunning = false
    brokerRequestDeadline.stop()
  }

  function clearNavigationRequests() {
    var identifiers = []
    for (var requestId in pendingNavigation) identifiers.push(requestId)
    pendingNavigation = ({})
    breadcrumbTarget = ""
    breadcrumbRequestRunning = false
    for (var i = 0; i < identifiers.length; i++) finishBrokerRequest(identifiers[i])
  }

  function requestBrokerTermination() {
    if (brokerTerminationRequested || !brokerProcess.running) return
    brokerTerminationRequested = true
    brokerProcess.signal(15)
    brokerKillTimer.restart()
  }

  function failBroker(message) {
    brokerError = String(message || "Snapshot backend failed.").slice(0, 1024)
    brokerReady = false
    pendingScan = null
    pendingActivation = null
    scanRequestId = ""
    stagingGenerationId = ""
    scanCancellationRequested = false
    clearBrokerRequests()
    clearBrokerQueue()
    activeBackendAvailable = false
    searchOutstanding = 0
    searchRunning = false
    if (searchState) searchState.error = "Snapshot backend is unavailable."
    revealState = null
    snapshotState = activeGenerationId !== "" ? "backend-unavailable" : "failed"
    requestBrokerTermination()
  }

  function startBroker() {
    if (brokerProcess.running || brokerReady) return
    brokerExpectedStop = false
    brokerTerminationRequested = false
    brokerError = ""
    brokerStderr = ""
    brokerStderrBytes = 0
    clearBrokerQueue()
    clearBrokerRequests()
    brokerProcess.command = [brokerPath]
    brokerProcess.running = true
    brokerHandshakeTimer.restart()
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

  function appendBrokerStderr(raw) {
    var value = String(raw || "")
    var bytes = utf8Bytes(value) + 1
    if (FrontendSafety.outputLimitExceeded(
          bytes, brokerStderrBytes, maxDiagnosticBytes)) {
      brokerStderr = ""
      brokerStderrBytes = 0
      failBroker("Snapshot backend exceeded its diagnostic output limit.")
      return
    }
    brokerStderr += value + "\n"
    brokerStderrBytes += bytes
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
    startBroker()
    if (filesystemModel.count === 0) launchDiscovery()
    Qt.callLater(function() { if (opened) keyCatcher.forceActiveFocus() })
  }

  function close() {
    opened = false
    pendingScan = null
    cancelActiveScan()
    if (brokerProcess.running) {
      brokerExpectedStop = true
      if (brokerReady)
        sendBrokerRequest("shutdown", {}, 2000, "", "")
      else requestBrokerTermination()
    }
  }

  function dismiss() {
    if (shell && typeof shell.hide === "function") shell.hide(pluginId)
    else close()
  }

  function refresh() {
    if (!brokerReady) startBroker()
    preferredMountpoint = selectedFilesystem
      ? selectedFilesystem.mountpoint : preferredMountpoint
    treeGeneration++
    pendingScan = null
    cancelActiveScan()
    if (discoveryProcess.running) {
      discoveryRefreshPending = true
      requestDiscoveryTermination()
    } else launchDiscovery()
  }

  function clearTree() {
    treeRootPath = ""
    selectedTreePath = ""
    selectedTreeIndex = -1
    progressEntries = 0
    progressBytes = 0
    brokerError = ""
    treeRows.clear()
    breadcrumbModel.clear()
    activeRootMetadata = null
    browserState = null
    pendingNavigation = ({})
    breadcrumbTarget = ""
    breadcrumbRequestRunning = false
    breadcrumbDebounce.stop()
    cachedDirectoryRows = 0
    cachedPages = 0
    visibleRows = 0
    expansionStates = 0
    breadcrumbRows = 0
    metadataRows = 0
    searchQuery = ""
    searchRunning = false
    searchState = null
    searchRows = 0
    searchPages = 0
    searchOutstanding = 0
    searchSessionsRetained = 0
    revealState = null
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
    if (selectedFilesystemIndex === index && treeRootPath === mountpoint) {
      requestScan(mountpoint)
      return
    }
    treeGeneration++
    pendingScan = null
    cancelActiveScan()
    selectedFilesystemIndex = index
    preferredMountpoint = mountpoint
    treeRootPath = mountpoint
    requestScan(mountpoint)
  }

  function selectTreePath(path, reveal) {
    for (var i = 0; i < treeRows.count; i++) {
      if (treeRows.get(i).nodePath === path) {
        selectedTreePath = path
        if (browserState) browserState.selection = path
        selectedTreeIndex = i
        treeView.positionViewAtIndex(i, ListView.Contain)
        requestBreadcrumb(path)
        return
      }
    }
    if (activeRootMetadata && activeRootMetadata.path === path) {
      selectedTreePath = path
      browserState.selection = path
      selectedTreeIndex = -1
    }
    requestBreadcrumb(path)
  }

  function rebuildBreadcrumb() {
    breadcrumbModel.clear()
    if (!browserState) return
    for (var i = 0; i < browserState.breadcrumbs.length; i++) {
      var crumb = browserState.breadcrumbs[i]
      breadcrumbModel.append({ crumbName: crumb.name || "…", crumbPath: crumb.path || "" })
    }
    updateBrowserHighWater()
  }

  function scheduleSearch() {
    var query = searchQuery.trim()
    if (FrontendSafety.utf8Bytes(query, maxSearchQueryBytes + 1) > maxSearchQueryBytes) {
      clearSearchState(false)
      showNavigationError("Search query exceeds 1024 bytes.")
      return
    }
    if (query === "") { clearSearchState(true); return }
    // Invalidate the previous session immediately; debounce only delays the
    // replacement SQLite query, never stale-response rejection.
    clearSearchState(false)
    treeRows.clear()
    searchRunning = true
    searchDebounce.restart()
  }

  function chooseSearchResult(path) {
    if (!searchState || !activeBackendAvailable) return
    beginReveal(path)
  }

  function clearSearchState(showBrowser) {
    searchDebounce.stop()
    searchSerial++
    var staleRequests = []
    for (var requestId in brokerRequests)
      if (brokerRequests[requestId].operation === "search") staleRequests.push(requestId)
    for (var index = 0; index < staleRequests.length; index++)
      finishBrokerRequest(staleRequests[index])
    searchRequestId = ""
    revealState = null
    searchRunning = false
    searchOutstanding = 0
    if (searchState) SearchState.clear(searchState)
    searchState = null
    searchRows = 0
    searchPages = 0
    searchSessionsRetained = 0
    if (showBrowser && browserState) rebuildVisibleWindow(selectedTreePath)
  }

  function beginSearch() {
    var query = searchQuery.trim()
    if (!query || !activeBackendAvailable) return
    clearSearchState(false)
    searchSerial++
    var session = "search-" + String(searchSerial) + "-" + String(Date.now())
    searchState = SearchState.create(activeGenerationId, session, query,
      { pageRows: searchPageRows, maxRows: maxSearchResultRows, maxPages: maxSearchPages })
    searchState.loading = true
    searchRunning = true
    searchSessionsRetained = 1
    searchSessionsHighWater = Math.max(searchSessionsHighWater, 1)
    treeRows.clear()
    requestSearchPage("")
  }

  function requestSearchPage(continuation) {
    if (!searchState || searchOutstanding >= 1 || !activeBackendAvailable) return
    var requestId = sendBrokerRequest("search", {
      generationId: activeGenerationId, query: searchState.query,
      continuation: continuation || undefined
    }, 10000, activeGenerationId, searchState.sessionId)
    if (!requestId) return
    searchRequestId = requestId
    searchOutstanding = 1
    searchOutstandingHighWater = Math.max(searchOutstandingHighWater, 1)
    searchState.loading = true
  }

  function updateSearchMetrics() {
    if (!searchState) { searchRows = 0; searchPages = 0; searchSessionsRetained = 0; return }
    var stats = SearchState.stats(searchState)
    searchRows = stats.rows
    searchPages = stats.pages
    searchSessionsRetained = stats.sessions
    searchRowsHighWater = Math.max(searchRowsHighWater, searchRows)
    searchPagesHighWater = Math.max(searchPagesHighWater, searchPages)
    searchSessionsHighWater = Math.max(searchSessionsHighWater, searchSessionsRetained)
  }

  function renderSearchPage(preferredPath) {
    treeRows.clear()
    selectedTreeIndex = -1
    var page = SearchState.currentPage(searchState)
    if (!page) return
    for (var i = 0; i < page.rows.length; i++) {
      var row = page.rows[i]
      treeRows.append({ nodeName: row.name, nodePath: row.path,
        nodeBytes: row.allocated_bytes, formattedSize: TreeModel.formatBytes(row.allocated_bytes),
        depth: 0, expanded: false, loading: false, loaded: true,
        warningCount: row.warning_count,
        warningText: row.warning_count > 0 ? String(row.warning_count) + " paths could not be read" : "",
        errorText: "", hasChildren: row.child_count > 0, percentParent: 0,
        contextPath: row.path, searchResult: true })
      if (row.path === preferredPath) selectedTreeIndex = i
    }
    if (selectedTreeIndex < 0 && treeRows.count > 0) selectedTreeIndex = 0
    selectedTreePath = selectedTreeIndex >= 0 ? treeRows.get(selectedTreeIndex).nodePath : ""
    updateSearchMetrics()
  }

  function nextSearchPage() {
    if (!searchState || searchOutstanding) return
    if (SearchState.nextCached(searchState)) { renderSearchPage(""); return }
    var page = SearchState.currentPage(searchState)
    if (page && page.hasMore && page.continuation) requestSearchPage(page.continuation)
  }

  function previousSearchPage() {
    if (SearchState.previous(searchState)) renderSearchPage("")
    else showNavigationError("Earlier search results are no longer retained.")
  }

  function beginReveal(path) {
    if (!searchState || !path) return
    revealState = { generationId: activeGenerationId, path: path,
      sessionId: searchState.sessionId, ancestors: null, page: null }
    var requestId = sendBrokerRequest("ancestors", {
      generationId: activeGenerationId, path: path
    }, 3000, activeGenerationId, "reveal:" + searchState.sessionId)
    if (requestId) pendingNavigation[requestId] = {
      kind: "revealAncestors", path: path, rows: [], continuation: "",
      sessionId: searchState.sessionId
    }
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
    selectTreePath(path, false)
    if (!browserState || !activeBackendAvailable) return
    if (browserState.expansions[path]) {
      BrowserState.collapse(browserState, path)
      rebuildVisibleWindow(path)
      return
    }
    var key = BrowserState.pageKey(path, "")
    if (browserState.pages[key]) {
      if (!BrowserState.expand(browserState, path, key, currentAncestorPaths()))
        showNavigationError("Expansion-state limit is fully pinned.")
      rebuildVisibleWindow(path)
      return
    }
    requestChildrenPage(path, "", "", true)
  }

  function browserLimits() {
    return { pageRows: childPageRows, maxRows: maxCachedDirectoryRows,
      maxPages: maxCachedPages, maxVisible: maxVisibleRows,
      maxExpansions: maxExpansionStates, maxBreadcrumbs: maxBreadcrumbRows,
      maxMetadata: maxMetadataCacheRows }
  }

  function currentAncestorPaths() {
    var result = []
    if (!browserState) return result
    for (var i = 0; i < browserState.breadcrumbs.length; i++)
      if (browserState.breadcrumbs[i].path) result.push(browserState.breadcrumbs[i].path)
    return result
  }

  function updateBrowserHighWater() {
    if (!browserState) return
    var stats = BrowserState.stats(browserState)
    cachedDirectoryRows = stats.rows
    cachedPages = stats.pages
    visibleRows = stats.visible
    expansionStates = stats.expansions
    breadcrumbRows = stats.breadcrumbs
    metadataRows = stats.metadata
    cachedDirectoryRowsHighWater = Math.max(cachedDirectoryRowsHighWater, stats.rows)
    cachedPagesHighWater = Math.max(cachedPagesHighWater, stats.pages)
    visibleRowsHighWater = Math.max(visibleRowsHighWater, stats.visible)
    expansionStatesHighWater = Math.max(expansionStatesHighWater, stats.expansions)
    breadcrumbRowsHighWater = Math.max(breadcrumbRowsHighWater, stats.breadcrumbs)
    metadataRowsHighWater = Math.max(metadataRowsHighWater, stats.metadata)
  }

  function showNavigationError(message) {
    actionFeedback = String(message || "Navigation request could not be completed.").slice(0, 1024)
    feedbackTimer.restart()
  }

  function rowForPath(path) {
    return browserState ? browserState.rows[path] : null
  }

  function appendVisibleRow(row, depth) {
    var parent = row.parent_path ? rowForPath(row.parent_path) : null
    var parentBytes = parent ? parent.allocated_bytes : row.allocated_bytes
    treeRows.append({
      nodeName: row.name, nodePath: row.path, nodeBytes: row.allocated_bytes,
      formattedSize: TreeModel.formatBytes(row.allocated_bytes), depth: depth,
      expanded: !!browserState.expansions[row.path], loading: false, loaded: true,
      warningCount: row.warning_count,
      warningText: row.warning_count > 0 ? String(row.warning_count) + " paths could not be read" : "",
      errorText: "", hasChildren: row.child_count > 0,
      percentParent: parentBytes > 0
        ? TreeModel.clampPercent(row.allocated_bytes * 100 / parentBytes) : 0,
      contextPath: row.path, searchResult: false
    })
  }

  function rebuildVisibleWindow(preferredPath) {
    if (!browserState || !activeRootMetadata) return
    var visible = BrowserState.visible(browserState,
      browserViewRootPath || activeRootMetadata.path)
    treeRows.clear()
    selectedTreeIndex = -1
    for (var i = 0; i < visible.length; i++) {
      var item = visible[i], row = rowForPath(item.path)
      if (!row) continue
      appendVisibleRow(row, item.depth)
      if (row.path === preferredPath) selectedTreeIndex = treeRows.count - 1
    }
    if (selectedTreeIndex < 0 && treeRows.count > 0) selectedTreeIndex = 0
    if (selectedTreeIndex >= 0) selectedTreePath = treeRows.get(selectedTreeIndex).nodePath
    activeRowsHighWater = Math.max(activeRowsHighWater, treeRows.count)
    updateBrowserHighWater()
  }

  function requestChildrenPage(parentPath, continuation, previousPageKey, expandAfter) {
    if (!browserState || !activeBackendAvailable) return
    var requestId = sendBrokerRequest("children", {
      generationId: activeGenerationId, path: parentPath,
      continuation: continuation || undefined
    }, 3000, activeGenerationId, "")
    if (requestId !== "") pendingNavigation[requestId] = {
      kind: "children", parent: parentPath, continuation: continuation || "",
      previousPageKey: previousPageKey || "", expandAfter: !!expandAfter
    }
  }

  function nextChildPage(path) {
    if (!browserState || !browserState.expansions[path]) return
    var expansion = browserState.expansions[path]
    var page = browserState.pages[expansion.pageKey]
    if (!page || !page.hasMore || !page.nextContinuation) return
    var key = BrowserState.pageKey(path, page.nextContinuation)
    if (browserState.pages[key]) {
      BrowserState.setPage(browserState, path, key)
      rebuildVisibleWindow(path)
    } else requestChildrenPage(path, page.nextContinuation, page.key, false)
  }

  function previousChildPage(path) {
    if (!browserState || !browserState.expansions[path]) return
    var key = BrowserState.previousPage(browserState, path)
    if (!key) { showNavigationError("Earlier page is no longer cached; collapse and reopen to return to the first page."); return }
    BrowserState.setPage(browserState, path, key)
    rebuildVisibleWindow(path)
  }

  function requestBreadcrumb(path) {
    if (!browserState || !activeBackendAvailable || !path) return
    breadcrumbTarget = path
    breadcrumbDebounce.restart()
  }

  function launchBreadcrumbQuery() {
    if (breadcrumbRequestRunning || !breadcrumbTarget || !browserState
        || !activeBackendAvailable) return
    var path = breadcrumbTarget
    breadcrumbRequestRunning = true
    var requestId = sendBrokerRequest("ancestors", {
      generationId: activeGenerationId, path: path
    }, 3000, activeGenerationId, "")
    if (requestId !== "") pendingNavigation[requestId] = {
      kind: "ancestors", path: path, rows: [], continuation: ""
    }
    else breadcrumbRequestRunning = false
  }

  function retryNode(path) {
    requestScan(treeRootPath)
  }

  function requestScan(path) {
    var filesystem = selectedFilesystem
    if (!filesystem || path !== treeRootPath) return
    var request = { path: path, mountpoint: filesystem.mountpoint, generation: treeGeneration }
    if (!brokerReady) {
      pendingScan = request
      startBroker()
    } else if (scanRequestId !== "") {
      pendingScan = request
      cancelActiveScan()
    } else launchScan(request)
  }

  function launchScan(request) {
    if (!request || request.generation !== treeGeneration || !brokerReady) return
    brokerError = ""
    progressEntries = 0
    progressBytes = 0
    activeScanWarnings = []
    scanStartedAt = Date.now()
    scanElapsedSeconds = 0
    snapshotState = "scanning"
    scanCancellationRequested = false
    scanRequestId = sendBrokerRequest("scanStart", {
      path: request.path, mountpoint: request.mountpoint
    }, 60 * 60 * 1000 + 10000, "", "")
  }

  function cancelActiveScan() {
    if (scanRequestId === "" || stagingGenerationId === "" || scanCancellationRequested) return
    scanCancellationRequested = true
    sendBrokerRequest("scanCancel", {
      generationId: stagingGenerationId
    }, 3000, stagingGenerationId, "")
  }

  function nextBrokerRequestId() {
    requestSerial++
    return "qml-" + String(Date.now()) + "-" + String(requestSerial)
  }

  function sendBrokerRequest(operation, values, timeoutMs, generationId, activationToken) {
    if (!brokerProcess.running || (operation !== "hello" && !brokerReady)) return ""
    if (brokerRequestCount >= maxBrokerRequests) {
      showNavigationError("Too many snapshot requests are still pending.")
      return ""
    }
    var requestId = nextBrokerRequestId()
    var payload = { protocolVersion: brokerProtocolVersion,
      requestId: requestId, operation: operation }
    var source = values || {}
    for (var key in source) payload[key] = source[key]
    brokerRequests[requestId] = {
      operation: operation, generationId: generationId || "",
      activationToken: activationToken || "",
      deadline: Date.now() + Number(timeoutMs || 3000)
    }
    brokerRequestCount++
    brokerRequestsHighWater = Math.max(brokerRequestsHighWater, brokerRequestCount)
    updateGenerationHighWater()
    brokerRequestDeadline.start()
    brokerProcess.write(JSON.stringify(payload) + "\n")
    return requestId
  }

  function updateGenerationHighWater() {
    var identities = ({})
    if (activeGenerationId !== "") identities[activeGenerationId] = true
    if (stagingGenerationId !== "") identities[stagingGenerationId] = true
    if (pendingActivation) identities[pendingActivation.generationId] = true
    for (var requestId in brokerRequests) {
      var generation = brokerRequests[requestId].generationId
      if (generation !== "") identities[generation] = true
    }
    generationIdsHighWater = Math.max(generationIdsHighWater,
      Object.keys(identities).length)
  }

  function finishBrokerRequest(requestId) {
    if (!brokerRequests[requestId]) return
    delete brokerRequests[requestId]
    brokerRequestCount--
    if (brokerRequestCount <= 0) {
      brokerRequestCount = 0
      brokerRequestDeadline.stop()
    }
  }

  function enqueueBrokerLine(line) {
    var value = String(line || "")
    var bytes = FrontendSafety.utf8Bytes(value, maxBrokerLineBytes + 1) + 1
    var error = FrontendSafety.brokerQueueLimitError(
      bytes, brokerQueuedLineCount, brokerQueuedBytes,
      maxBrokerLineBytes, maxBrokerQueueLines, maxBrokerQueueBytes)
    if (error !== "") { failBroker(error); return }
    brokerLineQueue.push({ text: value, bytes: bytes })
    brokerQueuedLineCount++
    brokerQueuedBytes += bytes
    brokerQueueLinesHighWater = Math.max(brokerQueueLinesHighWater, brokerQueuedLineCount)
    brokerQueueBytesHighWater = Math.max(brokerQueueBytesHighWater, brokerQueuedBytes)
    if (!brokerDrainTimer.running) brokerDrainTimer.start()
  }

  function drainBrokerLines() {
    var count = Math.min(brokerLineQueue.length, 32)
    var batch = brokerLineQueue.splice(0, count)
    for (var i = 0; i < batch.length; i++) {
      brokerQueuedLineCount--
      brokerQueuedBytes -= batch[i].bytes
      handleBrokerLine(batch[i].text)
      if (brokerTerminationRequested) break
    }
    if (brokerLineQueue.length === 0) {
      brokerDrainTimer.stop()
      brokerQueuedLineCount = 0
      brokerQueuedBytes = 0
    }
  }

  function validIdentifier(value, maximum) {
    return typeof value === "string" && value !== ""
      && FrontendSafety.utf8Bytes(value, maximum + 1) <= maximum
      && /^[A-Za-z0-9._:-]+$/.test(value)
  }

  function validNumber(value) {
    return typeof value === "number" && isFinite(value)
      && value >= 0 && Math.floor(value) === value
  }

  function validateDirectoryRow(row) {
    return FrontendSafety.brokerDirectoryRowError(row, maxPathBytes) === ""
  }

  function validateQueryResult(message, expectedOperation) {
    if (!message.result || message.result.kind !== expectedOperation
        || !Array.isArray(message.result.rows)
        || message.result.rows.length > maxBrokerRows
        || typeof message.result.hasMore !== "boolean") return null
    var rows = []
    for (var i = 0; i < message.result.rows.length; i++) {
      var row = message.result.rows[i]
      if (!validateDirectoryRow(row)) return null
      rows.push(row)
    }
    return rows
  }

  function validContinuation(result) {
    return (!result.hasMore && result.continuation === null)
      || (result.hasMore && typeof result.continuation === "string"
          && FrontendSafety.utf8Bytes(result.continuation, 32769) <= 32768)
  }

  function handleBrokerLine(rawLine) {
    var message
    try { message = JSON.parse(String(rawLine || "")) }
    catch (error) { failBroker("Snapshot backend returned malformed JSON."); return }
    var envelopeError = FrontendSafety.brokerEnvelopeError(
      message, brokerProtocolVersion, maxRequestIdBytes, maxGenerationIdBytes)
    if (envelopeError !== "") {
      failBroker(envelopeError)
      return
    }
    if (message.type === "ready" && message.requestId === "startup") return
    if (!validIdentifier(message.requestId, maxRequestIdBytes)) {
      failBroker("Snapshot backend returned an invalid request identity.")
      return
    }
    var expected = brokerRequests[message.requestId]
    if (!expected) return

    if (message.type === "ready" && expected.operation === "hello") {
      finishBrokerRequest(message.requestId)
      brokerHandshakeTimer.stop()
      brokerReady = true
      var waiting = pendingScan
      pendingScan = null
      if (waiting) Qt.callLater(function() { launchScan(waiting) })
      return
    }
    if (message.type === "scanStarted" && expected.operation === "scanStart") {
      if (!validIdentifier(message.generationId, maxGenerationIdBytes)) {
        failBroker("Snapshot backend returned an invalid generation identity.")
        return
      }
      expected.generationId = message.generationId
      stagingGenerationId = message.generationId
      updateGenerationHighWater()
      if (pendingScan) Qt.callLater(function() { cancelActiveScan() })
      return
    }
    if (expected.operation === "scanStart") {
      if (message.generationId !== expected.generationId
          || message.requestId !== scanRequestId) return
      if (message.type === "scanProgress") {
        if (!validNumber(message.entries) || !validNumber(message.bytes)) {
          failBroker("Snapshot backend returned invalid progress data.")
          return
        }
        progressEntries = message.entries
        progressBytes = message.bytes
        return
      }
      if (message.type === "scanWarning") {
        if (activeScanWarnings.length < 100) {
          activeScanWarnings.push(String(message.error || "Scan warning").slice(0, 1024))
          warningRowsHighWater = Math.max(warningRowsHighWater, activeScanWarnings.length)
        }
        return
      }
      if (message.type === "scanFailed" || message.type === "scanCancelled") {
        finishBrokerRequest(message.requestId)
        scanRequestId = ""
        stagingGenerationId = ""
        scanCancellationRequested = false
        snapshotState = activeGenerationId !== ""
          ? (activeBackendAvailable ? "complete" : "backend-unavailable") : "failed"
        if (message.type === "scanFailed") brokerError = String(message.error || "Scan failed.").slice(0, 1024)
        launchPendingScan()
        return
      }
      if (message.type === "snapshotActivated") {
        if (!validNumber(message.bytes) || !validNumber(message.directFilesBytes)
            || !validNumber(message.entries) || !validNumber(message.directoryCount)
            || !validNumber(message.warningCount) || !validNumber(message.durationMs)
            || typeof message.path !== "string"
            || FrontendSafety.utf8Bytes(message.path, maxPathBytes + 1) > maxPathBytes) {
          failBroker("Snapshot activation metadata is invalid.")
          return
        }
        if (scanCancellationRequested) {
          finishBrokerRequest(message.requestId)
          scanRequestId = ""
          stagingGenerationId = ""
          scanCancellationRequested = false
          sendBrokerRequest("activationAbort", {
            generationId: message.generationId
          }, 3000, message.generationId, "cancelled-activation")
          snapshotState = activeGenerationId !== ""
            ? (activeBackendAvailable ? "complete" : "backend-unavailable") : "failed"
          launchPendingScan()
          return
        }
        finishBrokerRequest(message.requestId)
        scanRequestId = ""
        stagingGenerationId = ""
        var token = nextBrokerRequestId()
        pendingActivation = { token: token, generationId: message.generationId,
          path: message.path, summary: message, metadata: null, children: null }
        updateGenerationHighWater()
        sendBrokerRequest("metadata", { generationId: message.generationId,
          path: message.path }, 3000, message.generationId, token)
        sendBrokerRequest("children", { generationId: message.generationId,
          path: message.path }, 3000, message.generationId, token)
        return
      }
    }
    if (message.type === "queryResult" && expected.operation === "search") {
      finishBrokerRequest(message.requestId)
      if (!searchState || message.requestId !== searchRequestId
          || message.generationId !== activeGenerationId
          || expected.activationToken !== searchState.sessionId
          || message.operation !== "search") return
      searchOutstanding = 0
      searchRequestId = ""
      var searchResultRows = validateQueryResult(message, "search")
      if (searchResultRows === null || !validContinuation(message.result)) {
        searchState.loading = false
        searchState.error = "Search response was invalid."
        searchRunning = false
        return
      }
      var searchError = SearchState.putPage(searchState,
        searchState.pages.length > 0
          ? SearchState.currentPage(searchState).continuation : "",
        searchResultRows, message.result.hasMore, message.result.continuation,
        validateDirectoryRow)
      if (searchError !== "") {
        searchState.error = searchError
        searchRunning = false
        return
      }
      searchRunning = false
      renderSearchPage("")
      return
    }
    if (message.type === "queryResult" && pendingNavigation[message.requestId]) {
      var navigation = pendingNavigation[message.requestId]
      if (message.generationId !== activeGenerationId
          || message.generationId !== expected.generationId
          || message.operation !== expected.operation) return
      var navigationRows = validateQueryResult(message, expected.operation)
      if (navigationRows === null || !validContinuation(message.result)) {
        delete pendingNavigation[message.requestId]
        finishBrokerRequest(message.requestId)
        showNavigationError("Snapshot navigation response was invalid.")
        return
      }
      delete pendingNavigation[message.requestId]
      finishBrokerRequest(message.requestId)
      if (navigation.kind === "children") {
        var pageResult = BrowserState.putPage(browserState, navigation.parent,
          navigation.continuation, navigationRows, message.result.hasMore,
          message.result.continuation, navigation.previousPageKey, validateDirectoryRow)
        if (pageResult.error !== "") { showNavigationError(pageResult.error); return }
        if (navigation.expandAfter
            && !BrowserState.expand(browserState, navigation.parent, pageResult.key,
                                    currentAncestorPaths())) {
          showNavigationError("Expansion-state limit is fully pinned.")
          return
        }
        BrowserState.setPage(browserState, navigation.parent, pageResult.key)
        rebuildVisibleWindow(navigation.parent)
        updateBrowserHighWater()
        return
      }
      if (navigation.kind === "revealAncestors") {
        if (!revealState || revealState.sessionId !== navigation.sessionId
            || revealState.generationId !== activeGenerationId) return
        var revealCombined = navigation.rows.concat(navigationRows)
        if (revealCombined.length > maxBreadcrumbRows)
          revealCombined = revealCombined.slice(0, maxBreadcrumbRows)
        if (message.result.hasMore && revealCombined.length < maxBreadcrumbRows) {
          var revealMore = sendBrokerRequest("ancestors", {
            generationId: activeGenerationId, path: navigation.path,
            continuation: message.result.continuation
          }, 3000, activeGenerationId, "reveal:" + navigation.sessionId)
          if (revealMore) pendingNavigation[revealMore] = {
            kind: "revealAncestors", path: navigation.path, rows: revealCombined,
            continuation: message.result.continuation, sessionId: navigation.sessionId
          }
          return
        }
        var revealError = BrowserState.insertRows(browserState, revealCombined, validateDirectoryRow)
        if (revealError !== "") { showNavigationError(revealError); revealState = null; return }
        BrowserState.setBreadcrumbs(browserState, revealCombined, message.result.hasMore)
        revealState.chain = revealCombined.slice().reverse()
        revealState.index = 1
        browserViewRootPath = revealState.chain[0].path
        continueReveal()
        return
      }
      if (navigation.kind === "revealPage") {
        if (!revealState || revealState.sessionId !== navigation.sessionId
            || revealState.generationId !== activeGenerationId) return
        if (navigationRows.length === 0 || navigationRows[0].path !== navigation.child) {
          showNavigationError("Snapshot could not reveal the selected result.")
          revealState = null
          return
        }
        var revealPage = BrowserState.putPage(browserState, navigation.parent,
          "reveal:" + navigation.child, navigationRows, message.result.hasMore,
          message.result.continuation, "", validateDirectoryRow)
        if (revealPage.error !== ""
            || !BrowserState.expand(browserState, navigation.parent, revealPage.key,
                                    revealState.chain.slice(0, revealState.index))) {
          showNavigationError(revealPage.error || "Reveal exceeded bounded navigation state.")
          revealState = null
          return
        }
        revealState.index++
        continueReveal()
        return
      }
      if (navigation.kind === "ancestors") {
        if (navigation.path !== selectedTreePath) {
          breadcrumbRequestRunning = false
          breadcrumbDebounce.restart()
          return
        }
        var combined = navigation.rows.concat(navigationRows)
        if (combined.length > maxBreadcrumbRows) combined = combined.slice(0, maxBreadcrumbRows)
        var rowError = BrowserState.insertRows(browserState, navigationRows, validateDirectoryRow)
        if (rowError !== "") { showNavigationError(rowError); return }
        if (message.result.hasMore && combined.length < maxBreadcrumbRows) {
          var ancestorRequest = sendBrokerRequest("ancestors", {
            generationId: activeGenerationId, path: navigation.path,
            continuation: message.result.continuation
          }, 3000, activeGenerationId, "")
          if (ancestorRequest !== "") pendingNavigation[ancestorRequest] = {
            kind: "ancestors", path: navigation.path, rows: combined,
            continuation: message.result.continuation
          }
          else breadcrumbRequestRunning = false
          return
        }
        BrowserState.setBreadcrumbs(browserState, combined, message.result.hasMore)
        breadcrumbRequestRunning = false
        rebuildBreadcrumb()
        if (breadcrumbTarget !== navigation.path) breadcrumbDebounce.restart()
        return
      }
    }
    if (message.type === "queryResult"
        && (expected.operation === "metadata" || expected.operation === "children")) {
      if (message.generationId !== expected.generationId || !pendingActivation
          || pendingActivation.token !== expected.activationToken
          || pendingActivation.generationId !== message.generationId
          || message.operation !== expected.operation) return
      var rows = validateQueryResult(message, expected.operation)
      if (rows === null || (expected.operation === "metadata" && rows.length !== 1)) {
        abortPendingActivation("Snapshot initial view was invalid.")
        return
      }
      if (expected.operation === "metadata") pendingActivation.metadata = rows[0]
      else {
        if (!validContinuation(message.result)) {
          abortPendingActivation("Snapshot initial continuation was invalid.")
          return
        }
        pendingActivation.children = rows
        pendingActivation.childrenHasMore = message.result.hasMore
        pendingActivation.childrenContinuation = message.result.continuation
      }
      activationRowsHighWater = Math.max(activationRowsHighWater,
        (pendingActivation.metadata ? 1 : 0)
          + (pendingActivation.children ? pendingActivation.children.length : 0))
      finishBrokerRequest(message.requestId)
      if (pendingActivation.metadata && pendingActivation.children)
        requestActivationCommit()
      return
    }
    if (message.type === "activationCommitted" && expected.operation === "activationCommit") {
      if (!pendingActivation || message.generationId !== pendingActivation.generationId
          || expected.activationToken !== pendingActivation.token) return
      finishBrokerRequest(message.requestId)
      commitInitialView()
      return
    }
    if (message.type === "activationAborted" && expected.operation === "activationAbort") {
      finishBrokerRequest(message.requestId)
      return
    }
    if (message.type === "scanCancelRequested" && expected.operation === "scanCancel") {
      finishBrokerRequest(message.requestId)
      return
    }
    if (message.type === "error") {
      if (pendingNavigation[message.requestId]) {
        if (pendingNavigation[message.requestId].kind === "ancestors")
          breadcrumbRequestRunning = false
        if (pendingNavigation[message.requestId].kind.indexOf("reveal") === 0)
          revealState = null
        delete pendingNavigation[message.requestId]
        finishBrokerRequest(message.requestId)
        showNavigationError(String(message.error || "Snapshot navigation failed."))
        return
      }
      if (expected.operation === "search") {
        finishBrokerRequest(message.requestId)
        if (message.requestId === searchRequestId && searchState) {
          searchOutstanding = 0
          searchRequestId = ""
          searchRunning = false
          searchState.loading = false
          searchState.error = String(message.error || "Search failed.").slice(0, 1024)
        }
        return
      }
      finishBrokerRequest(message.requestId)
      if (expected.operation === "metadata" || expected.operation === "children")
        abortPendingActivation(String(message.error || "Initial snapshot query failed."))
      else if (expected.operation === "hello" || expected.operation === "activationCommit")
        failBroker(String(message.error || "Snapshot backend request failed."))
      return
    }
  }

  function requestActivationCommit() {
    if (!pendingActivation) return
    sendBrokerRequest("activationCommit", {
      generationId: pendingActivation.generationId
    }, 3000, pendingActivation.generationId, pendingActivation.token)
  }

  function continueReveal() {
    if (!revealState) return
    if (revealState.index >= revealState.chain.length) {
      var selected = revealState.path
      revealState = null
      clearSearchState(false)
      searchField.text = ""
      searchQuery = ""
      selectedTreePath = selected
      browserState.selection = selected
      rebuildVisibleWindow(selected)
      rebuildBreadcrumb()
      return
    }
    var child = revealState.chain[revealState.index]
    var parent = revealState.chain[revealState.index - 1]
    var requestId = sendBrokerRequest("childrenAt", {
      generationId: activeGenerationId, path: child.path
    }, 3000, activeGenerationId, "reveal:" + revealState.sessionId)
    if (requestId) pendingNavigation[requestId] = {
      kind: "revealPage", child: child.path, parent: parent.path,
      sessionId: revealState.sessionId
    }
  }

  function abortPendingActivation(message) {
    var failed = pendingActivation
    if (!failed) return
    pendingActivation = null
    var related = []
    for (var requestId in brokerRequests) {
      if (brokerRequests[requestId].activationToken === failed.token)
        related.push(requestId)
    }
    for (var index = 0; index < related.length; index++)
      finishBrokerRequest(related[index])
    brokerError = String(message || "Snapshot activation failed.").slice(0, 1024)
    sendBrokerRequest("activationAbort", {
      generationId: failed.generationId
    }, 3000, failed.generationId, failed.token)
    snapshotState = activeGenerationId !== ""
      ? (activeBackendAvailable ? "complete" : "backend-unavailable") : "failed"
    launchPendingScan()
  }

  function commitInitialView() {
    var activation = pendingActivation
    if (!activation) return
    var metadata = activation.metadata
    var children = activation.children
    var nextState = BrowserState.create(browserLimits(), activation.generationId)
    var metadataError = BrowserState.putMetadata(nextState, metadata, validateDirectoryRow)
    var pageResult = BrowserState.putPage(nextState, metadata.path, "", children,
      !!activation.childrenHasMore, activation.childrenContinuation, "", validateDirectoryRow)
    if (metadataError !== "" || pageResult.error !== ""
        || !BrowserState.expand(nextState, metadata.path, pageResult.key, [metadata.path])) {
      failBroker("Committed snapshot could not enter the bounded frontend cache.")
      return
    }
    nextState.selection = children.length > 0 ? children[0].path : metadata.path
    BrowserState.setBreadcrumbs(nextState, [metadata], false)
    // This assignment is the frontend generation swap. No A cache object is
    // copied into B, and no normal property retains the old state afterward.
    clearNavigationRequests()
    clearSearchState(false)
    revealState = null
    browserState = nextState
    browserViewRootPath = metadata.path
    activeRootMetadata = metadata
    activeGenerationId = activation.generationId
    activeBackendAvailable = true
    updateGenerationHighWater()
    treeRootPath = metadata.path
    snapshotDirectoryCount = activation.summary.directoryCount
    snapshotDurationMs = activation.summary.durationMs
    snapshotState = "complete"
    selectedTreePath = nextState.selection
    pendingActivation = null
    progressEntries = 0
    progressBytes = 0
    rebuildVisibleWindow(selectedTreePath)
    rebuildBreadcrumb()
    launchPendingScan()
  }

  function launchPendingScan() {
    var next = pendingScan
    pendingScan = null
    if (next && next.generation === treeGeneration)
      Qt.callLater(function() { launchScan(next) })
  }

  function moveTreeSelection(delta) {
    if (treeRows.count === 0) return
    var next = Math.max(0, Math.min(treeRows.count - 1, selectedTreeIndex + delta))
    selectedTreeIndex = next
    selectedTreePath = treeRows.get(next).nodePath
    if (browserState) browserState.selection = selectedTreePath
    treeView.positionViewAtIndex(next, ListView.Contain)
    rebuildBreadcrumb()
  }

  function expandSelected() {
    if (selectedTreePath !== "") toggleNode(selectedTreePath)
  }

  function collapseSelected() {
    if (!browserState || !selectedTreePath) return
    if (browserState.expansions[selectedTreePath]) {
      BrowserState.collapse(browserState, selectedTreePath)
      rebuildVisibleWindow(selectedTreePath)
      return
    }
    var row = rowForPath(selectedTreePath)
    if (row && row.parent_path) selectTreePath(row.parent_path, false)
  }

  function pageCurrentParent(forward) {
    if (!browserState || !selectedTreePath) return
    var row = rowForPath(selectedTreePath)
    var parent = row && row.parent_path ? row.parent_path : selectedTreePath
    if (forward) nextChildPage(parent)
    else previousChildPage(parent)
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
    id: brokerProcess
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.enqueueBrokerLine(line) } }
    stderr: SplitParser { onRead: function(line) { root.appendBrokerStderr(line) } }
    onStarted: {
      root.sendBrokerRequest("hello", {}, 15000, "", "")
    }
    onExited: function(exitCode) {
      brokerHandshakeTimer.stop()
      brokerKillTimer.stop()
      brokerTerminationRequested = false
      brokerReady = false
      activeBackendAvailable = false
      clearBrokerQueue()
      clearBrokerRequests()
      scanRequestId = ""
      stagingGenerationId = ""
      pendingActivation = null
      if (!brokerExpectedStop) {
        brokerError = "Snapshot backend stopped unexpectedly. Refresh to start a new scan."
        snapshotState = activeGenerationId !== "" ? "backend-unavailable" : "failed"
      }
      brokerExpectedStop = false
    }
  }

  Timer { id: brokerDrainTimer; interval: 1; repeat: true; onTriggered: root.drainBrokerLines() }
  Timer { id: breadcrumbDebounce; interval: 35; onTriggered: root.launchBreadcrumbQuery() }
  Timer { id: searchDebounce; interval: 220; onTriggered: root.beginSearch() }
  Timer {
    id: brokerHandshakeTimer
    interval: 15000
    onTriggered: root.failBroker("Snapshot backend did not complete its startup handshake.")
  }
  Timer {
    id: brokerKillTimer
    interval: 2000
    onTriggered: {
      if (brokerProcess.running && root.brokerTerminationRequested)
        brokerProcess.signal(9)
    }
  }
  Timer {
    id: brokerRequestDeadline
    interval: 250
    repeat: true
    onTriggered: {
      var now = Date.now()
      for (var requestId in root.brokerRequests) {
        if (root.brokerRequests[requestId].deadline <= now) {
          root.failBroker("Snapshot backend request timed out.")
          return
        }
      }
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
          else if (event.key === Qt.Key_Right || event.key === Qt.Key_L || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) root.expandSelected()
          else if (event.key === Qt.Key_Left || event.key === Qt.Key_H || event.key === Qt.Key_Backspace) root.collapseSelected()
          else if (event.key === Qt.Key_Home) root.moveTreeSelection(-root.treeRows.count)
          else if (event.key === Qt.Key_End) root.moveTreeSelection(root.treeRows.count)
          else if (event.key === Qt.Key_PageDown) root.moveTreeSelection(Math.max(1, Math.floor(treeView.height / Style.space(34))))
          else if (event.key === Qt.Key_PageUp) root.moveTreeSelection(-Math.max(1, Math.floor(treeView.height / Style.space(34))))
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
                Text { Layout.fillWidth: true; text: "Scanning " + root.treeRootPath + "…"; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideMiddle }
                Text { text: root.progressEntries.toLocaleString() + " entries · " + TreeModel.formatBytes(root.progressBytes) + " processed · " + root.scanElapsedSeconds.toFixed(1) + " s"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
              }
            }
            Text {
              Layout.fillWidth: true
              visible: root.brokerError !== ""
              text: root.brokerError
              color: Color.urgent
              font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
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
                placeholderText: "Search directories"
                foreground: Color.popups.text; accent: Color.accent
                enabled: root.activeBackendAvailable
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
                    Button { anchors.verticalCenter: parent.verticalCenter; text: crumbName; foreground: Color.popups.text; enabled: crumbPath !== ""; onClicked: root.selectTreePath(crumbPath, true) }
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
                RowLayout {
                  Layout.fillWidth: true; Layout.preferredHeight: Style.space(28)
                  Button { text: "Previous page"; foreground: Color.popups.text; enabled: root.activeBackendAvailable; onClicked: root.searchState ? root.previousSearchPage() : root.pageCurrentParent(false) }
                  Button { text: "Next page"; foreground: Color.popups.text; enabled: root.activeBackendAvailable; onClicked: root.searchState ? root.nextSearchPage() : root.pageCurrentParent(true) }
                  Item { Layout.fillWidth: true }
                  Text { text: "Up/Down · Enter expand · Left parent"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall }
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
                  text: root.discoveryError !== "" ? root.discoveryError
                    : root.searchRunning ? "Searching snapshot…"
                    : root.searchState && root.searchState.error !== "" ? root.searchState.error
                    : root.searchQuery.trim() !== "" ? "No matching directories" : "No directory data"
                  color: root.discoveryError !== "" ? Color.urgent : Color.muted
                  font.family: Style.font.family; font.pixelSize: Style.font.body
                }
              }
              Text { Layout.fillWidth: true; visible: false; text: ""; color: Color.muted }
              }
            }
          }
        }
      }
    }
  }
}
