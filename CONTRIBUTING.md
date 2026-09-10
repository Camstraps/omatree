# Contributing to OmaTree

Bug reports and focused pull requests are welcome. Please describe the Omarchy
version, filesystem type, mount layout, and steps needed to reproduce an issue.
Never include private paths or directory listings without reviewing them first.

Before opening a pull request, run:

```bash
python3 -m unittest discover -s tests
omarchy plugin validate .
qmllint -I /usr/share/omarchy/shell Panel.qml BarWidget.qml
git diff --check
```

Keep filesystem traversal in the Python helper, preserve mount-boundary and
cancellation behavior, and avoid blocking work in QML.
