import QtQuick
import Quickshell
import Quickshell.Io
import "BrowserState.js" as BrowserState

ShellRoot {
  id: root
  property string repositoryPath: Quickshell.env("OMATREE_TEST_REPOSITORY")
  property string scanPath: Quickshell.env("OMATREE_TEST_SCAN_PATH")
  property int serial: 0
  property string phase: "startup"
  property string scanRequest: ""
  property string generation: ""
  property string metadataRequest: ""
  property string childrenRequest: ""
  property string commitRequest: ""
  property bool metadataReady: false
  property bool childrenReady: false
  property int activationCount: 0
  property int maximumRows: 0
  property bool cancellationObserved: false
  property int processStarts: 0
  property var browserState: null
  property var metadataRow: null
  property var childRows: []
  property string nextContinuation: ""
  property string navigationRequest: ""
  property string previousPageKey: ""
  property int navigationPages: 0
  property string nestedRequest: ""
  property string ancestorRequest: ""
  property string reloadRequest: ""
  property int scanStartedCount: 0
  property double browseStarted: 0
  property double browseElapsed: 0
  property string staleSearchRequest: ""
  property string searchRequest: ""
  property string revealRequest: ""
  property bool revealReady: false
  property bool searchAncestorReady: false

  function startSearchSupersession() {
    phase = "search"
    staleSearchRequest = send("search", { generationId: generation, query: "directory" })
    searchRequest = send("search", { generationId: generation, query: "nested-00" })
  }

  function validRow(row) { return row && typeof row.path === "string" && row.path !== "" }

  function requestNextPage() {
    navigationRequest = send("children", { generationId: generation,
      path: scanPath, continuation: nextContinuation })
  }

  function finishBrowse() {
    reloadRequest = send("children", { generationId: generation, path: scanPath })
  }

  function send(operation, values) {
    serial++
    var payload = { protocolVersion: 1, requestId: "qml-" + serial,
      operation: operation }
    var source = values || {}
    for (var key in source) payload[key] = source[key]
    broker.write(JSON.stringify(payload) + "\n")
    return payload.requestId
  }

  function startScan() {
    scanRequest = send("scanStart", { path: scanPath, mountpoint: scanPath })
  }

  function handle(line) {
    var message = JSON.parse(String(line || ""))
    if (message.protocolVersion !== 1) return
    if (message.type === "ready" && message.requestId === "startup") return
    if (message.type === "ready" && phase === "startup") {
      phase = "first"
      startScan()
    } else if (message.type === "scanStarted" && message.requestId === scanRequest) {
      scanStartedCount++
      generation = message.generationId
      if (phase === "cancel")
        send("scanCancel", { generationId: generation })
    } else if (message.type === "snapshotActivated"
               && message.requestId === scanRequest) {
      if (phase === "cancel") {
        send("activationAbort", { generationId: message.generationId })
        cancellationObserved = true
        send("shutdown", {})
        return
      }
      generation = message.generationId
      metadataReady = false
      childrenReady = false
      metadataRequest = send("metadata", {
        generationId: generation, path: scanPath
      })
      childrenRequest = send("children", {
        generationId: generation, path: scanPath
      })
    } else if (message.type === "queryResult") {
      if (!message.result || !Array.isArray(message.result.rows)
          || message.result.rows.length > 64) return
      maximumRows = Math.max(maximumRows, message.result.rows.length)
      if (message.requestId === navigationRequest && phase === "browse") {
        var result = BrowserState.putPage(browserState, scanPath,
          nextContinuation, message.result.rows, message.result.hasMore,
          message.result.continuation, previousPageKey, validRow)
        if (result.error !== "") return
        BrowserState.setPage(browserState, scanPath, result.key)
        previousPageKey = result.key
        nextContinuation = message.result.continuation || ""
        navigationPages++
        if (message.result.hasMore && navigationPages < 30) requestNextPage()
        else finishBrowse()
        return
      }
      if (message.requestId === nestedRequest && phase === "browse") {
        if (message.result.rows.length !== 10) return
        ancestorRequest = send("ancestors", { generationId: generation,
          path: scanPath + "/directory-0000/nested-00" })
        return
      }
      if (message.requestId === reloadRequest && phase === "browse") {
        var reloaded = BrowserState.putPage(browserState, scanPath, "",
          message.result.rows, message.result.hasMore, message.result.continuation,
          "", validRow)
        if (reloaded.error !== "") return
        nestedRequest = send("children", { generationId: generation,
          path: scanPath + "/directory-0000" })
        return
      }
      if (message.requestId === ancestorRequest && phase === "browse") {
        if (message.result.rows.length < 3) return
        browseElapsed = Date.now() - browseStarted
        startSearchSupersession()
        return
      }
      if (message.requestId === searchRequest && phase === "search") {
        if (message.result.rows.length !== 1) return
        var found = message.result.rows[0].path
        revealRequest = send("childrenAt", { generationId: generation, path: found })
        ancestorRequest = send("ancestors", { generationId: generation, path: found })
        return
      }
      if (message.requestId === revealRequest && phase === "search") {
        revealReady = message.result.rows.length > 0
        if (revealReady && searchAncestorReady) { phase = "replacement"; startScan() }
        return
      }
      if (message.requestId === ancestorRequest && phase === "search") {
        searchAncestorReady = message.result.rows.length >= 3
        if (revealReady && searchAncestorReady) { phase = "replacement"; startScan() }
        return
      }
      if (message.requestId === metadataRequest) {
        metadataReady = message.result.rows.length === 1
        metadataRow = metadataReady ? message.result.rows[0] : null
      }
      if (message.requestId === childrenRequest) {
        childrenReady = true
        childRows = message.result.rows
        nextContinuation = message.result.continuation || ""
      }
      if (metadataReady && childrenReady && commitRequest === "")
        commitRequest = send("activationCommit", { generationId: generation })
    } else if (message.type === "activationCommitted"
               && message.requestId === commitRequest) {
      activationCount++
      commitRequest = ""
      if (phase === "first") {
        browserState = BrowserState.create({ pageRows: 64, maxRows: 2048,
          maxPages: 24, maxVisible: 512, maxExpansions: 256,
          maxBreadcrumbs: 128, maxMetadata: 256 }, generation)
        BrowserState.putMetadata(browserState, metadataRow, validRow)
        var initial = BrowserState.putPage(browserState, scanPath, "", childRows,
          nextContinuation !== "", nextContinuation || null, "", validRow)
        BrowserState.expand(browserState, scanPath, initial.key, [scanPath])
        previousPageKey = initial.key
        phase = "browse"
        browseStarted = Date.now()
        if (nextContinuation !== "") requestNextPage()
        else finishBrowse()
      } else if (phase === "replacement") {
        phase = "cancel"
        startScan()
      }
    } else if (message.type === "scanCancelled" && phase === "cancel") {
      cancellationObserved = true
      send("shutdown", {})
    } else if (message.type === "shutdownComplete") {
      if (activationCount === 2 && cancellationObserved
          && maximumRows <= 64 && processStarts === 1 && scanStartedCount === 3)
        console.log("OMATREE_BROKER_PIPELINE_PASS activations=" + activationCount
                    + " maxRows=" + maximumRows + " browseMs=" + browseElapsed)
      else
        console.error("OMATREE_BROKER_PIPELINE_FAIL")
      Qt.quit()
    }
  }

  Process {
    id: broker
    command: [root.repositoryPath + "/bin/omatree-broker"]
    running: true
    stdinEnabled: true
    onStarted: {
      root.processStarts++
      root.send("hello", {})
    }
    stdout: SplitParser { onRead: function(line) { root.handle(line) } }
    stderr: SplitParser {
      onRead: function(line) { console.error("OMATREE_BROKER_STDERR " + line) }
    }
  }
}
