# -*- mode: python -*-

home = 'C:/electrum-myr/'

# We don't put these files in to actually include them in the script but to make the Analysis method scan them for imports
a = Analysis([home+'electrum',
              home+'gui/qt/main_window.py',
              home+'gui/qt/lite_window.py',
              home+'gui/text.py',
              home+'lib/util.py',
              home+'lib/wallet.py',
              home+'lib/simple_config.py',
              home+'lib/bitcoin.py'
              ],
             hiddenimports=['lib', 'gui'],
             pathex=['lib:gui:plugins'],
             hookspath=None)

##### include folder in distribution #######
def extra_data(folder):
    def rec_glob(p, files):
        import os
        import glob
        for d in glob.glob(p):
            if os.path.isfile(d):
                files.append(d)
            rec_glob("%s/*" % d, files)
    files = []
    rec_glob("%s/*" % folder, files)
    extra_data = []
    for f in files:
        extra_data.append((f, f, 'DATA'))

    return extra_data
###########################################

# append dirs

# Theme data
a.datas += extra_data('data')

# Localization
a.datas += extra_data('locale')

# Py folders that are needed because of the magic import finding
a.datas += extra_data('gui')
a.datas += extra_data('lib')
a.datas += extra_data('plugins')

pyz = PYZ(a.pure)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.datas,
          name=os.path.join('build\\pyi.win32\\electrum-myr', 'electrum-myr.exe'),
          debug=False,
          strip=None,
          upx=False,
          icon=home+'icons/electrum.ico',
          console=False)
          # The console True makes an annoying black box pop up, but it does make Electrum output command line commands, with this turned off no output will be given but commands can still be used

coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=None,
               upx=True,
               debug=False,
               icon=home+'icons/electrum.ico',
               console=False,
               name=os.path.join('dist', 'electrum-myr'))
