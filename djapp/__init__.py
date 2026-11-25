# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Package marker that re-exports the program name, author, version and website from brand.py.

from .brand import APP_NAME, AUTHOR, SITE, VERSION, WEBSITE

__all__ = ["APP_NAME", "AUTHOR", "SITE", "VERSION", "WEBSITE"]
