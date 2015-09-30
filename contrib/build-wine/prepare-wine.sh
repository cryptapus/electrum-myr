#!/bin/bash

# Please update these links carefully, some versions won't work under Wine
PYTHON_URL=http://www.python.org/ftp/python/2.7.8/python-2.7.8.msi
PYQT4_URL=http://sourceforge.net/projects/pyqt/files/PyQt4/PyQt-4.11.1/PyQt4-4.11.1-gpl-Py2.7-Qt4.8.6-x32.exe
PYWIN32_URL=http://sourceforge.net/projects/pywin32/files/pywin32/Build%20219/pywin32-219.win32-py2.7.exe/download
#PYINSTALLER_URL=https://pypi.python.org/packages/source/P/PyInstaller/PyInstaller-2.1.zip
PYINSTALLER_URL=http://downloads.sourceforge.net/project/pyinstaller/2.0/pyinstaller-2.0.zip
NSIS_URL=http://prdownloads.sourceforge.net/nsis/nsis-2.46-setup.exe?download
SETUPTOOLS_URL=https://pypi.python.org/packages/2.7/s/setuptools/setuptools-0.6c11.win32-py2.7.exe
NUMPY_URL=http://sourceforge.net/projects/numpy/files/NumPy/1.9.0/numpy-1.9.0-win32-superpack-python2.7.exe
#ZBAR_URL=http://sourceforge.net/projects/zbar/files/zbar/0.10/zbar-0.10-setup.exe/download

# These settings probably don't need change
export WINEPREFIX=/opt/wine-electrum
PYHOME=c:/python27
PYTHON="wine $PYHOME/python.exe -OO -B"

# Let's begin!
cd `dirname $0`
set -e

# Clean up Wine environment
echo "Cleaning $WINEPREFIX"
rm -rf $WINEPREFIX/*
echo "done"

echo "Cleaning tmp"
rm -rf tmp
mkdir -p tmp
echo "done"

cd tmp

# Install Python
wget -O python.msi "$PYTHON_URL"
wine msiexec /q /i python.msi

# Install PyWin32
wget -O pywin32.exe "$PYWIN32_URL"
wine pywin32.exe

# Install PyQt4
wget -O PyQt.exe "$PYQT4_URL"
wine PyQt.exe

#cp -r /electrum-wine/pyinstaller $WINEPREFIX/drive_c/
# Install pyinstaller
wget -O pyinstaller.zip "$PYINSTALLER_URL"
unzip pyinstaller.zip
#mv PyInstaller-2.1 $WINEPREFIX/drive_c/pyinstaller
mv pyinstaller-2.0 $WINEPREFIX/drive_c/pyinstaller

# Patch pyinstaller's DummyZlib
#patch $WINEPREFIX/drive_c/pyinstaller/PyInstaller/loader/archive.py < ../archive.patch

# Install ZBar
#wget -q -O zbar.exe "http://sourceforge.net/projects/zbar/files/zbar/0.10/zbar-0.10-setup.exe/download"
#wine zbar.exe

# Install setuptools
wget -O setuptools.exe "$SETUPTOOLS_URL"
wine setuptools.exe

# Install numpy
wget -O numpy.exe "$NUMPY_URL"
wine numpy.exe

# Fix Python27/Lib/random.py
#def ni(i): raise NotImplementedError
#import os
#os.urandom = ni

# Install dependencies
wine "$PYHOME\\Scripts\\easy_install.exe" ecdsa #zbar

# Install NSIS installer
wget -q -O nsis.exe "$NSIS_URL"
wine nsis.exe

# Install UPX
#wget -O upx.zip "http://upx.sourceforge.net/download/upx308w.zip"
#unzip -o upx.zip
#cp upx*/upx.exe .
