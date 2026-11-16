#!/usr/bin/env python
# This file is part of pyrasite.
#
# pyrasite is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyrasite is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyrasite.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2012 Red Hat, Inc., Luke Macken <lmacken@redhat.com>
#
# This interface may contain some code from the gtk-demo, written
# by John (J5) Palmieri, and licensed under the LGPLv2.1
# http://git.gnome.org/browse/pygobject/tree/demos/gtk-demo/gtk-demo.py

import contextlib
import io
import json
import keyword
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import tokenize
from os.path import abspath, dirname, join
from random import randrange

import psutil

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Pango", "1.0")
    for _webkit_version in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", _webkit_version)
            break
        except ValueError:
            continue
    else:
        raise ImportError("WebKit2")
    from gi.repository import GLib, GObject, Gtk, Pango
    from gi.repository import WebKit2 as WebKit
except (ImportError, ValueError) as exc:
    print("Unable to import the GTK 3 / WebKit2 bindings: %s" % exc)
    print("On Debian/Ubuntu install python3-gi and gir1.2-webkit2-4.1.")
    print("On Fedora install pygobject3 and webkit2gtk4.1.")
    sys.exit(1)

try:
    from meliae import loader
except Exception:
    loader = None
    print("meliae not found; using the built-in object summary.")

import pyrasite

log = logging.getLogger("pyrasite")

POLL_INTERVAL = 1.0
INTERVALS = 200
TEMP_DIR = tempfile.gettempdir()
cpu_intervals = []
cpu_details = ""
mem_intervals = []
mem_details = ""
write_intervals = []
read_intervals = []
read_count = read_bytes = write_count = write_bytes = 0

process_title = ""
process_status = ""
thread_intervals = {}
thread_colors = {}
thread_totals = {}

open_connections = []
open_files = []


def _process_value(proc, *names, default=None):
    """Return the first available psutil value, calling methods if needed."""
    for name in names:
        attr = getattr(proc, name, None)
        if attr is None:
            continue
        try:
            value = attr() if callable(attr) else attr
        except (psutil.Error, OSError):
            continue
        if value is not None:
            return value
    return default


def _format_cmdline(value):
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value)
    return str(value)


def _format_time(value):
    if value is None:
        return "?"
    try:
        return time.ctime(float(value))
    except (TypeError, ValueError, OSError):
        return "?"


def _format_address(address):
    if not address:
        return "*:*"
    try:
        host, port = address
    except (TypeError, ValueError):
        return str(address)
    return "%s:%s" % (host, port)


def _ptrace_restricted():
    """Return True when Yama only allows attaching to descendants."""
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            return int(f.read().strip()) != 0
    except (OSError, ValueError):
        return False


def _ptrace_hint():
    return (
        "ptrace is restricted (kernel.yama.ptrace_scope is non-zero).\n"
        "Run this once to allow gdb to attach:\n\n"
        "    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope\n\n"
        "Alternatively, have the target call "
        "prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY) before attaching."
    )


class Process(pyrasite.PyrasiteIPC, GObject.GObject):
    """
    A :class:`GObject.GObject` subclass that represents a Process, for use in
    the :class:`ProcessTreeStore`
    """


class ProcessListStore(Gtk.ListStore):
    """This TreeStore finds all running python processes."""

    def __init__(self, *args):
        Gtk.ListStore.__init__(self, str, Process, Pango.Style)
        for pid in os.listdir("/proc"):
            try:
                pid = int(pid)
                proc = Process(pid)
                try:
                    with open("/proc/%d/maps" % pid) as maps_file:
                        maps = maps_file.read().strip()
                    if "python" in maps:
                        self.append((proc.title.strip(), proc, Pango.Style.NORMAL))
                except OSError:
                    pass
            except ValueError:
                pass


