# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Refuses to open the encrypted databases of other programs such as rekordbox, by name and file header.

import os

# The databases that rekordbox encrypts. Whatever their content, the program does not open them: it has no key,
# it looks for none and it carries no code to decrypt a database
NAMES = ("master.db", "exportlibrary.db")
EXTENSIONS = (".db", ".sqlite", ".sqlite3")
# the private libraries of Apple, only the exported XML is read
BINARY_LIBRARIES = (".itl", ".musiclibrary")
MAGIC = b"SQLite format 3\x00"

# A plain SQLite file starts with its magic text. A database file that does not is encrypted (or damaged), and by
# the name alone the databases of rekordbox are refused
def encrypted(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in NAMES or os.path.splitext(name)[1] in BINARY_LIBRARIES:
        return True
    if os.path.splitext(name)[1] not in EXTENSIONS or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(len(MAGIC))
    except OSError:
        return False
    return len(head) == len(MAGIC) and head != MAGIC

def refuse(path: str):
    if encrypted(path):
        raise ValueError(
            f"{os.path.basename(path)} is an encrypted or private library. Boothbridge never opens, "
            "reads or decrypts the protected databases of other programs. For iTunes or Music, "
            "use File > Library > Export Library and open the XML file."
        )
