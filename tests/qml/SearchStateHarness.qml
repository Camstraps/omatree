import QtQuick
import Quickshell
import "SearchState.js" as SearchState

ShellRoot {
  function row(number) {
    return { path: "/root/match-" + number, parent_path: "/root",
      name: "match-" + number, allocated_bytes: Math.max(0, 500000 - number),
      direct_files_bytes: 0, child_count: 0, warning_count: 0 }
  }
  function valid(value) {
    return value && typeof value.path === "string" && value.path !== ""
      && typeof value.name === "string" && value.name !== ""
      && typeof value.allocated_bytes === "number" && value.allocated_bytes >= 0
  }
  function require(value, message) {
    if (value) return true
    console.error("OMATREE_SEARCH_STATE_FAIL " + message); Qt.quit(); return false
  }
  Component.onCompleted: {
    var limits = { pageRows: 64, maxRows: 128, maxPages: 2 }
    var state = SearchState.create("generation", "session", "match", limits)
    var started = Date.now()
    var rows = []
    for (var seed = 0; seed < 64; seed++) rows.push(row(seed))
    for (var page = 0; page < 7813; page++) {
      for (var index = 0; index < 64; index++) {
        var number = page * 64 + index
        rows[index].path = "/root/match-" + number
        rows[index].name = "match-" + number
        rows[index].allocated_bytes = Math.max(0, 500000 - number)
      }
      if (!require(SearchState.putPage(state, "t" + page, rows, true,
          "t" + (page + 1), valid) === "", "page insert")) return
      var stats = SearchState.stats(state)
      if (!require(stats.rows <= 128 && stats.pages <= 2 && stats.sessions === 1,
                   "search caps")) return
    }
    var before = SearchState.stats(state)
    var malformed = [row(1), row(2)]; malformed[1].allocated_bytes = -1
    if (!require(SearchState.putPage(state, "bad", malformed, false, null, valid) !== ""
        && SearchState.stats(state).rows === before.rows, "malformed atomicity")) return
    var duplicate = [row(3), row(3)]
    if (!require(SearchState.putPage(state, "dup", duplicate, false, null, valid) !== ""
        && SearchState.stats(state).rows === before.rows, "duplicate atomicity")) return
    for (var session = 0; session < 5000; session++) {
      SearchState.clear(state)
      state = SearchState.create("generation", "session-" + session, "q" + session, limits)
    }
    var finalStats = SearchState.stats(state)
    console.log("OMATREE_SEARCH_STATE_PASS rows=" + finalStats.rows
      + " pages=" + finalStats.pages + " sessions=" + finalStats.sessions
      + " match500kMs=" + (Date.now() - started))
    Qt.quit()
  }
}