class PyrasiteWindow(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.processes = {}
        self.pid = None  # Currently selected pid
        self.resource_thread = None

        self.set_title("Pyrasite v%s" % pyrasite.__version__)
        self.set_default_size(600, 400)

        hbox = Gtk.HBox(homogeneous=False, spacing=0)
        self.add(hbox)

        tree = self.create_tree()
        hbox.pack_start(tree, False, False, 0)

        notebook = Gtk.Notebook()

        main_vbox = Gtk.VBox()
        main_vbox.pack_start(notebook, True, True, 0)
        self.progress = Gtk.ProgressBar()
        main_vbox.pack_end(self.progress, False, False, 0)
        hbox.pack_start(main_vbox, True, True, 0)

        self.info_html = ""
        self.info_view = WebKit.WebView()
        self._js_injected = False
        self.info_view.connect("load-changed", self.on_info_load_changed)
        self.info_view.load_html(self.info_html, None)

        info_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        info_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        info_window.add(self.info_view)
        notebook.append_page(info_window, Gtk.Label.new_with_mnemonic("_Resources"))

        (stacks_widget, source_buffer) = self.create_text(True)
        notebook.append_page(stacks_widget, Gtk.Label.new_with_mnemonic("_Stacks"))

        self.source_buffer = source_buffer
        self.source_buffer.create_tag("bold", weight=Pango.Weight.BOLD)
        self.source_buffer.create_tag("italic", style=Pango.Style.ITALIC)
        self.source_buffer.create_tag("comment", foreground="#c0c0c0")
        self.source_buffer.create_tag(
            "decorator", foreground="#7d7d7d", style=Pango.Style.ITALIC
        )
        self.source_buffer.create_tag("keyword", foreground="#0000ff")
        self.source_buffer.create_tag("number", foreground="#800000")
        self.source_buffer.create_tag(
            "string", foreground="#00aa00", style=Pango.Style.ITALIC
        )

        self.obj_tree = obj_tree = Gtk.TreeView()
        self.obj_store = obj_store = Gtk.ListStore(
            str, int, int, int, int, int, int, str
        )
        obj_tree.set_model(obj_store)
        obj_selection = obj_tree.get_selection()
        obj_selection.set_mode(Gtk.SelectionMode.BROWSE)
        obj_tree.set_size_request(200, -1)

        columns = [
            Gtk.TreeViewColumn(
                title="Count", cell_renderer=Gtk.CellRendererText(), text=1, style=2
            ),
            Gtk.TreeViewColumn(
                title="%", cell_renderer=Gtk.CellRendererText(), text=2, style=2
            ),
            Gtk.TreeViewColumn(
                title="Size", cell_renderer=Gtk.CellRendererText(), text=3, style=2
            ),
            Gtk.TreeViewColumn(
                title="%", cell_renderer=Gtk.CellRendererText(), text=4, style=2
            ),
            Gtk.TreeViewColumn(
                title="Cumulative",
                cell_renderer=Gtk.CellRendererText(),
                text=5,
                style=2,
            ),
            Gtk.TreeViewColumn(
                title="Max", cell_renderer=Gtk.CellRendererText(), text=6, style=2
            ),
            Gtk.TreeViewColumn(
                title="Kind", cell_renderer=Gtk.CellRendererText(), text=7, style=2
            ),
        ]

        first_iter = obj_store.get_iter_first()
        if first_iter is not None:
            obj_selection.select_iter(first_iter)

        obj_selection.connect("changed", self.obj_selection_cb, obj_store)
        obj_tree.connect("row_activated", self.obj_row_activated_cb, obj_store)

        for i, column in enumerate(columns):
            column.set_sort_column_id(i + 1)
            obj_tree.append_column(column)

        obj_tree.collapse_all()
        obj_tree.set_headers_visible(True)

        scrolled_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add(obj_tree)

        hbox = Gtk.VBox(homogeneous=False, spacing=0)

        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.INFO)
        self.obj_totals = Gtk.Label()
        bar.get_content_area().pack_start(self.obj_totals, False, False, 0)
        hbox.pack_start(bar, False, False, 0)

        hbox.pack_start(scrolled_window, True, True, 0)
        (text_widget, obj_buffer) = self.create_text(False)
        self.obj_buffer = obj_buffer
        hbox.pack_end(text_widget, True, True, 0)

        notebook.append_page(hbox, Gtk.Label.new_with_mnemonic("_Objects"))

        (shell_view, shell_widget, shell_buffer) = self.create_text(
            False, return_view=True
        )
        self.shell_view = shell_view
        self.shell_buffer = shell_buffer
        self.shell_widget = shell_widget
        shell_hbox = Gtk.VBox()
        shell_hbox.pack_start(shell_widget, True, True, 0)
        shell_bottom = Gtk.HBox()

        shell_prompt = Gtk.Entry()
        self.shell_prompt = shell_prompt
        self.shell_prompt.connect("activate", self.run_shell_command)
        shell_bottom.pack_start(shell_prompt, True, True, 0)

        self.shell_button = shell_button = Gtk.Button("Run")
        shell_button.connect("clicked", self.run_shell_command)
        shell_bottom.pack_start(shell_button, False, False, 0)
        shell_hbox.pack_end(shell_bottom, False, False, 0)

        shell_label = Gtk.Label.new_with_mnemonic("_Shell")
        notebook.append_page(shell_hbox, shell_label)

        # To try and grab focus of our text input
        notebook.connect("switch-page", self.switch_page)
        self.notebook = notebook

        graph_vbox = Gtk.VBox()
        graph_spinner_box = Gtk.HBox(False, 0)
        graph_vbox.pack_start(graph_spinner_box, False, False, 0)

        label = Gtk.Label("Sample size(seconds): ")
        label.set_alignment(0, 0.5)
        graph_spinner_box.pack_start(label, False, False, 0)

        adj = Gtk.Adjustment(1.0, 1.0, 60.0, 1.0, 5.0, 0.0)
        self.spinner = spinner = Gtk.SpinButton()
        spinner.configure(adj, 0, 0)
        spinner.set_wrap(False)
        graph_spinner_box.pack_start(spinner, False, False, 0)

        self.spinner_button = spinner_button = Gtk.Button("Go")
        spinner_button.connect("clicked", self.sample_call_tree)
        graph_spinner_box.pack_start(spinner_button, False, False, 0)

        scrolled_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.ALWAYS, Gtk.PolicyType.ALWAYS)

        self.call_graph = Gtk.Image()
        scrolled_window.add_with_viewport(self.call_graph)

        graph_vbox.pack_start(scrolled_window, True, True, 0)
        notebook.append_page(graph_vbox, Gtk.Label.new_with_mnemonic("_Call Graph"))

        self.details_html = ""
        self.details_view = WebKit.WebView()
        self.details_view.load_html(self.details_html, None)

        details_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        details_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        details_window.add(self.details_view)
        notebook.append_page(details_window, Gtk.Label.new_with_mnemonic("_Details"))

        self.show_all()
        self.progress.hide()

        # Load up our javascript resources
        js = join(dirname(abspath(__file__)), "js")
        if not os.path.isdir(js):
            js = "/usr/lib/javascript/"
        with open(join(js, "jquery-1.7.1.min.js")) as jquery_file:
            self.jquery_js = jquery_file.read()
        with open(join(js, "jquery.sparkline.min.js")) as sparkline_file:
            self.jquery_sparkline_js = sparkline_file.read()

    def sample_call_tree(self, widget):
        self.generate_callgraph(sample_size=self.spinner.get_value())

    def switch_page(self, notebook, page, pagenum):
        name = self.notebook.get_tab_label(self.notebook.get_nth_page(pagenum))
        if name.get_text() == "Shell":
            GObject.timeout_add(0, self.shell_prompt.grab_focus)

    def run_shell_command(self, widget):
        if getattr(self, "proc", None) is None or self.proc.sock is None:
            return
        cmd = self.shell_prompt.get_text()
        end = self.shell_buffer.get_end_iter()
        self.shell_buffer.insert(end, "\n>>> %s\n" % cmd)
        log.debug("run_shell_command(%r)" % cmd)
        output = self.proc.cmd(cmd) or ""
        log.debug(repr(output))
        self.shell_buffer.insert(end, output)
        self.shell_prompt.set_text("")

        insert_mark = self.shell_buffer.get_insert()
        self.shell_buffer.place_cursor(self.shell_buffer.get_end_iter())
        self.shell_view.scroll_to_mark(insert_mark, 0.0, True, 0.0, 1.0)

    def obj_selection_cb(self, selection, model):
        sel = selection.get_selected()
        treeiter = sel[1]
        if treeiter is None:
            return
        address = model.get_value(treeiter, 0)
        value = pyrasite.inspect(self.pid, address, timeout=5)
        if value:
            self.obj_buffer.set_text(value)
            self.obj_buffer.place_cursor(self.obj_buffer.get_start_iter())
        elif _ptrace_restricted():
            self.obj_buffer.set_text(
                "Unable to inspect object because gdb cannot attach.\n\n"
                + _ptrace_hint()
            )
        else:
            self.obj_buffer.set_text(
                "Unable to inspect object: gdb timed out or could not read "
                "the interpreter's memory. The process may be busy, or its "
                "Python build may not expose symbols to gdb."
            )

    def obj_row_activated_cb(self, *args, **kw):
        log.debug("obj_row_activated_cb(%s, %s)" % (args, kw))

    def generate_description(self, title):
        p = psutil.Process(self.proc.pid)

        self.info_html = """
        <html><head>
            <style>
            body {font: normal 12px/150%% Arial, Helvetica, sans-serif;}
            .grid table {
                border-collapse: collapse;
                text-align: left;
                width: 100%%;
            }
            .grid {
                font: normal 12px/150%% Arial, Helvetica, sans-serif;
                background: #fff; overflow: hidden; border: 1px solid #2e3436;
                -webkit-border-radius: 3px; border-radius: 3px;
            }
            .grid table td, .grid table th { padding: 3px 10px; }
            .grid table thead th {
                background:-webkit-gradient(linear, left top, left bottom,
                                            color-stop(0.05, #888a85),
                                            color-stop(1, #555753) );
                background-color:#2e3436; color:#FFFFFF; font-size: 15px;
                font-weight: bold; border-left: 1px solid #2e3436; }
            .grid table thead th:first-child { border: none; }
            .grid table tbody td {
                color: #2e3436;
                border-left: 1px solid #2e3436;
                font-size: 12px;
                font-weight: normal;
            }
            .grid table tbody .alt td { background: #d3d7cf; color: #2e3436; }
            .grid table tbody td:first-child { border: none; }
            </style>
        </head>
        <body>
            <h2 id="proc_title">%(title)s</h2>
                <div class="grid">
                <table>
                    <thead><tr>
                        <th width="50%%">CPU: <span id="cpu_details"/></th>
                        <th width="50%%">Memory: <span id="mem_details"/></th>
                    </tr></thead>
                    <tbody>
                        <tr>
                            <td>
                                <span id="cpu_graph" class="cpu_graph"></span>
                            </td>
                            <td>
                                <span id="mem_graph" class="mem_graph"></span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <br/>
            <div class="grid">
                <table>
                    <thead><tr>
                        <th width="50%%">Read: <span id="read_details"/></th>
                        <th width="50%%">Write: <span id="write_details"/></th>
                    </tr></thead>
                    <tbody>
                        <tr><td><span id="read_graph"></span></td>
                            <td><span id="write_graph"></span></td></tr>
                    </tbody>
                </table>
            </div>
            <br/>
            <div class="grid">
                <table>
                    <thead>
                        <tr><th>Threads</th></tr>
                    </thead>
                    <tbody>
                        <tr><td><span id="thread_graph"></span></td></tr>
                    </tbody>
                </table>
            </div>
            <br/>
        """ % dict(title=self.proc.title)

        global process_title, process_status
        process_title = self.proc.title
        process_status = ""

        self.info_html += """
        <div class="grid">
            <table>
                <thead><tr><th>Open Files</th></tr></thead>
                <tbody id="open_files"></tbody>
            </table>
        </div>
        <br/>
        <div class="grid">
            <table>
                <thead><tr><th colspan="4">Connections</th></tr></thead>
                <tbody id="open_connections"></tbody>
            </table>
        </div>
        </body></html>
        """

        self.info_view.load_html(self.info_html, None)

        # The Details tab
        self.details_html = """
        <style>
        body {font: normal 12px/150%% Arial, Helvetica, sans-serif;}
        </style>
        <h2>%s</h2>
        <ul>
            <li><b>status:</b> %s</li>
            <li><b>cwd:</b> %s</li>
            <li><b>cmdline:</b> %s</li>
            <li><b>terminal:</b> %s</li>
            <li><b>created:</b> %s</li>
            <li><b>username:</b> %s</li>
            <li><b>uid:</b> %s</li>
            <li><b>gid:</b> %s</li>
            <li><b>nice:</b> %s</li>
        </ul>
        """ % (
            self.proc.title,
            _process_value(p, "status", default="?"),
            _process_value(p, "cwd", "getcwd", default="?"),
            _format_cmdline(_process_value(p, "cmdline", default=[])),
            getattr(p, "terminal", "unknown"),
            _format_time(_process_value(p, "create_time")),
            _process_value(p, "username", default="?"),
            getattr(_process_value(p, "uids"), "real", "?"),
            getattr(_process_value(p, "gids"), "real", "?"),
            _process_value(p, "nice", default="?"),
        )

        self.details_view.load_html(self.details_html, None)

        if not self.resource_thread:
            self.resource_thread = ResourceUsagePoller(self.proc.pid)
            self.resource_thread.daemon = True
            self.resource_thread.info_view = self.info_view
            self.resource_thread.start()
        self.resource_thread.process = p

        self._js_injected = False
        GObject.timeout_add(int(POLL_INTERVAL * 1000), self.render_resource_usage)

    def on_info_load_changed(self, view, event):
        if event == WebKit.LoadEvent.FINISHED and not self._js_injected:
            self.inject_js()

    def inject_js(self):
        if self._js_injected:
            return
        log.debug("Injecting jQuery")
        script = self.jquery_js + "\n" + self.jquery_sparkline_js
        self.info_view.run_javascript(script, None, self.on_js_loaded, None)

    def on_js_loaded(self, view, result, user_data):
        self._js_injected = True
        self.render_resource_usage()

    def render_resource_usage(self):
        """
        Render our resource usage using jQuery+Sparklines in our WebKit view
        """
        global cpu_intervals, mem_intervals, cpu_details, mem_details
        global read_intervals, write_intervals, read_bytes, write_bytes
        global open_files, open_connections
        global process_title, process_status
        if not self._js_injected:
            return True
        script = """
            jQuery('#cpu_graph').sparkline(%s, {'height': 75, 'width': 250,
                spotRadius: 3, fillColor: '#73d216', lineColor: '#4e9a06'});
            jQuery('#mem_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#5c3566', fillColor: '#75507b',
                minSpotColor: false, maxSpotColor: false, spotColor: '#f57900',
                spotRadius: 3});
            jQuery('#cpu_details').text('%s');
            jQuery('#mem_details').text('%s');
            jQuery('#read_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#a40000', fillColor: '#cc0000',
                minSpotColor: false, maxSpotColor: false, spotColor: '#729fcf',
                spotRadius: 3});
            jQuery('#write_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#ce5c00', fillColor: '#f57900',
                minSpotColor: false, maxSpotColor: false, spotColor: '#8ae234',
                spotRadius: 3});
            jQuery('#read_details').text('%s');
            jQuery('#write_details').text('%s');
        """ % (
            cpu_intervals,
            mem_intervals,
            cpu_details,
            mem_details,
            read_intervals,
            write_intervals,
            humanize_bytes(read_bytes),
            humanize_bytes(write_bytes),
        )

        for i, thread in enumerate(thread_intervals):
            script += """
                jQuery('#thread_graph').sparkline(%s, {
                    %s'lineColor': '#%s', 'fillColor': false, 'spotRadius': 3,
                    'spotColor': '#%s'});
            """ % (
                thread_intervals[thread],
                (i != 0 and "'composite': true,") or "'height': 75, 'width': 575,",
                thread_colors[thread],
                thread_colors[thread],
            )

        if open_files:
            script += """
                jQuery('#open_files').html('%s');
            """ % "".join(
                [
                    "<tr%s><td>%s</td></tr>"
                    % ((i % 2 and ' class="alt"') or "", open_file)
                    for i, open_file in enumerate(open_files)
                ]
            )

        if open_connections:
            row = "<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            script += """
                jQuery('#open_connections').html('%s');
            """ % "".join(
                [
                    row
                    % (
                        (i % 2 and ' class="alt"') or "",
                        conn["type"],
                        conn["local"],
                        conn["remote"],
                        conn["status"],
                    )
                    for i, conn in enumerate(open_connections)
                ]
            )

        script += """
            jQuery('#proc_title').text('%s %s');
        """ % (str(process_title).strip(), process_status)
        self.info_view.run_javascript(script)
        return True

    def update_progress(self, fraction, text=None):
        if text:
            self.progress.set_text(text + "...")
            self.progress.set_show_text(True)
        if fraction:
            self.progress.set_fraction(fraction)
        else:
            self.progress.pulse()
        while Gtk.events_pending():
            Gtk.main_iteration()

    def selection_cb(self, selection, model):
        sel = selection.get_selected()
        if sel[1] is None:
            return

        self.progress.show()
        self.update_progress(0.1, "Analyzing process")

        treeiter = sel[1]
        title = model.get_value(treeiter, 0)
        proc = model.get_value(treeiter, 1)
        self.proc = proc

        if self.pid and proc.pid != self.pid:
            global \
                cpu_intervals, \
                mem_intervals, \
                write_intervals, \
                read_intervals, \
                cpu_details, \
                mem_details, \
                read_count, \
                read_bytes, \
                thread_totals, \
                write_count, \
                write_bytes, \
                thread_intervals, \
                thread_colors, \
                open_files, \
                open_connections
            cpu_intervals = [0.0]
            mem_intervals = []
            write_intervals = []
            read_intervals = []
            cpu_details = mem_details = ""
            read_count = read_bytes = write_count = write_bytes = 0
            thread_intervals = {}
            thread_colors = {}
            thread_totals = {}
            open_connections = []
            open_files = []

        self.pid = proc.pid

        # Analyze the process
        self.generate_description(title)

        # Inject a reverse subshell
        self.update_progress(0.2, "Injecting reverse connection")
        if proc.title not in self.processes:
            try:
                proc.connect()
            except Exception as exc:
                detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                log.debug("Unable to connect to %s", proc.title.strip(), exc_info=True)
                log.warning("Unable to connect to %s: %s", proc.title.strip(), detail)
                message = "Unable to inject into %s:\n\n%s" % (
                    proc.title.strip(),
                    detail,
                )
                if _ptrace_restricted():
                    message += "\n\n" + _ptrace_hint()
                elif "No symbol table is loaded" in str(exc) or "No symbol " in str(
                    exc
                ):
                    message += (
                        "\n\nThis interpreter does not expose Python symbols to "
                        "gdb. It may be statically linked, stripped, or not be "
                        "CPython. Try a distribution-provided CPython build."
                    )
                message += "\n\nSee the terminal for the full gdb output."
                self.shell_buffer.set_text(message)
                self.progress.hide()
                return
            self.processes[proc.title] = proc

        # Dump objects and load them into our store
        self.update_progress(0.3, "Dumping all objects")
        self.dump_objects()

        # Shell
        self.update_progress(0.5, "Determining Python version")
        self.shell_buffer.set_text(
            proc.cmd('import sys; print("Python " + sys.version)')
        )

        # Dump stacks
        self.dump_stacks()

        ## Call Stack
        self.generate_callgraph(1, True)

        self.fontify()
        self.update_progress(1.0)
        self.progress.hide()
        self.update_progress(0.0)

    def dump_objects(self):
        self.obj_store.clear()
        if loader is not None:
            try:
                if self._dump_objects_with_meliae():
                    return
            except Exception:
                log.debug("meliae failed; using built-in summary", exc_info=True)
        self._dump_objects_builtin()

    def _dump_objects_with_meliae(self):
        tmp = os.path.join(TEMP_DIR, str(self.proc.pid))
        objects_path = tmp + ".objects"
        cmd = "\n".join(
            [
                "import os, shutil",
                "from meliae import scanner",
                "tmp = %r" % tmp,
                "scanner.dump_all_objects(tmp + '.json')",
                "shutil.move(tmp + '.json', tmp + '.objects')",
                "",
            ]
        )
        output = self.proc.cmd(cmd) or ""
        if "meliae" in output and "No module named" in output:
            log.info("target cannot import meliae; using built-in summary")
            return False
        self.update_progress(0.35)
        self.update_progress(0.4, "Loading object dump")

        try:
            try:
                objects = loader.load(objects_path, show_prog=False)
            except Exception:
                log.debug("Falling back to slower meliae object dump loader")
                objects = loader.load(objects_path, show_prog=False, using_json=False)
            objects.compute_referrers()
            self.update_progress(0.45)
            summary = objects.summarize()
            self.update_progress(0.47)

            def intify(x):
                try:
                    return int(x)
                except (TypeError, ValueError):
                    return x

            for i, line in enumerate(str(summary).split("\n")):
                if i == 0:
                    self.obj_totals.set_text(line)
                elif i == 1:
                    continue  # column headers
                else:
                    obj = summary.summaries[i - 2]
                    self.obj_store.append(
                        [str(obj.max_address), *map(intify, line.split()[1:])]
                    )
            return True
        except Exception:
            log.debug("Unable to load meliae object dump", exc_info=True)
            return False
        finally:
            with contextlib.suppress(OSError):
                os.unlink(objects_path)

    def _dump_objects_builtin(self):
        self.update_progress(0.35, "Scanning objects")
        marker = "__PYRASITE_OBJECTS__"
        code = "\n".join(
            [
                "import gc, json, sys",
                "stats = {}",
                "for obj in gc.get_objects():",
                "    try:",
                "        name = type(obj).__name__",
                "        size = sys.getsizeof(obj)",
                "    except Exception:",
                "        continue",
                "    entry = stats.get(name)",
                "    if entry is None:",
                "        stats[name] = [1, size, size, id(obj)]",
                "    else:",
                "        entry[0] += 1",
                "        entry[1] += size",
                "        if size > entry[2]:",
                "            entry[2] = size",
                "            entry[3] = id(obj)",
                "total_count = sum(entry[0] for entry in stats.values())",
                "total_size = sum(entry[1] for entry in stats.values())",
                "items = sorted(stats.items(),",
                "               key=lambda item: item[1][1],",
                "               reverse=True)[:250]",
                "print(%r + json.dumps({'total_count': total_count,"
                " 'total_size': total_size, 'items': items}))" % marker,
                "",
            ]
        )
        output = self.proc.cmd(code) or ""
        if marker not in output:
            message = "Unable to collect object information"
            self.obj_totals.set_text(message)
            log.warning("%s: %s", message, output.strip()[:500])
            return
        try:
            data = json.loads(output.split(marker, 1)[1].strip())
        except json.JSONDecodeError:
            message = "Unable to parse object information"
            self.obj_totals.set_text(message)
            log.warning(message, exc_info=True)
            return

        total_count = data["total_count"]
        total_size = data["total_size"]
        self.obj_totals.set_text(
            "%d objects, %s total (top 250 types shown)"
            % (total_count, humanize_bytes(total_size))
        )
        for name, (count, size, max_size, address) in data["items"]:
            count_pct = int(count * 100 / total_count) if total_count else 0
            size_pct = int(size * 100 / total_size) if total_size else 0
            self.obj_store.append(
                [str(address), count, count_pct, size, size_pct, size, max_size, name]
            )
        self.update_progress(0.47)

    def dump_stacks(self):
        self.update_progress(0.55, "Dumping stacks")
        payloads = os.path.join(
            os.path.abspath(os.path.dirname(pyrasite.__file__)), "payloads"
        )
        dump_stacks = os.path.join(payloads, "dump_stacks.py")
        with open(dump_stacks) as payload:
            code = self.proc.cmd(payload.read())
        self.update_progress(0.6)

        self.source_buffer.set_text("")
        start = self.source_buffer.get_iter_at_offset(0)
        end = start.copy()
        self.source_buffer.insert(end, code or "")

    def generate_callgraph(self, sample_size=1, show_progress=False):
        if shutil.which("dot") is None:
            log.warning("graphviz dot not found; call graph unavailable")
            return

        if show_progress:
            self.update_progress(0.7, "Tracing call stack for %d seconds" % sample_size)

        image = os.path.join(TEMP_DIR, "%d-callgraph.png" % self.proc.pid)
        image_literal = repr(image)
        start_code = "\n".join(
            [
                "try:",
                "    import pycallgraph",
                "except ImportError:",
                "    from pycallgraph2 import PyCallGraph",
                "    from pycallgraph2.output import GraphvizOutput",
                "    __pyrasite_callgraph = PyCallGraph(",
                "        output=GraphvizOutput(output_file=%s))" % image_literal,
                "    __pyrasite_callgraph.start()",
                "else:",
                "    pycallgraph.start_trace()",
                "    __pyrasite_callgraph = None",
                "",
            ]
        )
        out = self.proc.cmd(start_code) or ""
        if "No module named" in out or "Traceback" in out:
            log.warning(out)
            return

        if show_progress:
            self.update_progress(0.8)

        time.sleep(sample_size)

        if show_progress:
            self.update_progress(0.9, "Generating call stack graph")

        stop_code = "\n".join(
            [
                "try:",
                "    _callgraph = __pyrasite_callgraph",
                "except NameError:",
                "    pass",
                "else:",
                "    if _callgraph is None:",
                "        pycallgraph.make_dot_graph(%s)" % image_literal,
                "    else:",
                "        _callgraph.stop()",
                "        del __pyrasite_callgraph",
                "",
            ]
        )
        out = self.proc.cmd(stop_code) or ""
        if out:
            log.debug(out)
        if os.path.exists(image):
            self.call_graph.set_from_file(image)
        else:
            log.warning("Call graph image was not created")

    def row_activated_cb(self, view, path, col, store):
        iter = store.get_iter(path)
        proc = store.get_value(iter, 1)
        if proc is not None:
            store.set_value(iter, 2, Pango.Style.NORMAL)

    def create_tree(self):
        tree_store = ProcessListStore()
        tree_view = Gtk.TreeView()
        self.tree_view = tree_view
        tree_view.set_model(tree_store)
        selection = tree_view.get_selection()
        selection.set_mode(Gtk.SelectionMode.BROWSE)
        tree_view.set_size_request(200, -1)

        cell = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(
            title="Processes", cell_renderer=cell, text=0, style=2
        )

        first_iter = tree_store.get_iter_first()
        if first_iter is not None:
            selection.select_iter(first_iter)

        selection.connect("changed", self.selection_cb, tree_store)
        tree_view.connect("row_activated", self.row_activated_cb, tree_store)

        tree_view.append_column(column)

        tree_view.collapse_all()
        tree_view.set_headers_visible(False)
        scrolled_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        scrolled_window.add(tree_view)

        label = Gtk.Label(label="Processes")

        box = Gtk.Notebook()
        box.set_size_request(250, -1)
        box.append_page(scrolled_window, label)

        tree_view.grab_focus()

        return box

    def create_text(self, is_source, return_view=False):
        scrolled_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_shadow_type(Gtk.ShadowType.IN)

        text_view = Gtk.TextView()
        buffer = Gtk.TextBuffer()

        text_view.set_buffer(buffer)
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)

        scrolled_window.add(text_view)

        if is_source:
            font_desc = Pango.FontDescription("monospace")
            text_view.modify_font(font_desc)
            text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        else:
            text_view.set_wrap_mode(Gtk.WrapMode.WORD)
            text_view.set_pixels_above_lines(2)
            text_view.set_pixels_below_lines(2)

        if return_view:
            return (text_view, scrolled_window, buffer)
        return (scrolled_window, buffer)

    def fontify(self):
        start_iter = self.source_buffer.get_iter_at_offset(0)
        end_iter = self.source_buffer.get_iter_at_offset(0)
        data = self.source_buffer.get_text(
            self.source_buffer.get_start_iter(),
            self.source_buffer.get_end_iter(),
            False,
        )

        builtin_constants = ["None", "True", "False"]
        is_decorator = False
        is_func = False

        def prepare_iters():
            start_iter.set_line(srow - 1)
            start_iter.set_line_offset(scol)
            end_iter.set_line(erow - 1)
            end_iter.set_line_offset(ecol)

        try:
            for x in tokenize.tokenize(io.BytesIO(data.encode("utf-8")).readline):
                # x has 5-tuples
                tok_type, tok_str = x[0], x[1]
                srow, scol = x[2]
                erow, ecol = x[3]

                if tok_type == tokenize.COMMENT:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name(
                        "comment", start_iter, end_iter
                    )
                elif tok_type == tokenize.NAME:
                    if tok_str in keyword.kwlist or tok_str in builtin_constants:
                        prepare_iters()
                        self.source_buffer.apply_tag_by_name(
                            "keyword", start_iter, end_iter
                        )
                        if tok_str == "def" or tok_str == "class":
                            # Next token is going to be a
                            # function/method/class name
                            is_func = True
                            continue
                    elif tok_str == "self":
                        prepare_iters()
                        self.source_buffer.apply_tag_by_name(
                            "italic", start_iter, end_iter
                        )
                    else:
                        if is_func is True:
                            prepare_iters()
                            self.source_buffer.apply_tag_by_name(
                                "bold", start_iter, end_iter
                            )
                        elif is_decorator is True:
                            prepare_iters()
                            self.source_buffer.apply_tag_by_name(
                                "decorator", start_iter, end_iter
                            )
                elif tok_type == tokenize.STRING:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name("string", start_iter, end_iter)
                elif tok_type == tokenize.NUMBER:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name("number", start_iter, end_iter)
                elif tok_type == tokenize.OP and tok_str == "@":
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name(
                        "decorator", start_iter, end_iter
                    )

                    # next token is going to be the decorator name
                    is_decorator = True
                    continue

                if is_func is True:
                    is_func = False

                if is_decorator is True:
                    is_decorator = False
        except tokenize.TokenError:
            pass

    def close(self):
        self.progress.show()
        self.update_progress(None, "Shutting down")
        log.debug("Closing %r" % self)
        for process in self.processes.values():
            self.update_progress(None)
            process.close()
            callgraph = os.path.join(TEMP_DIR, "%d-callgraph.png" % process.pid)
            if os.path.exists(callgraph):
                os.unlink(callgraph)


##
## Background Threads
##


class ResourceUsagePoller(threading.Thread):
    """A thread for polling a processes CPU & memory usage"""

    process = None

    def __init__(self, pid):
        super().__init__()
        self.process = psutil.Process(pid)

    def run(self):
        while True:
            try:
                if self.process:
                    for poll in (
                        self.poll_cpu,
                        self.poll_mem,
                        self.poll_io,
                        self.poll_threads,
                        self.poll_connections,
                        self.poll_files,
                    ):
                        with contextlib.suppress(psutil.AccessDenied):
                            poll()
                else:
                    time.sleep(1)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                log.warning("Lost Process")
                self.process = None
                global process_status
                process_status = "[Terminated]"
            except Exception:
                log.exception("Error while polling process")
                time.sleep(1)

    def poll_cpu(self):
        global cpu_intervals, cpu_details
        if len(cpu_intervals) >= INTERVALS:
            cpu_intervals = cpu_intervals[1:]
        cpu_intervals.append(self.process.cpu_percent(interval=POLL_INTERVAL))
        cputimes = self.process.cpu_times()
        cpu_details = "%0.2f%% (%s user, %s system)" % (
            cpu_intervals[-1],
            cputimes.user,
            cputimes.system,
        )

    def poll_mem(self):
        global mem_intervals, mem_details
        if len(mem_intervals) >= INTERVALS:
            mem_intervals = mem_intervals[1:]
        meminfo = self.process.memory_info()
        mem_intervals.append(meminfo.rss)
        mem_details = "%0.2f%% (%s RSS, %s VMS)" % (
            self.process.memory_percent(),
            humanize_bytes(meminfo.rss),
            humanize_bytes(meminfo.vms),
        )

    def poll_io(self):
        global read_count, read_bytes, write_count, write_bytes
        global read_intervals, write_intervals
        if len(read_intervals) >= INTERVALS:
            read_intervals = read_intervals[1:]
            write_intervals = write_intervals[1:]
        io = self.process.io_counters()
        read_since_last = io.read_bytes - read_bytes
        read_intervals.append(read_since_last)
        read_count = io.read_count
        read_bytes = io.read_bytes
        write_since_last = io.write_bytes - write_bytes
        write_intervals.append(write_since_last)
        write_count = io.write_count
        write_bytes = io.write_bytes

    def poll_threads(self):
        global thread_intervals
        for thread in self.process.threads():
            if thread.id not in thread_intervals:
                thread_intervals[thread.id] = []
                thread_colors[thread.id] = get_color()
                thread_totals[thread.id] = 0.0

            if len(thread_intervals[thread.id]) >= INTERVALS:
                thread_intervals[thread.id] = thread_intervals[thread.id][1:INTERVALS]

            # FIXME: we should figure out some way to visually
            # distinguish between user and system time.
            total = thread.system_time + thread.user_time
            amount_since = total - thread_totals[thread.id]
            thread_intervals[thread.id].append(float("%.2f" % amount_since))
            thread_totals[thread.id] = total

    def poll_connections(self):
        global open_connections
        connections = []
        if hasattr(self.process, "net_connections"):
            conns = self.process.net_connections()
        else:
            conns = self.process.connections()
        for conn in conns:
            if conn.type == socket.SOCK_STREAM:
                kind = "TCP"
            elif conn.type == socket.SOCK_DGRAM:
                kind = "UDP"
            else:
                kind = "UNIX"
            connections.append(
                {
                    "type": kind,
                    "status": conn.status,
                    "local": _format_address(conn.laddr),
                    "remote": _format_address(conn.raddr),
                }
            )
        open_connections = connections

    def poll_files(self):
        global open_files
        files = []
        for open_file in self.process.open_files():
            files.append(open_file.path)
        open_files = files


##
## Utilities
##


def get_color():
    """Prefer tango colors for our lines. Fall back to random ones."""
    tango = [
        "c4a000",
        "ce5c00",
        "8f5902",
        "4e9a06",
        "204a87",
        "5c3566",
        "a40000",
        "555753",
    ]
    used = thread_colors.values()
    for color in tango:
        if color not in used:
            return color
    return "".join([hex(randrange(0, 255))[2:] for i in range(3)])


def humanize_bytes(nbytes, precision=1):
    """Return a humanized string representation of a number of bytes.
    http://code.activestate.com/recipes/577081-humanized-representation-of-a-number-of-bytes/
    """
    abbrevs = (
        (1 << 50, "PB"),
        (1 << 40, "TB"),
        (1 << 30, "GB"),
        (1 << 20, "MB"),
        (1 << 10, "kB"),
        (1, "bytes"),
    )
    if nbytes == 1:
        return "1 byte"
    factor, suffix = abbrevs[-1]
    for candidate_factor, candidate_suffix in abbrevs:
        if nbytes >= candidate_factor:
            factor, suffix = candidate_factor, candidate_suffix
            break
    return "%.*f %s" % (precision, nbytes / factor, suffix)


def setup_logger(verbose=False):
    """Based on code from Will Maier's 'ideal Python script'.
    https://github.com/wcmaier/python-script
    """
    # NullHandler was added in Python 3.1.
    try:
        NullHandler = logging.NullHandler
    except AttributeError:

        class NullHandler(logging.Handler):
            def emit(self, record):
                pass

    # Add a do-nothing NullHandler to the module logger to prevent "No handlers
    # could be found" errors. The calling code can still add other, more useful
    # handlers, or otherwise configure logging.
    log = logging.getLogger("pyrasite")
    log.addHandler(NullHandler())

    level = logging.INFO
    if verbose:
        level = logging.DEBUG

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)
    log.addHandler(handler)
    log.setLevel(level)

    return log


def check_depends():
    if shutil.which("dot") is None:
        print(
            "WARNING: graphviz dot command not found. Call graph will not be available"
        )


def show_ptrace_warning(window):
    if not _ptrace_restricted():
        return
    dialog = Gtk.MessageDialog(
        transient_for=window,
        modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.OK,
        text="gdb cannot attach to other processes",
    )
    dialog.format_secondary_text(_ptrace_hint())
    dialog.run()
    dialog.destroy()


def main():
    check_depends()

    mainloop = GLib.MainLoop()

    window = PyrasiteWindow()
    window.show()
    show_ptrace_warning(window)

    def quit(widget, event, mainloop):
        window.close()
        mainloop.quit()

    window.connect("delete-event", quit, mainloop)

    try:
        mainloop.run()
    except KeyboardInterrupt:
        window.close()
        mainloop.quit()


if __name__ == "__main__":
    setup_logger(verbose="-v" in sys.argv)
    log.info("Loading Pyrasite...")
    sys.exit(main())

# vim: tabstop=4 shiftwidth=4 expandtab
