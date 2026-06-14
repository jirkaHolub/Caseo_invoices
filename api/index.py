"""Vstupní bod pro Vercel (@vercel/python).

Vercel detekuje ASGI objekt `app` a obslouží jím všechny požadavky.
Přidáme kořen repa do sys.path, ať fungují importy a cesty k templates/static/fonts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
