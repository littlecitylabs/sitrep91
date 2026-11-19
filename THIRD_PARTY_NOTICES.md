# Third-party notices

SITREP91 is MIT-licensed (see `LICENSE`). It started from, and the release zip
carries, the following other people's work.

## skyrc-mc-rs

https://github.com/rssdev10/skyrc-mc-rs. MIT License, full text below, exactly as
in that project's `LICENSE`.

This project started from skyrc-mc-rs's protocol notes (`docs/PROTOCOL.md`)
and its `charger-controller-cli`. Used here:

- the MC5000 packet framing and command set, as the starting point for
  `protocol.py`
- the init sequence, transcribed from `charger-controller-cli`
- the `0x57` device-info request, sent byte for byte as in PROTOCOL.md's
  worked example
- that example's FW 1.82 response, used as a test case during development

Everything specific to firmware 2.02 was worked out independently against a
real charger and its firmware image. That includes the 24-byte status frame,
per-slot stop, slot temperature over `0x55`, the alert codes and the config
range checks.

```
Copyright 2026 Roman Samarev

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## Bundled components

The release zip is self-contained: it carries its own Python and the libraries
below, unmodified, under `python\`. Each is under its own license, and the
license texts are in the zip. Where a component's package already carries its
license file, that file is left where the package put it
(`python\Lib\site-packages\<package>.dist-info\licenses\`); the rest are
in the `licenses\` folder beside this file.

| Component | Version | License | License text | Source |
|---|---|---|---|---|
| CPython runtime, and the software it incorporates (OpenSSL, expat, zlib, libmpdec, mimalloc, bzip2, libffi, SQLite, ...) | 3.13.14 | PSF License 2.0, plus each incorporated component's own | `python\LICENSE.txt` and `licenses\python-bundled-software.txt` | https://www.python.org/downloads/ |
| PySide6 (Qt for Python) | 6.11.1 | LGPL-3.0 | `licenses\LGPL-3.0.txt`, `licenses\GPL-3.0.txt` | https://code.qt.io/cgit/pyside/pyside-setup.git/ (tag v6.11.1) |
| Shiboken6 | 6.11.1 | LGPL-3.0 | `licenses\LGPL-3.0.txt`, `licenses\GPL-3.0.txt` | same repository |
| Qt (the `Qt6*.dll` files) | 6.11.1 | LGPL-3.0 | `licenses\LGPL-3.0.txt`, `licenses\GPL-3.0.txt` | https://download.qt.io/official_releases/qt/ (6.11.1) |
| NumPy, with its bundled OpenBLAS and other libraries | 2.5.2 | BSD-3-Clause, and the bundled libraries' own | `numpy-2.5.2.dist-info\licenses\LICENSE.txt` | https://github.com/numpy/numpy |
| pyqtgraph | 0.14.0 | MIT | `pyqtgraph-0.14.0.dist-info\licenses\LICENSE.txt` | https://github.com/pyqtgraph/pyqtgraph |
| bleak | 3.0.2 | MIT | `bleak-3.0.2.dist-info\licenses\LICENSE` | https://github.com/hbldh/bleak |
| winrt-runtime and the eight winrt-Windows-* packages | 3.2.1 | MIT | `licenses\winrt-MIT.txt` | https://github.com/pywinrt/pywinrt |
| colorama | 0.4.6 | BSD-3-Clause | `colorama-0.4.6.dist-info\licenses\LICENSE.txt` | https://github.com/tartley/colorama |
| typing_extensions | 4.16.0 | PSF License 2.0 | `typing_extensions-4.16.0.dist-info\licenses\LICENSE` | https://github.com/python/typing_extensions |
| Microsoft Visual C++ runtime (`msvcp140*.dll`, `vcruntime140*.dll`, ...) | | Microsoft's redistribution terms | | https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |

### Qt, PySide6 and Shiboken6 (LGPL-3.0)

SITREP91 uses Qt for Python (PySide6 and Shiboken6) and the Qt libraries under
the **GNU Lesser General Public License, version 3**. They are used
**unmodified** and **dynamically linked**: they are separate files that
SITREP91 loads when it runs, in `python\Lib\site-packages\PySide6\` and
`python\Lib\site-packages\shiboken6\`. Under LGPL-3.0 you may replace them
with another build of the same libraries; the program does not need to be
rebuilt. The full LGPL-3.0 text is `licenses\LGPL-3.0.txt`, and it refers to
the GPL-3.0 text, `licenses\GPL-3.0.txt`. The corresponding source for the
exact versions shipped is at the addresses in the table.

(The PySide6 and Shiboken6 packages' own `licenses` folders hold only Qt's
reference to its commercial license. That is not a license SITREP91 uses.)

The Qt libraries contain third-party code under other licenses (zlib, libpng,
FreeType, HarfBuzz, PCRE2 and others). **Their notices are not reproduced
here.** Qt publishes the list, with each component's license and copyright
holders, at https://doc.qt.io/qt-6/licenses-used-in-qt.html, and the source of
the exact Qt version shipped (6.11.1) is at https://download.qt.io. An earlier
build carried generic license templates for these with the copyright lines left
blank; they were removed because a template with no holder named is not the
notice those licenses ask for.

### opengl32sw.dll

`python\Lib\site-packages\PySide6\opengl32sw.dll` is the software OpenGL
renderer that ships with Qt: Mesa 3D (MIT, text in `licenses\Mesa-MIT.txt`,
https://docs.mesa3d.org/license.html) built with LLVM (Apache-2.0 with LLVM exceptions, text in
`licenses\LLVM-Apache-2.0-with-LLVM-exception.txt`). SITREP91 draws with
Qt's ordinary painting and does not need it.
