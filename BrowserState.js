.pragma library

// Bounded, frontend-only navigation state.  Rows are owned by `rows`; pages,
// expansions, breadcrumbs and the visible window contain path references only.

function create(limits, generationId) {
  return {
    limits: limits, generationId: generationId, clock: 0,
    rows: ({}), rowCount: 0,
    pages: ({}), pageCount: 0,
    expansions: ({}), expansionCount: 0,
    metadata: ({}), metadataCount: 0,
    breadcrumbs: [], visible: [], selection: ""
  }
}

function touch(state, object) { object.used = ++state.clock }

function pageKey(parent, continuation) {
  return parent + "\u0000" + String(continuation || "")
}

function contains(array, value) { return array.indexOf(value) !== -1 }

function removePage(state, key) {
  var page = state.pages[key]
  if (!page) return
  delete state.pages[key]
  state.pageCount--
  for (var path in state.expansions) {
    var expansion = state.expansions[path]
    if (expansion.pageKey === key) expansion.pageKey = ""
    if (expansion.previousPageKey === key) expansion.previousPageKey = ""
  }
}

function pinnedPaths(state) {
  var pinned = ({})
  if (state.selection) pinned[state.selection] = true
  for (var i = 0; i < state.visible.length; i++) pinned[state.visible[i].path] = true
  for (var j = 0; j < state.breadcrumbs.length; j++) {
    var crumb = state.breadcrumbs[j]
    if (crumb.path) pinned[crumb.path] = true
  }
  for (var path in state.expansions) pinned[path] = true
  for (var key in state.pages) {
    var paths = state.pages[key].paths
    for (var pageIndex = 0; pageIndex < paths.length; pageIndex++)
      pinned[paths[pageIndex]] = true
  }
  return pinned
}

function removeRowAndReferences(state, path) {
  if (!state.rows[path]) return
  var pageKeys = []
  for (var key in state.pages)
    if (contains(state.pages[key].paths, path)) pageKeys.push(key)
  for (var i = 0; i < pageKeys.length; i++) removePage(state, pageKeys[i])
  if (state.metadata[path]) { delete state.metadata[path]; state.metadataCount-- }
  delete state.rows[path]
  state.rowCount--
}

function evictOnePage(state, protectedKey) {
  var candidate = "", used = Number.MAX_SAFE_INTEGER
  for (var key in state.pages) {
    if (key === protectedKey) continue
    var page = state.pages[key]
    var current = false
    for (var path in state.expansions)
      if (state.expansions[path].pageKey === key) { current = true; break }
    if (!current && page.used < used) { candidate = key; used = page.used }
  }
  if (!candidate) {
    for (var fallback in state.pages) {
      if (fallback === protectedKey) continue
      if (state.pages[fallback].used < used) {
        candidate = fallback; used = state.pages[fallback].used
      }
    }
  }
  if (!candidate) return false
  removePage(state, candidate)
  return true
}

function ensurePageRoom(state, protectedKey) {
  while (state.pageCount >= state.limits.maxPages)
    if (!evictOnePage(state, protectedKey)) return false
  return true
}

function ensureRowRoom(state, needed) {
  while (state.rowCount + needed > state.limits.maxRows) {
    var pinned = pinnedPaths(state)
    var candidates = []
    for (var path in state.rows) {
      if (!pinned[path]) candidates.push({ path: path, used: state.rows[path].used })
    }
    candidates.sort(function(left, right) { return left.used - right.used })
    var removeCount = Math.min(candidates.length,
      state.rowCount + needed - state.limits.maxRows)
    for (var index = 0; index < removeCount; index++) {
      var candidate = candidates[index].path
      if (state.metadata[candidate]) { delete state.metadata[candidate]; state.metadataCount-- }
      delete state.rows[candidate]
      state.rowCount--
    }
    if (removeCount === 0) {
      if (!evictOnePage(state, "")) return false
      continue
    }
  }
  return true
}

function validateBatch(state, rows, validateRow) {
  if (!Array.isArray(rows) || rows.length > state.limits.pageRows) return "invalid page size"
  var seen = ({})
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (!validateRow(row)) return "invalid directory row"
    if (seen[row.path]) return "duplicate directory row"
    seen[row.path] = true
    var old = state.rows[row.path]
    if (old && (old.parent_path !== row.parent_path || old.name !== row.name
        || old.allocated_bytes !== row.allocated_bytes
        || old.direct_files_bytes !== row.direct_files_bytes
        || old.child_count !== row.child_count || old.warning_count !== row.warning_count
        || old.kind !== (row.kind || "directory")))
      return "conflicting directory row"
  }
  return ""
}

function insertRows(state, rows, validateRow) {
  var error = validateBatch(state, rows, validateRow)
  if (error) return error
  var needed = 0
  for (var i = 0; i < rows.length; i++) if (!state.rows[rows[i].path]) needed++
  if (!ensureRowRoom(state, needed)) return "directory cache is full"
  for (var j = 0; j < rows.length; j++) {
    var source = rows[j]
    var row = state.rows[source.path]
    if (!row) {
      row = {
        path: source.path, parent_path: source.parent_path, name: source.name,
        allocated_bytes: source.allocated_bytes,
        direct_files_bytes: source.direct_files_bytes,
        child_count: source.child_count, warning_count: source.warning_count,
        kind: source.kind || "directory",
        used: 0
      }
      state.rows[row.path] = row
      state.rowCount++
    }
    touch(state, row)
  }
  return ""
}

