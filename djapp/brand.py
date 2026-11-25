# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Single source for the program name, version, author, website and the names of its files.

# The name of the program lives here and nowhere else in the code. A new name is a new value of APP_NAME,
# then tools/rename.py renames the files and the texts that cannot read it at run time

APP_NAME = "Boothbridge"
APP_SLUG = APP_NAME.lower()
VERSION = "1.0.0"
AUTHOR = "Benoit (BSM) Saint-Moulin"
WEBSITE = "www.benoitsaintmoulin.com"
SITE = ""  # the address of the program's own site, when it has one
SUPPORT_URL = "https://ko-fi.com/bsm3d"  # the Ko-fi page, the support entries appear when it is set

# the names of the files the program writes next to itself and on a key
SETTINGS_FILE = f"{APP_SLUG}.json"
ORG_FOLDER = "BSM"  # the folder of the author among the user's data: %APPDATA%\BSM\<program>
PORTABLE_FLAG = "portable.flag"  # a file next to the program: the settings and the log stay beside it
HOME_ENV = f"{APP_NAME.upper()}_HOME"  # a variable that replaces the data folder (the tests, another disk)
LOG_FILE = f"{APP_SLUG}.log"
CHECKSUMS_FILE = f"{APP_SLUG}_checksums.json"
BACKUP_SUFFIX = ".bak"
