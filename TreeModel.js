.pragma library

function formatBytes(value) {
  var bytes = Number(value || 0)
  var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
  var index = 0
  while (Math.abs(bytes) >= 1024 && index < units.length - 1) {
    bytes /= 1024
    index++
  }
  var digits = index === 0 ? 0 : (bytes >= 100 ? 0 : 1)
  return bytes.toFixed(digits) + " " + units[index]
}

function createNode(name, path, bytes, depth) {
  return {
    name: String(name || path), path: String(path), bytes: Number(bytes || 0),
    formattedSize: formatBytes(bytes), depth: Number(depth || 0), expanded: false,
    loading: false, loaded: false, warningCount: 0, warningText: "", error: "",
    children: [], requestId: ""
  }
}

function visibleNodes(cache, rootPath) {
  var visible = []
  function append(path) {
    var node = cache[path]
    if (!node) return
    visible.push(node)
    if (!node.expanded || !node.loaded) return
    for (var i = 0; i < node.children.length; i++) append(node.children[i])
  }
  append(rootPath)
  return visible
}
