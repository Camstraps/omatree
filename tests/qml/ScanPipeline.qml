import QtQuick
import Quickshell
import Quickshell.Io
import "TreeModel.js" as TreeModel

ShellRoot {
  id: root

  property string requestId: "qml-pipeline-regression"
  property string repositoryPath: Quickshell.env("OMATREE_TEST_REPOSITORY")
  property string helperPath: repositoryPath + "/helper/discover.py"
  property string requestedScanPath: Quickshell.env("OMATREE_TEST_SCAN_PATH")
  property string requestedMountpoint: Quickshell.env("OMATREE_TEST_MOUNTPOINT")
  property string scanPath: requestedScanPath || repositoryPath
  property string mountpoint: requestedMountpoint || scanPath
  property var queue: []
  property int queueIndex: 0
  property var pendingCache: ({})
  property var pendingChildren: ({})
  property int directoryCount: 0
  property var complete: null
  property bool processExited: false
  property int exitCode: -1
  property int processStarts: 0
  property var cache: ({})
  property bool postorderFixturePassed: false
  property bool uxModelFixturePassed: false

  ListModel { id: visibleRows }

  function rebuildRows(tree, rootPath) {
    var visible = TreeModel.visibleNodes(tree, rootPath)
    visibleRows.clear()
    for (var i = 0; i < visible.length; i++) {
      var node = visible[i]
      visibleRows.append({
        nodeName: node.name, nodePath: node.path, nodeBytes: node.bytes,
        formattedSize: node.formattedSize, depth: node.depth,
        expanded: node.expanded, loading: node.loading, loaded: node.loaded,
        warningCount: node.warningCount, warningText: node.warningText,
        errorText: node.error
      })
    }
  }

  function verifyPostorderFixture() {
    var staged = ({})
    var unresolved = ({})
    var records = [
      { path: "/root/a/a1", parentPath: "/root/a", name: "a1", bytes: 10,
        directFilesBytes: 10, childDirectoryCount: 0, warningCount: 0 },
      { path: "/root/a", parentPath: "/root", name: "a", bytes: 20,
        directFilesBytes: 10, childDirectoryCount: 1, warningCount: 0 },
      { path: "/root/b/b1", parentPath: "/root/b", name: "b1", bytes: 30,
        directFilesBytes: 30, childDirectoryCount: 0, warningCount: 0 },
      { path: "/root/b", parentPath: "/root", name: "b", bytes: 40,
        directFilesBytes: 10, childDirectoryCount: 1, warningCount: 0 },
      { path: "/root", parentPath: null, name: "root", bytes: 60,
        directFilesBytes: 0, childDirectoryCount: 2, warningCount: 0 }
    ]
    for (var i = 0; i < records.length; i++) {
      if (TreeModel.stageDirectory(staged, unresolved, records[i]) !== "") return
    }
    var built = TreeModel.finalizeTree(staged, unresolved, records.length, "/root", "root")
    if (built.error !== "") return
    var visible = TreeModel.visibleNodes(built.cache, "/root")
    postorderFixturePassed = built.root.children.length === 2
      && built.cache["/root/a"].children.length === 1
      && built.cache["/root/b"].children.length === 1
      && visible.length === 3

    var cache = built.cache
    var zero = TreeModel.createNode("zero", "/zero", 0, 0, 0)
    zero.loaded = true
    cache["/zero"] = zero
    var percentOk = TreeModel.percentageOfParent(cache, "/root/a", 100) === (20 * 100 / 60)
      && TreeModel.percentageOfParent(cache, "/root", 100) === 100
      && TreeModel.percentageOfParent(cache, "/zero", 0) === 0
      && TreeModel.clampPercent(140) === 100 && TreeModel.clampPercent(-4) === 0
    TreeModel.revealPath(cache, "/root/a/a1")
    var breadcrumb = TreeModel.ancestorPaths(cache, "/root/a/a1")
    var search = TreeModel.searchMatches(cache, "A", 20)
    var searchSorted = search.matches.length === 2
      && search.matches[0].path === "/root/a" && search.matches[1].path === "/root/a/a1"
    uxModelFixturePassed = percentOk && cache["/root/a"].expanded
      && breadcrumb.join(",") === "/root,/root/a,/root/a/a1" && searchSorted
    var heap = []
    TreeModel.offerSearchMatch(heap, cache["/root/a/a1"], 1)
    TreeModel.offerSearchMatch(heap, cache["/root/b"], 1)
    uxModelFixturePassed = uxModelFixturePassed
      && TreeModel.sortedSearchHeap(heap).length === 1
      && TreeModel.sortedSearchHeap(heap)[0].path === "/root/b"

    var large = ({})
    var largeRoot = TreeModel.createNode("large", "/large", 10000, 0, 0)
    largeRoot.loaded = true; largeRoot.expanded = true; largeRoot.children = []
    large[largeRoot.path] = largeRoot
    for (var branchIndex = 0; branchIndex < 100; branchIndex++) {
      var branchPath = "/large/b" + branchIndex
      var branch = TreeModel.createNode("b" + branchIndex, branchPath, 100, 1, 0)
      branch.loaded = true; branch.parentPath = "/large"; branch.children = []
      large[branchPath] = branch; largeRoot.children.push(branchPath)
      for (var leafIndex = 0; leafIndex < 100; leafIndex++) {
        var leafPath = branchPath + "/l" + leafIndex
        var leaf = TreeModel.createNode("l" + leafIndex, leafPath, 1, 2, 1)
        leaf.loaded = true; leaf.parentPath = branchPath
        large[leafPath] = leaf; branch.children.push(leafPath)
      }
    }
    uxModelFixturePassed = uxModelFixturePassed
      && Object.keys(large).length === 10101
      && TreeModel.visibleNodes(large, "/large").length === 101
  }

  Component.onCompleted: verifyPostorderFixture()

  function enqueue(line) {
    queue.push(String(line || ""))
    if (!drainTimer.running) drainTimer.start()
  }

  function drainLines() {
    var end = Math.min(queue.length, queueIndex + 100)
    while (queueIndex < end) {
      var message = JSON.parse(String(queue[queueIndex++]).trim())
      if (String(message.requestId || "") !== requestId) continue
      if (message.type === "directory") {
        var error = TreeModel.stageDirectory(pendingCache, pendingChildren, message)
        if (error !== "") console.error("OMATREE_PIPELINE_FAIL " + error)
        else directoryCount++
      }
      else if (message.type === "complete") complete = message
    }
    if (queueIndex >= queue.length) {
      drainTimer.stop()
      queue = []
      queueIndex = 0
      maybeCommit()
    }
  }

  function maybeCommit() {
    if (!processExited || drainTimer.running || queue.length > 0 || !complete) return
    if (Number(complete.directoryCount) !== directoryCount) {
      console.error("OMATREE_PIPELINE_FAIL incomplete"); return
    }
    var built = TreeModel.finalizeTree(
      pendingCache, pendingChildren, directoryCount, scanPath, "fixture")
    if (built.error !== "") { console.error("OMATREE_PIPELINE_FAIL " + built.error); return }
    cache = built.cache
    var rootNode = built.root
    rebuildRows(cache, scanPath)
    var initialVisibleRowCount = visibleRows.count
    var sorted = true
    for (var parentPath in cache) {
      cache[parentPath].children.sort(function(leftPath, rightPath) {
        return cache[rightPath].bytes - cache[leftPath].bytes
      })
      for (var j = 1; j < cache[parentPath].children.length; j++) {
        if (cache[cache[parentPath].children[j - 1]].bytes
            < cache[cache[parentPath].children[j]].bytes) sorted = false
      }
    }
    var startsBeforeExpansion = processStarts
    for (var expandPath in cache) cache[expandPath].expanded = true
    var visible = TreeModel.visibleNodes(cache, scanPath)
    rootNode.expanded = false
    TreeModel.visibleNodes(cache, scanPath)
    rootNode.expanded = true
    var visibleAfterReexpand = TreeModel.visibleNodes(cache, scanPath)
    if (exitCode === 0 && complete && directoryCount > 1
        && visible.length === directoryCount && sorted
        && visibleAfterReexpand.length === directoryCount
        && initialVisibleRowCount === rootNode.children.length + 1
        && processStarts === startsBeforeExpansion && processStarts === 1
        && postorderFixturePassed && uxModelFixturePassed) {
      console.log("OMATREE_PIPELINE_PASS directories=" + directoryCount
                  + " rootChildren=" + rootNode.children.length
                  + " first=" + rootNode.children.slice(0, 10).join(","))
    } else {
      console.error("OMATREE_PIPELINE_FAIL")
    }
  }

  Process {
    id: scanProcess
    command: [
      "python3", root.helperPath, "scan",
      "--mountpoint", root.mountpoint,
      "--path", root.scanPath,
      "--request-id", root.requestId
    ]
    running: true
    onRunningChanged: if (running) root.processStarts++
    stdout: SplitParser { onRead: function(line) { root.enqueue(line) } }
    stderr: SplitParser { onRead: function(line) { console.error("OMATREE_PIPELINE_STDERR " + line) } }
    onExited: function(code) {
      console.log("OMATREE_PIPELINE_EXIT code=" + code + " helper=" + root.helperPath)
      root.exitCode = code
      root.processExited = true
      root.maybeCommit()
    }
  }

  // A zero interval does not fire under the Quickshell/Qt runtime used by
  // Omarchy 4.0.3. This test fails by hanging before PASS if regressed to 0.
  Timer { id: drainTimer; interval: 1; repeat: true; onTriggered: root.drainLines() }
}
