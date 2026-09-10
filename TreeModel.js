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

function createNode(name, path, bytes, depth, directFilesBytes) {
  return {
    name: String(name || path), path: String(path), bytes: Number(bytes || 0),
    formattedSize: formatBytes(bytes), depth: Number(depth || 0), expanded: false,
    loading: false, loaded: false, warningCount: 0, warningText: "", error: "",
    children: [], requestId: "", directFilesBytes: Number(directFilesBytes || 0),
    childDirectoryCount: 0
  }
}

function stageDirectory(cache, record) {
  var path = String(record.path || "")
  if (!path || cache[path]) return "Scanner returned duplicate or empty directory paths."
  var node = createNode(record.name, path, record.bytes, 0, record.directFilesBytes)
  node.loaded = true
  node.parentPath = record.parentPath === null ? "" : String(record.parentPath || "")
  node.childDirectoryCount = Number(record.childDirectoryCount || 0)
  node.warningCount = Number(record.warningCount || 0)
  node.warningText = node.warningCount > 0
    ? String(node.warningCount) + " path" + (node.warningCount === 1 ? "" : "s") + " could not be read"
    : ""
  cache[path] = node
  return ""
}

function finalizeTree(cache, recordCount, rootPath, rootName) {
  var root = cache[rootPath]
  if (!root) return { error: "Scanner did not return the filesystem root directory." }
  root.name = String(rootName || root.name)
  root.parentPath = ""
  root.expanded = true

  for (var childPath in cache) {
    var child = cache[childPath]
    if (childPath === rootPath) continue
    var parent = cache[child.parentPath]
    if (!parent) return { error: "Scanner returned a directory with no parent." }
    parent.children.push(childPath)
  }
  for (var parentPath in cache) {
    if (cache[parentPath].children.length !== cache[parentPath].childDirectoryCount)
      return { error: "Scanner returned an inconsistent directory hierarchy." }
    cache[parentPath].children.sort(function(leftPath, rightPath) {
      var left = cache[leftPath]
      var right = cache[rightPath]
      if (left.bytes !== right.bytes) return right.bytes - left.bytes
      return left.name.toLocaleLowerCase().localeCompare(right.name.toLocaleLowerCase())
    })
  }

  var visited = ({})
  var stack = [{ path: rootPath, depth: 0 }]
  var visitedCount = 0
  while (stack.length > 0) {
    var item = stack.pop()
    if (visited[item.path]) return { error: "Scanner returned a cyclic directory hierarchy." }
    visited[item.path] = true
    visitedCount++
    cache[item.path].depth = item.depth
    var children = cache[item.path].children
    for (var j = children.length - 1; j >= 0; j--)
      stack.push({ path: children[j], depth: item.depth + 1 })
  }
  if (visitedCount !== recordCount)
    return { error: "Scanner returned directories outside the filesystem root." }
  return { cache: cache, root: root, count: recordCount, error: "" }
}

function buildTree(records, rootPath, rootName) {
  var cache = ({})
  for (var i = 0; i < records.length; i++) {
    var error = stageDirectory(cache, records[i])
    if (error !== "") return { error: error }
  }
  return finalizeTree(cache, records.length, rootPath, rootName)
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
