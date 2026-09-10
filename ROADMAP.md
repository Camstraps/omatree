# OmaTree Roadmap

OmaTree v0.1.0 establishes the initial foundation: filesystem-aware disk usage scanning, an expandable directory tree, search, navigation, and native Omarchy integration.

The following roadmap contains the main ideas currently being considered for future releases.

## Planned

### Faster scanning

Improve scanning performance, especially on filesystems containing a very large number of directories and files.

Before changing the scanner architecture, performance should be benchmarked to identify the main bottlenecks, including filesystem traversal, metadata collection, NDJSON streaming, and QML-side processing.

The goal is to make scans noticeably faster without sacrificing filesystem boundaries, symlink safety, hardlink handling, cancellation, or accuracy.

### Persistent OmaTree window

Improve how OmaTree behaves when used as a disk management tool.

The current panel can close when it loses focus, which makes actions such as opening a directory less convenient.

Explore a persistent window/application mode that can remain open independently of panel focus.

Ideally, OmaTree could also be launched from a terminal:

```bash
omatree
```
And optionally opened directly at a specific location:

```bash
omatree /path/to/directory
```
This should remain compatible with Omarchy's Quickshell architecture rather than spawning unnecessary shell instances.

### Customizable sidebar

Allow users to customize the locations displayed in the sidebar.
In addition to automatically detected filesystems, users could pin directories they frequently want to inspect, such as:
- Downloads
- Games
- Projects
- External drives
- NAS or other mounted locations

Pinned locations should be easy to add and remove without affecting the automatically detected filesystem list.


### Move directories to Trash

Add an action for moving selected directories to the user's Trash directly from OmaTree.
Deletion should be safe by default:
- Move to Trash instead of permanently deleting
- Require confirmation before the operation
- Clearly display the path being affected
- Avoid using permanent recursive deletion as the default behavior

The implementation should follow the standard Linux/Freedesktop Trash behavior where possible.

## Future Ideas

### Display individual files

OmaTree currently focuses on directories.
A future version could display individual files alongside directories, making it easier to identify exactly which files consume the most disk space.
This needs to be designed carefully because retaining millions of individual file nodes could significantly increase memory usage in the Omarchy shell.
Possible approaches should prioritize keeping the interface responsive and memory usage reasonable.

## Current Stable Foundation

### V0.1.0

- Filesystem capacity overview
- Physical-disk grouping
- Mount-boundary-aware recursive scanning
- Complete expandable directory hierarchy
- Size and percentage-of-parent visualization
- Directory search
- Breadcrumb navigation
- Copy Path
- Open Folder
- Native Omarchy bar widget
- Symlink-safe scanning
- Hardlink-aware allocated-size accounting
- Cancellable unprivileged scans
- NDJSON streaming

## Notes

This roadmap is intentionally not tied to strict release numbers yet.
Features may be reordered, changed, split into smaller milestones, or dropped depending on technical constraints, performance testing, user feedback, and changes to Omarchy or Quickshell.
The priority is to keep OmaTree fast, reliable, safe, and well integrated with Omarchy rather than adding features simply to meet a fixed version schedule.