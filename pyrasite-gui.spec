%global betaver beta7

Name:             pyrasite-gui
Version:          2.0
Release:          0.1.%{betaver}%{?dist}
Summary:          A graphical interface for monitoring and introspecting Python

Group:            Development/Tools
License:          GPLv3
URL:              http://pyrasite.com
Source0:          http://pypi.python.org/packages/source/p/%{name}/%{name}-%{version}%{betaver}.tar.gz

BuildArch:        noarch
BuildRequires:    python-devel
BuildRequires:    python-meliae
BuildRequires:    pygobject3

Requires:         pyrasite
Requires:         pygobject3
Requires:         webkitgtk3
Requires:         python-meliae
Requires:         python-pycallgraph
Requires:         jquery-sparkline

%description
Pyrasite uses gdb to inject code into a running Python process.  This package
contains a graphical interface for Pyrasite, which allows you to easily analyze
and introspect a running Python process.

%prep
%setup -q -n %{name}-%{version}%{betaver}

%build

%install
%{__install} -D -m 655 pyrasite-gui.py %{buildroot}%{_bindir}/pyrasite-gui

%files
%defattr(-,root,root,-)
%doc LICENSE
%{_bindir}/pyrasite-gui

%changelog
* Mon Mar 12 2012 Luke Macken <lmacken@redhat.com> 2.0-0.1.beta6
- Initial package for Fedora
