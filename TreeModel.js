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
    childDirectoryCount: 0, parentPath: ""
  }
}

function clampPercent(value) {
  var number = Number(value || 0)
  if (!isFinite(number)) return 0
  return Math.max(0, Math.min(100, number))
}

function percentageOfParent(cache, path, rootBytes) {
  var node = cache[path]
  if (!node) return 0
  if (!node.parentPath) return Number(node.bytes || 0) > 0 ? 100 : 0
  var parentBytes = node.parentPath && cache[node.parentPath]
    ? Number(cache[node.parentPath].bytes || 0) : Number(rootBytes || node.bytes || 0)
  if (parentBytes <= 0) return 0
  return clampPercent(Number(node.bytes || 0) * 100 / parentBytes)
}

function ancestorPaths(cache, path) {
  var result = []
  var current = cache[path]
  var seen = ({})
  while (current && !seen[current.path]) {
    result.unshift(current.path)
    seen[current.path] = true
    current = current.parentPath ? cache[current.parentPath] : null
  }
  return result
}

function revealPath(cache, path) {
  var paths = ancestorPaths(cache, path)
  for (var i = 0; i < paths.length - 1; i++) cache[paths[i]].expanded = true
  return paths
}

function searchMatches(cache, query, limit) {
  var normalized = String(query || "").toLocaleLowerCase()
  var matches = []
  if (!normalized) return { matches: matches, total: 0 }
  for (var path in cache) {
    var node = cache[path]
    if (node.name.toLocaleLowerCase().indexOf(normalized) !== -1) matches.push(node)
  }
  matches.sort(function(left, right) {
    if (left.bytes !== right.bytes) return right.bytes - left.bytes
    return left.path.localeCompare(right.path)
  })
  return { matches: matches.slice(0, Math.max(0, Number(limit || matches.length))), total: matches.length }
}

// Bounded min-heap used by the QML timer-driven search. It keeps search memory
// stable even for a query that matches every directory in a large snapshot.
function offerSearchMatch(heap, node, limit) {
  function less(left, right) {
    if (left.bytes !== right.bytes) return left.bytes < right.bytes
    return left.path > right.path
  }
  function swap(a, b) { var value = heap[a]; heap[a] = heap[b]; heap[b] = value }
  function up(index) {
    while (index > 0) {
      var parent = Math.floor((index - 1) / 2)
      if (!less(heap[index], heap[parent])) break
      swap(index, parent); index = parent
    }
  }
  function down(index) {
    while (true) {
      var left = index * 2 + 1, right = left + 1, smallest = index
      if (left < heap.length && less(heap[left], heap[smallest])) smallest = left
      if (right < heap.length && less(heap[right], heap[smallest])) smallest = right
      if (smallest === index) break
      swap(index, smallest); index = smallest
    }
  }
  if (heap.length < limit) { heap.push(node); up(heap.length - 1) }
  else if (limit > 0 && less(heap[0], node)) { heap[0] = node; down(0) }
}

function sortedSearchHeap(heap) {
  return heap.slice().sort(function(left, right) {
    if (left.bytes !== right.bytes) return right.bytes - left.bytes
    return left.path.localeCompare(right.path)
  })
}

function stageDirectory(cache, pendingChildren, record) {
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
  node.children = pendingChildren[path] || []
  delete pendingChildren[path]
  cache[path] = node
  if (node.parentPath !== "") {
    if (cache[node.parentPath]) cache[node.parentPath].children.push(path)
    else {
      if (!pendingChildren[node.parentPath]) pendingChildren[node.parentPath] = []
      pendingChildren[node.parentPath].push(path)
    }
  }
  return ""
}

function finalizeTree(cache, pendingChildren, recordCount, rootPath, rootName) {
  var root = cache[rootPath]
  if (!root) return { error: "Scanner did not return the filesystem root directory." }
  root.name = String(rootName || root.name)
  root.parentPath = ""
  root.expanded = true

  for (var unresolvedParent in pendingChildren)
    return { error: "Scanner returned a directory with no parent: " + unresolvedParent }

  var visited = ({})
  var stack = [{ path: rootPath, depth: 0 }]
  var visitedCount = 0
  while (stack.length > 0) {
    var item = stack.pop()
    if (visited[item.path]) return { error: "Scanner returned a cyclic directory hierarchy." }
    visited[item.path] = true
    visitedCount++
    var current = cache[item.path]
    current.depth = item.depth
    if (current.children.length !== current.childDirectoryCount)
      return { error: "Scanner returned an inconsistent directory hierarchy." }
    current.children.sort(function(leftPath, rightPath) {
      var left = cache[leftPath]
      var right = cache[rightPath]
      if (left.bytes !== right.bytes) return right.bytes - left.bytes
      return left.name.toLocaleLowerCase().localeCompare(right.name.toLocaleLowerCase())
    })
    var children = current.children
    for (var j = children.length - 1; j >= 0; j--)
      stack.push({ path: children[j], depth: item.depth + 1 })
  }
  if (visitedCount !== recordCount)
    return { error: "Scanner returned directories outside the filesystem root." }
  return { cache: cache, root: root, count: recordCount, error: "" }
}

function buildTree(records, rootPath, rootName) {
  var cache = ({})
  var pendingChildren = ({})
  for (var i = 0; i < records.length; i++) {
    var error = stageDirectory(cache, pendingChildren, records[i])
    if (error !== "") return { error: error }
  }
  return finalizeTree(cache, pendingChildren, records.length, rootPath, rootName)
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
