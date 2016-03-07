#!/bin/bash

# You probably need to update only this link
ELECTRUM_GIT_URL=git://github.com/cryptapus/electrum-myr.git
BRANCH=master
NAME_ROOT=electrum-myr


# These settings probably don't need any change
export WINEPREFIX=/opt/wine64

PYHOME=c:/python27
PYTHON="wine $PYHOME/python.exe -OO -B"


# Let's begin!
cd `dirname $0`
set -e

cd tmp

if [ -d "electrum-git" ]; then
    # GIT repository found, update it
    echo "Pull"
    cd electrum-git
    git checkout master
    git pull
    cd ..
else
    # GIT repository not found, clone it
    echo "Clone"
    git clone -b $BRANCH $ELECTRUM_GIT_URL electrum-git
fi

cd electrum-git
VERSION=`git describe --tags`
echo "Last commit: $VERSION"

cd ..

rm -rf $WINEPREFIX/drive_c/electrum-myr
cp -r electrum-git $WINEPREFIX/drive_c/electrum-myr
cp electrum-git/LICENCE .

# add python packages (built with make_packages)
cp -r ../../../packages $WINEPREFIX/drive_c/electrum-myr/

# add locale dir
cp -r ../../../lib/locale $WINEPREFIX/drive_c/electrum-myr/lib/

# Build Qt resources
wine $WINEPREFIX/drive_c/Python27/Lib/site-packages/PyQt4/pyrcc4.exe C:/electrum-myr/icons.qrc -o C:/electrum-myr/lib/icons_rc.py
wine $WINEPREFIX/drive_c/Python27/Lib/site-packages/PyQt4/pyrcc4.exe C:/electrum-myr/icons.qrc -o C:/electrum-myr/gui/qt/icons_rc.py

cd ..

rm -rf dist/

# build standalone version
$PYTHON "C:/pyinstaller/pyinstaller.py" --noconfirm --ascii -w deterministic.spec

# build NSIS installer
wine "$WINEPREFIX/drive_c/Program Files (x86)/NSIS/makensis.exe" electrum.nsi

cd dist
mv electrum-myr.exe $NAME_ROOT-$VERSION.exe
mv electrum-myr-setup.exe $NAME_ROOT-$VERSION-setup.exe
mv electrum-myr $NAME_ROOT-$VERSION
zip -r $NAME_ROOT-$VERSION.zip $NAME_ROOT-$VERSION
cd ..

# build portable version
cp portable.patch $WINEPREFIX/drive_c/electrum-myr
pushd $WINEPREFIX/drive_c/electrum-myr
patch < portable.patch 
popd
$PYTHON "C:/pyinstaller/pyinstaller.py" --noconfirm --ascii -w deterministic.spec
cd dist
mv electrum-myr.exe $NAME_ROOT-$VERSION-portable.exe
cd ..

echo "Done."
