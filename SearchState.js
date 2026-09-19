.pragma library

function create(generationId, sessionId, query, limits) {
  return { generationId: generationId, sessionId: sessionId, query: query,
    limits: limits, pages: [], currentIndex: -1, loading: false, error: "",
    selectedPath: "" }
}

function stats(state) {
  var count = 0
  for (var i = 0; i < state.pages.length; i++) count += state.pages[i].rows.length
  return { rows: count, pages: state.pages.length, sessions: 1 }
}

function putPage(state, inputContinuation, rows, hasMore, continuation, validateRow) {
  if (!Array.isArray(rows) || rows.length > state.limits.pageRows)
    return "invalid search page size"
  var seen = ({})
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (!validateRow(row)) return "invalid search result"
    if (seen[row.path]) return "duplicate search result"
    seen[row.path] = true
  }
  var copy = []
  for (var j = 0; j < rows.length; j++) {
    var source = rows[j]
    copy.push({ path: source.path, parent_path: source.parent_path, name: source.name,
      allocated_bytes: source.allocated_bytes, direct_files_bytes: source.direct_files_bytes,
      child_count: source.child_count, warning_count: source.warning_count,
      kind: source.kind || "directory" })
  }
  if (state.pages.length >= state.limits.maxPages) state.pages.shift()
  state.pages.push({ inputContinuation: inputContinuation || "", rows: copy,
    hasMore: hasMore, continuation: continuation || "" })
  state.currentIndex = state.pages.length - 1
  state.loading = false
  state.error = ""
  return ""
}

function currentPage(state) {
  return state && state.currentIndex >= 0 ? state.pages[state.currentIndex] : null
}

function previous(state) {
  if (!state || state.currentIndex <= 0) return false
  state.currentIndex--
  return true
}

function nextCached(state) {
  if (!state || state.currentIndex + 1 >= state.pages.length) return false
  state.currentIndex++
  return true
}

function clear(state) {
  if (!state) return
  state.pages = []
  state.currentIndex = -1
  state.selectedPath = ""
  state.loading = false
  state.error = ""
}
