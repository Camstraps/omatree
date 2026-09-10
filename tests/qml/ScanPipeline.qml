import QtQuick
import Quickshell
import Quickshell.Io

ShellRoot {
  id: root

  property string requestId: "qml-pipeline-regression"
  property string repositoryPath: Quickshell.env("OMATREE_TEST_REPOSITORY")
  property string helperPath: repositoryPath + "/helper/discover.py"
  property string scanPath: repositoryPath
  property var queue: []
  property int queueIndex: 0
  property var parsedChildren: []
  property var complete: null
  property bool processExited: false
  property int exitCode: -1
  property var cache: ({})

  function createNode(name, path, bytes, depth) {
    return {
      name: String(name), path: String(path), bytes: Number(bytes || 0),
      depth: Number(depth || 0), expanded: false, loaded: false, children: []
    }
  }

  function visibleNodes(path) {
    var visible = []
    function append(nodePath) {
      var node = cache[nodePath]
      if (!node) return
      visible.push(node)
      if (!node.expanded || !node.loaded) return
      for (var i = 0; i < node.children.length; i++) append(node.children[i])
    }
    append(path)
    return visible
  }

  function enqueue(line) {
    queue.push(String(line || ""))
    if (!drainTimer.running) drainTimer.start()
  }

  function drainLines() {
    var end = Math.min(queue.length, queueIndex + 100)
    while (queueIndex < end) {
      var message = JSON.parse(String(queue[queueIndex++]).trim())
      if (String(message.requestId || "") !== requestId) continue
      if (message.type === "child") parsedChildren.push(message)
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
    var rootNode = createNode("OmaTree fixture", scanPath, complete.bytes, 0)
    rootNode.expanded = true
    rootNode.loaded = true
    cache[scanPath] = rootNode
    var paths = []
    for (var i = 0; i < parsedChildren.length; i++) {
      var child = createNode(parsedChildren[i].name, parsedChildren[i].path, parsedChildren[i].bytes, 1)
      cache[child.path] = child
      paths.push(child.path)
    }
    rootNode.children = paths
    var visible = visibleNodes(scanPath)
    if (exitCode === 0 && complete && parsedChildren.length > 0
        && visible.length === parsedChildren.length + 1) {
      console.log("OMATREE_PIPELINE_PASS children=" + parsedChildren.length)
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
