from setuptools import setup, find_packages

version = '2.0beta4'

f = open('README.rst')
long_description = f.read().split('split here')[1]
f.close()

try:
    from gi.repository import GLib, GObject, Pango, Gtk, WebKit
except ImportError:
    print "Unable to find pygobject3. Please install the 'pygobject3' "
    print "package on Fedora, or 'python-gobject-dev on Ubuntu."
    sys.exit(1)
try:
    from meliae import version_info
except ImportError:
    print "We require meliae to be installed."
    exit(1)

setup(name='pyrasite-gui',
      version=version,
      description="Inject code into a running Python process",
      long_description=long_description,
      keywords='debugging injection runtime',
      author='Luke Macken',
      author_email='lmacken@redhat.com',
      url='http://pyrasite.fedorahosted.org',
      license='GPLv3',
      packages=find_packages(exclude=['ez_setup', 'examples', 'tests']),
      include_package_data=True,
      zip_safe=False,
      install_requires=[
        "Cython", # Needed for meliae
        "meliae",
        "pycallgraph",
        "psutil",
        "Sphinx",
      ],
      tests_require=['nose'],
      test_suite='nose.collector',
      entry_points="""
          [console_scripts]
          pyrasite-gui = pyrasite.tools.gui:main
      """,
      classifiers=[
          'Development Status :: 4 - Beta',
          'Environment :: Console',
          'Intended Audience :: Developers',
          'Intended Audience :: System Administrators',
          'License :: OSI Approved :: GNU General Public License (GPL)',
          'Topic :: System :: Monitoring',
          'Topic :: Software Development :: Debuggers',
      ],
      )
