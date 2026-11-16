pyrasite-gui
============

A graphical interface for `pyrasite
<https://github.com/lmacken/pyrasite>`_ that lets you monitor, analyze,
introspect, and alter running Python programs.

Requirements
------------

* Python 3.9+
* `gdb <https://www.gnu.org/s/gdb>`_ (7.3+) and permission to attach to the
  target process. See the pyrasite installation notes for ``ptrace`` setup.
* GTK 3 and WebKit2 introspection bindings.

On Debian/Ubuntu::

    sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gdb

On Fedora::

    sudo dnf install python3-gobject gtk3 webkit2gtk4.1 gdb

The ``dot`` binary from Graphviz is required for the call graph tab::

    sudo apt install graphviz

Optional features:

* The Objects tab uses a built-in ``gc.get_objects()`` summary. ``meliae`` adds
  per-type memory accounting, but it does not build on current CPython
  versions, so the built-in summary is used by default.
* Call graphs need ``pycallgraph2`` in the target interpreter and Graphviz.

Installation
------------

Install pyrasite and pyrasite-gui into the same Python environment. Because
PyGObject is normally provided by the system package manager, a virtualenv
that inherits system packages is the simplest setup::

    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install pyrasite-gui

Or from this checkout::

    python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -e .

Running
-------

::

    pyrasite-gui

Select a process in the left pane. Pyrasite injects a reverse Python
connection and populates the Resources, Stacks, Objects, Shell, Call Graph,
and Details tabs.

The target must allow ``gdb`` to attach. If ``/proc/sys/kernel/yama/ptrace_scope``
is non-zero, either relax it::

    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope

or have the target opt in with ``prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)``.

Troubleshooting
---------------

* **"Unable to import the GTK 3 / WebKit2 bindings"** — install the system
  packages listed above and make sure the GUI runs with the same interpreter
  that can import ``gi``.
* **Injection fails with a ptrace error** — see the ptrace notes above.
* **Objects tab mentions meliae** — ``meliae`` does not build on current
  CPython versions. The tab falls back to the built-in ``gc.get_objects()``
  summary, which is expected.
* **Call Graph is unavailable** — install Graphviz and make sure ``dot`` is on
  ``PATH``.
