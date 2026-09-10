# OmaTree

OmaTree is a native Omarchy disk usage analyzer inspired by TreeSize and
ncdu. It presents mounted filesystems and their directory usage through an
Omarchy/Quickshell panel and optional bar widget.

## Features

- Native Omarchy panel and bar widget
- Filesystem capacity overview and physical-disk grouping where available
- One recursive scan builds a complete in-memory directory hierarchy
- Expandable directory tree with size and percentage-of-parent visualization
- Directory search and breadcrumb navigation
- Copy Path and Open Folder actions
- Explicit mount-boundary pruning, including nested mounts
- Symlinks are never followed
- Hard-linked files are counted once per scan
- Allocated-size accounting using `st_blocks * 512`
- Cancellable, unprivileged scans with atomic result commits

## Screenshots

<!-- Add screenshots when available:
![OmaTree panel](docs/screenshots/panel.png)
![OmaTree bar widget](docs/screenshots/bar-widget.png)
-->

Screenshots are coming soon.

## Requirements

- Omarchy Quattro (tested with Omarchy 4.0.3)
- Quickshell as supplied by Omarchy
- Python 3
- `lsblk` and `findmnt` from util-linux
- `wl-copy` for Copy Path
- `xdg-open` for Open Folder

## Installation

Install and enable OmaTree from GitHub:

```bash
omarchy plugin add https://github.com/Camstraps/omatree.git --enable
```

When prompted, choose the bar section where OmaTree should appear. Restart the
shell once after installation:

```bash
omarchy restart shell
```

### Troubleshooting

Development copies installed before OmaTree gained its bar widget may already
be registered only as a panel. Omarchy 4.0.3 can preserve that stale
`plugins[]` registration across removal because it reports mixed plugins as
enabled only when their widget is on the bar. Repair an installed copy using
the supported commands below; no manual `shell.json` edit is required:

```bash
omarchy plugin disable io.github.camstraps.omatree
omarchy plugin enable io.github.camstraps.omatree --section right
omarchy restart shell
```

To perform a clean reinstall from that state, explicitly disable before
removing so Omarchy clears the stale registration:

```bash
omarchy plugin disable io.github.camstraps.omatree
omarchy plugin remove io.github.camstraps.omatree --yes
omarchy plugin add https://github.com/Camstraps/omatree.git --enable
omarchy restart shell
```

If updated QML appears stale, run:

```bash
omarchy plugin update io.github.camstraps.omatree --yes
omarchy restart shell
```

## Usage

Open OmaTree by left-clicking its bar widget or with:

```bash
omarchy-shell shell summon io.github.camstraps.omatree '{}'
```

Choose a filesystem and wait for its initial scan. Expanding and collapsing
directories then uses the completed in-memory snapshot without scanning again.
Search also operates only on that snapshot. Use **Refresh** to discard it and
scan the selected filesystem again.

The bar widget shows the filesystem containing `$HOME`. Left click opens
OmaTree; right click cycles between percentage used, used space, free space,
and detailed display modes.

## Performance

The initial scan can take time on filesystems containing millions of entries.
After it completes, directory expansion is memory-only and performs no further
recursive scans. Large directory trees also require corresponding memory in
the long-running shell process.

## Architecture

A Python standard-library helper discovers filesystems and performs
unprivileged scans. It streams rate-limited progress and post-order directory
records as NDJSON. QML incrementally reconstructs and atomically commits the
complete hierarchy to an in-memory model. No root privileges or background
daemon are required.

## Limitations

- Only directories are displayed; individual files are not shown yet.
- Filesystem changes are not reflected until Refresh.
- Very large directory trees consume additional shell memory.
- Hardlink attribution between sibling directories depends on traversal order.
- Network filesystem scans may be slow or become unavailable during a scan.
- Copy Path requires `wl-copy`.

## Development

Run the test and validation suite from the repository root:

```bash
python3 -m unittest discover -s tests
omarchy plugin validate .
qmllint -I /usr/share/omarchy/shell Panel.qml BarWidget.qml
git diff --check
```

Update an installed development copy and reload Quickshell with:

```bash
omarchy plugin update io.github.camstraps.omatree --yes
omarchy restart shell
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

## License

OmaTree is available under the [MIT License](LICENSE).
