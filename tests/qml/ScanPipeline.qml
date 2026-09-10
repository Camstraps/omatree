import QtQuick
import Quickshell
import Quickshell.Io
import "TreeModel.js" as TreeModel

ShellRoot {
  id: root

  property string requestId: "qml-pipeline-regression"
  property string repositoryPath: Quickshell.env("OMATREE_TEST_REPOSITORY")
  property string helperPath: repositoryPath + "/helper/discover.py"
  property string scanPath: repositoryPath
  property var queue: []
  property int queueIndex: 0
  property var pendingCache: ({})
  property int directoryCount: 0
  property var complete: null
  property bool processExited: false
  property int exitCode: -1
  property int processStarts: 0
  property var cache: ({})

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
        var error = TreeModel.stageDirectory(pendingCache, message)
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
    var built = TreeModel.finalizeTree(pendingCache, directoryCount, scanPath, "fixture")
    if (built.error !== "") { console.error("OMATREE_PIPELINE_FAIL " + built.error); return }
    cache = built.cache
    var rootNode = built.root
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
        && processStarts === startsBeforeExpansion && processStarts === 1) {
      console.log("OMATREE_PIPELINE_PASS directories=" + directoryCount)
    } else {
      console.error("OMATREE_PIPELINE_FAIL")
    }
  }

  Process {
    id: scanProcess
    command: [
      "python3", root.helperPath, "scan",
      "--mountpoint", root.scanPath,
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
