# OmaTree

OmaTree is an Omarchy disk-usage analyzer inspired by TreeSize and ncdu. It
combines a native Quickshell panel, a lightweight bar widget, and an optional
curses terminal interface.

![OmaTree Marketplace preview](preview.png)

## Features

- Filesystem capacity overview with physical-disk grouping where available
- Filesystem-aware scans with explicit nested-mount pruning
- Directory and immediate-file browsing, sorted directories first and then by size
- Persistent SQLite snapshots that reopen without another recursive scan
- Bounded, lazy pagination for very large directories
- Snapshot-wide directory-name search and breadcrumb navigation
- Allocated-size accounting (`st_blocks * 512`) with hardlink deduplication
- Symlinks are never followed
- Atomic, cancellable rescans that keep the previous snapshot available
- Lightweight bar widget that performs capacity discovery only
- Curses TUI using the shared discovery and scanner core

## Installation and panel usage

Install and enable OmaTree from the Omarchy Marketplace, or from GitHub:

```bash
omarchy plugin add https://github.com/Camstraps/omatree.git --enable
omarchy restart shell
```

Left-click the bar widget to open the panel. Right-click cycles its capacity
display mode. Select a filesystem in the panel; OmaTree immediately reuses its
last valid snapshot, or performs an initial scan when none exists. **Rescan**
builds a replacement in the background while the current snapshot remains
browseable. A replacement becomes visible only after successful validation.

Panel search covers directory names across the complete active snapshot. File
rows are browseable but are not included in search in v0.2.1.

## Terminal interface

Run the launcher directly (the default target is `$HOME`):

```bash
~/.config/omarchy/plugins/io.github.camstraps.omatree/bin/omatree
~/.config/omarchy/plugins/io.github.camstraps.omatree/bin/omatree ~/Downloads
```

The initial TUI remains directory-only. It shares OmaTree's scanner, mount
policy, allocated-size accounting, hardlink handling, and symlink safety.

To explicitly install a shell command, create an OmaTree-owned symlink with:

```bash
~/.config/omarchy/plugins/io.github.camstraps.omatree/bin/omatree install-cli
omatree ~/Downloads
```

The plugin never changes `~/.local/bin` merely by being enabled. The installer
refuses to overwrite unrelated paths. Remove its symlink with:

```bash
omatree uninstall-cli
```

TUI controls: Up/Down or `k`/`j` move; Enter toggles a directory; Right/`l`
expands; Left/`h` collapses; Backspace selects the parent; Home, End, Page Up,
and Page Down move farther; `r` rescans; `q` quits. During scanning, `q` or
Ctrl-C cancels without committing a partial result.

## Architecture and safety

The Python scanner streams finalized records directly into a generation-specific
SQLite database. The panel-owned broker validates and atomically activates the
database, then serves bounded pages for browsing, breadcrumbs, and search.
Quickshell never retains a complete filesystem tree. Files in directories with
hundreds of thousands of entries remain paginated, and opening, navigation, and
search do not rescan the filesystem.

Snapshots are private per-user cache data. Schema-incompatible or corrupt
snapshots are ignored and rebuilt safely. Scanner, database, protocol, parser,
and frontend caches all enforce explicit resource ceilings. The bar widget
never starts recursive work and no root privileges are required.

## Updating and removal

```bash
omarchy plugin update io.github.camstraps.omatree --yes
omarchy restart shell
```

Before removing the plugin, optionally run `omatree uninstall-cli`. Cached
snapshots are disposable and may be removed from `$XDG_CACHE_HOME/omatree`
(normally `~/.cache/omatree`) while OmaTree is closed.

## Release notes

### v0.2.1

- Reuses the latest valid per-filesystem snapshot on panel reopen.
- Adds explicit background Rescan with atomic replacement.
- Adds bounded, paginated immediate-file browsing in the panel.
- Preserves directory-only snapshot-wide search and the directory-only TUI.

### v0.2.0

- Replaced the complete QML tree with broker-owned SQLite snapshots and bounded
  lazy browsing/search state.
- Added the curses TUI and opt-in CLI symlink installer.
- Hardened executable identities, subprocess lifecycles, protocol bounds,
  snapshot validation, and cleanup for Marketplace review.

## Development

```bash
/usr/bin/python3 -m unittest discover -s tests
omarchy plugin validate .
qmllint -I /usr/share/omarchy/shell Panel.qml BarWidget.qml
git diff --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md). OmaTree is available under the
[MIT License](LICENSE).
