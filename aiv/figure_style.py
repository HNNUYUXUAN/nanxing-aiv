"""Shared publication typography for paper and notebook data figures.

Times New Roman is first so Latin characters and digits never resolve to the
CJK font. SimSun supplies Chinese glyphs. Missing fonts fail explicitly rather
than silently changing the typography on a collaborator's machine.
"""
from functools import lru_cache

import matplotlib as mpl
from matplotlib import font_manager

RASTER_DPI = 600
FONT_FAMILIES = ("Times New Roman", "SimSun")


@lru_cache(maxsize=1)
def require_publication_fonts():
    paths = {}
    for family in FONT_FAMILIES:
        try:
            paths[family] = str(font_manager.findfont(
                font_manager.FontProperties(family=family), fallback_to_default=False))
        except ValueError as error:
            raise RuntimeError(
                f"Publication font missing: {family}. Install the licensed font "
                "locally before generating figures; substitution is disabled."
            ) from error
    return paths


def configure_publication_style():
    require_publication_fonts()
    mpl.rcParams.update({
        "font.family": list(FONT_FAMILIES),
        "font.serif": list(FONT_FAMILIES),
        "font.sans-serif": list(FONT_FAMILIES),
        "font.monospace": list(FONT_FAMILIES),
        "figure.dpi": 120,
        "savefig.dpi": RASTER_DPI,
        "savefig.facecolor": "white",
        "axes.unicode_minus": False,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "path",
        "mathtext.fontset": "custom",
        "mathtext.rm": "Times New Roman",
        "mathtext.it": "Times New Roman:italic",
        "mathtext.bf": "Times New Roman:bold",
        "mathtext.bfit": "Times New Roman:italic:bold",
        "mathtext.sf": "Times New Roman",
        "mathtext.tt": "Times New Roman",
        "mathtext.cal": "Times New Roman:italic",
        "mathtext.fallback": None,
    })


def publication_metadata():
    return {
        "raster_dpi": RASTER_DPI,
        "fonts": {"chinese": "SimSun", "latin_and_digits": "Times New Roman"},
        "pdf_fonttype": 42,
        "svg_fonttype": "path",
    }
