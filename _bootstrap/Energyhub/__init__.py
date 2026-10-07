"""Load the owning Energyhub package without exposing its host site-packages.

This tiny bridge is only placed on a calculation subprocess's PYTHONPATH.
It supports a wheel installed in one Python and PySCF installed in another.
"""
from pathlib import Path as _Path

_package = _Path(__file__).resolve().parents[2]
__path__ = [str(_package)]
__file__ = str(_package / '__init__.py')
if __spec__ is not None:
    __spec__.submodule_search_locations = __path__
exec(compile(_Path(__file__).read_bytes(), __file__, 'exec'), globals(), globals())
