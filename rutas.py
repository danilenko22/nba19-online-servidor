"""Carpeta base del servidor: la del .exe cuando va empaquetado con PyInstaller (el codigo se descomprime
en una carpeta temporal) y la de los .py en otro caso. Ahi van logs/, el certificado y los ajustes."""
import os
import sys

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
