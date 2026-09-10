# OmaTree

OmaTree is a third-party Omarchy shell plugin for exploring disk usage in a
native Quickshell interface. It is inspired by TreeSize and ncdu.

The current version discovers mounted filesystems, groups them by their backing
physical disks where possible, and displays their capacity. Selecting a
filesystem starts one asynchronous allocated-size scan that constructs a full
in-memory directory snapshot. Expanding, collapsing, searching, and navigating
that snapshot perform no further filesystem traversal.

The panel provides a virtualized directory table with parent-relative size
bars, breadcrumbs, keyboard navigation, bounded in-memory search, Copy Path,
and Open Folder actions. Search is limited to the 500 largest matches so broad
queries remain responsive on very large directory trees.

## Scanner protocol

Run a scan by supplying both the selected filesystem mountpoint and a directory
inside it:

```bash
python3 helper/discover.py scan \
  --mountpoint /home \
  --path /home/camstraps \
  --request-id example-1
```

The command emits newline-delimited JSON. It reports coarse, rate-limited
progress and recoverable warnings, followed by one finalized `directory` record
per directory in post-order and a final completion record. Directory records
contain `path`, `parentPath`, allocated `bytes`, `directFilesBytes`,
`childDirectoryCount`, and `warningCount`, allowing the QML client to construct
the complete hierarchy without one giant JSON result. Sending SIGTERM or SIGINT
produces a `cancelled` record and never a successful `complete` record.

## Install

Once published, install and enable OmaTree with:

```bash
omarchy plugin add https://github.com/Camstraps/omatree.git --enable
```

During local development, update the installed clone and restart the shell so
Quickshell does not retain an older instantiated component:

```bash
omarchy plugin update io.github.camstraps.omatree --yes
omarchy restart shell
```

## Open

```bash
omarchy-shell shell summon io.github.camstraps.omatree '{}'
```

Press Escape, click outside the panel, or select **Close** to dismiss it.

## Requirements

- Omarchy 4 with the plugin schema v1 shell architecture
- Quickshell as provided by Omarchy

## License

MIT