function putMetadata(state, row, validateRow) {
  var error = insertRows(state, [row], validateRow)
  if (error) return error
  if (!state.metadata[row.path]) {
    while (state.metadataCount >= state.limits.maxMetadata) {
      var candidate = "", used = Number.MAX_SAFE_INTEGER
      for (var path in state.metadata) {
        if (path !== state.selection && state.rows[path].used < used) {
          candidate = path; used = state.rows[path].used
        }
      }
      if (!candidate) return "metadata cache is full"
      delete state.metadata[candidate]
      state.metadataCount--
    }
    state.metadata[row.path] = true
    state.metadataCount++
  }
  return ""
}

function putPage(state, parent, inputContinuation, rows, hasMore, nextContinuation,
                 previousPageKey, validateRow) {
  var error = validateBatch(state, rows, validateRow)
  if (error) return { error: error }
  var key = pageKey(parent, inputContinuation)
  if (!state.pages[key] && !ensurePageRoom(state, key)) return { error: "page cache is full" }
  error = insertRows(state, rows, validateRow)
  if (error) return { error: error }
  var paths = []
  for (var i = 0; i < rows.length; i++) paths.push(rows[i].path)
  if (!state.pages[key]) state.pageCount++
  state.pages[key] = {
    key: key, parent: parent, inputContinuation: inputContinuation || "",
    paths: paths, hasMore: hasMore, nextContinuation: nextContinuation || "",
    previousPageKey: previousPageKey || "", used: 0
  }
  touch(state, state.pages[key])
  return { error: "", key: key }
}

function ensureExpansionRoom(state, pinned) {
  while (state.expansionCount >= state.limits.maxExpansions) {
    var candidate = "", used = Number.MAX_SAFE_INTEGER
    for (var path in state.expansions) {
      if (!pinned[path] && state.expansions[path].used < used) {
        candidate = path; used = state.expansions[path].used
      }
    }
    if (!candidate) return false
    delete state.expansions[candidate]
    state.expansionCount--
  }
  return true
}

function expand(state, path, page, ancestorPaths) {
  var pinned = ({})
  for (var i = 0; i < ancestorPaths.length; i++) pinned[ancestorPaths[i]] = true
  var expansion = state.expansions[path]
  if (!expansion) {
    if (!ensureExpansionRoom(state, pinned)) return false
    expansion = { path: path, pageKey: page || "", previousPageKey: "", used: 0 }
    state.expansions[path] = expansion
    state.expansionCount++
  }
  if (page) expansion.pageKey = page
  touch(state, expansion)
  return true
}

function collapse(state, path) {
  if (!state.expansions[path]) return false
  delete state.expansions[path]
  state.expansionCount--
  return true
}

function setPage(state, parent, key) {
  var expansion = state.expansions[parent]
  var page = state.pages[key]
  if (!expansion || !page) return false
  expansion.previousPageKey = page.previousPageKey
  expansion.pageKey = key
  touch(state, expansion); touch(state, page)
  return true
}

function previousPage(state, parent) {
  var expansion = state.expansions[parent]
  if (!expansion || !expansion.pageKey) return ""
  var current = state.pages[expansion.pageKey]
  if (!current || !current.previousPageKey || !state.pages[current.previousPageKey]) return ""
  return current.previousPageKey
}

function visible(state, rootPath) {
  var output = [], stack = [{ path: rootPath, depth: 0 }]
  while (stack.length && output.length < state.limits.maxVisible) {
    var item = stack.pop(), row = state.rows[item.path]
    if (!row) continue
    output.push({ path: item.path, depth: item.depth })
    touch(state, row)
    var expansion = state.expansions[item.path]
    var page = expansion && state.pages[expansion.pageKey]
    if (!page) continue
    touch(state, expansion); touch(state, page)
    for (var i = page.paths.length - 1; i >= 0; i--)
      stack.push({ path: page.paths[i], depth: item.depth + 1 })
  }
  state.visible = output
  return output
}

function setBreadcrumbs(state, rows, truncated) {
  var maximum = state.limits.maxBreadcrumbs
  // Broker ancestor pages arrive selected-directory first. Display the
  // bounded nearest chain in rootward-to-selected order.
  var result = rows.slice(0, maximum).reverse()
  if (truncated && result.length >= maximum) result.shift()
  state.breadcrumbs = result
  if (truncated) state.breadcrumbs.unshift({ marker: true, name: "…", path: "" })
  return state.breadcrumbs
}

function stats(state) {
  return { rows: state.rowCount, pages: state.pageCount,
    expansions: state.expansionCount, visible: state.visible.length,
    breadcrumbs: state.breadcrumbs.length, metadata: state.metadataCount }
}
