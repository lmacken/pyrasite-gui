pyrasite-gui
============

.. split here

A graphical interface that lets you easily analyze and introspect unaltered
running Python processes.

.. image:: http://lewk.org/img/pyrasite/pyrasite-info.png

.. image:: http://lewk.org/img/pyrasite/pyrasite-stacks.png

.. image:: http://lewk.org/img/pyrasite/pyrasite-objects.png

.. image:: http://lewk.org/img/pyrasite/pyrasite-callgraph.png

Requirements
~~~~~~~~~~~~

  - `Pyrasite <https://github.com/lmacken/pyrasite>`_
  - PyGObject3 Introspection bindings

    - Fedora: pygobject3
    - Ubuntu: python-gobject-dev
    - Arch: python2-gobject

  - WebKitGTK3

    - Fedora: webkitgtk3
    - Ubuntu: gir1.2-webkit-3.0
    - Arch: libwebkit3

  - `meliae <https://launchpad.net/meliae>`_
    - easy_install/pip may not work for this install. If not, use the tarball from the distribution website. You may need to install `Cython <http://cython.org>`_ in order to get meliae to build.

Mailing List
~~~~~~~~~~~~

https://fedorahosted.org/mailman/listinfo/pyrasite

IRC
~~~

#pyrasite on Freenode.

Authors
~~~~~~~

Luke Macken <lmacken@redhat.com>

.. image:: http://api.coderwall.com/lmacken/endorsecount.png
   :target: http://coderwall.com/lmacken
