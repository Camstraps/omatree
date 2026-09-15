import QtQuick
import Quickshell
import "BrowserState.js" as BrowserState

ShellRoot {
  id: root

  function require(value, message) {
    if (value) return true
    console.error("OMATREE_BROWSER_STATE_FAIL " + message)
    Qt.quit()
    return false
  }

  function row(path, parent, size) {
    return { path: path, parent_path: parent, name: path.split("/").pop() || "root",
      allocated_bytes: size, direct_files_bytes: 0, child_count: 1, warning_count: 0 }
  }

  function valid(value) {
    return value && typeof value.path === "string" && value.path !== ""
      && typeof value.name === "string" && value.name !== ""
      && typeof value.allocated_bytes === "number" && value.allocated_bytes >= 0
  }

  function run() {
    var limits = { pageRows: 64, maxRows: 2048, maxPages: 24,
      maxVisible: 512, maxExpansions: 256, maxBreadcrumbs: 128,
      maxMetadata: 256 }
    var state = BrowserState.create(limits, "generation")
    if (!require(BrowserState.putMetadata(state, row("/root", null, 999999), valid) === "", "root metadata")) return
    BrowserState.expand(state, "/root", "", ["/root"])
    var invalidBefore = BrowserState.stats(state).rows
    var malformed = [row("/root/good", "/root", 1), row("/root/bad", "/root", 1)]
    malformed[1].allocated_bytes = -1
    if (!require(BrowserState.insertRows(state, malformed, valid) !== ""
                 && BrowserState.stats(state).rows === invalidBefore,
                 "malformed batch atomicity")) return
    var duplicate = [row("/root/duplicate", "/root", 1), row("/root/duplicate", "/root", 1)]
    if (!require(BrowserState.insertRows(state, duplicate, valid) !== ""
                 && BrowserState.stats(state).rows === invalidBefore,
                 "duplicate batch atomicity")) return

    // More than 2048 unique rows and more than 24 pages pass through the
    // actual JS cache without either retained bound being exceeded.
    var previous = ""
    var flatStarted = Date.now()
    for (var pageIndex = 0; pageIndex < 7813; pageIndex++) {
      var rows = []
      for (var item = 0; item < 64; item++) {
        var number = pageIndex * 64 + item
        rows.push(row("/root/flat-" + number, "/root", 600000 - number))
      }
      var result = BrowserState.putPage(state, "/root", "token-" + pageIndex,
        rows, true, "token-" + (pageIndex + 1), previous, valid)
      if (!require(result.error === "", "page insertion " + pageIndex)) return
      BrowserState.setPage(state, "/root", result.key)
      previous = result.key
      var stats = BrowserState.stats(state)
      if (!require(stats.rows <= 2048 && stats.pages <= 24, "flat cache caps")) return
    }
    var flatElapsed = Date.now() - flatStarted

    // Expansion LRU cannot exceed 256, even after thousands of attempts.
    for (var expansion = 0; expansion < 3000; expansion++)
      BrowserState.expand(state, "/expanded-" + expansion, "", [])
    if (!require(BrowserState.stats(state).expansions <= 256, "expansion cap")) return

    // A logical expanded result larger than the backing model is clipped.
    var visibleState = BrowserState.create(limits, "visible-generation")
    BrowserState.putMetadata(visibleState, row("/v", null, 100000), valid)
    var rootChildren = []
    for (var child = 0; child < 64; child++) rootChildren.push(row("/v/p" + child, "/v", 1000 - child))
    var rootPage = BrowserState.putPage(visibleState, "/v", "", rootChildren, false, null, "", valid)
    BrowserState.expand(visibleState, "/v", rootPage.key, ["/v"])
    for (var parent = 0; parent < 10; parent++) {
      var nested = []
      for (var nestedIndex = 0; nestedIndex < 64; nestedIndex++)
        nested.push(row("/v/p" + parent + "/c" + nestedIndex, "/v/p" + parent, nestedIndex))
      var nestedPage = BrowserState.putPage(visibleState, "/v/p" + parent, "", nested, false, null, "", valid)
      BrowserState.expand(visibleState, "/v/p" + parent, nestedPage.key, ["/v"])
    }
    BrowserState.visible(visibleState, "/v")
    if (!require(BrowserState.stats(visibleState).visible === 512, "visible cap")) return

    var crumbs = []
    for (var depth = 0; depth < 200; depth++) crumbs.push(row("/deep/" + depth, depth ? "/deep/" + (depth - 1) : null, depth))
    BrowserState.setBreadcrumbs(visibleState, crumbs, true)
    if (!require(BrowserState.stats(visibleState).breadcrumbs <= 128, "breadcrumb cap")) return

    var before = BrowserState.stats(state)
    var navigationStarted = Date.now()
    for (var repeat = 0; repeat < 5000; repeat++) {
      BrowserState.visible(state, "/root")
      BrowserState.previousPage(state, "/root")
    }
    var after = BrowserState.stats(state)
    if (!require(after.rows === before.rows && after.pages === before.pages
                 && after.expansions === before.expansions, "repeated navigation growth")) return

    var expansionStarted = Date.now()
    for (var cached = 0; cached < 5000; cached++) {
      BrowserState.collapse(visibleState, "/v/p0")
      BrowserState.expand(visibleState, "/v/p0",
        BrowserState.pageKey("/v/p0", ""), ["/v"])
    }
    var cachedExpansionMs = Date.now() - expansionStarted
    var visibleStarted = Date.now()
    for (var rebuild = 0; rebuild < 1000; rebuild++) BrowserState.visible(visibleState, "/v")
    var visibleRebuildMs = Date.now() - visibleStarted

    console.log("OMATREE_BROWSER_STATE_PASS rows=" + after.rows
                + " pages=" + after.pages + " expansions=" + after.expansions
                + " visible=" + BrowserState.stats(visibleState).visible
                + " breadcrumbs=" + BrowserState.stats(visibleState).breadcrumbs
                + " metadata=" + BrowserState.stats(visibleState).metadata
                + " flat500kMs=" + flatElapsed
                + " repeatedNavigationMs=" + (Date.now() - navigationStarted)
                + " cachedExpansion5kMs=" + cachedExpansionMs
                + " visibleRebuild1kMs=" + visibleRebuildMs)
    Qt.quit()
  }

  Component.onCompleted: run()
}
