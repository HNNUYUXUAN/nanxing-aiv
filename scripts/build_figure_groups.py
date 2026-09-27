"""Assemble independently rendered panels with the paper's LaTeX templates."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "fig01_dimensions": 4,
    "fig02_random_flow": 3,
    "fig03_unknown_labels": 2,
    "fig04_human_pairs": 5,
    "fig05_score_sensitivity": 6,
}


def build_groups(panel_directory, output, build_dir=None):
    panel_directory, output = Path(panel_directory).resolve(), Path(output).resolve()
    build_dir = Path(build_dir or output / "latex-build").resolve()
    output.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    tex = Path(os.environ.get("APPDATA", "")) / "TinyTeX/bin/windows/xelatex.exe"
    if not tex.is_file():
        found = shutil.which("xelatex")
        if not found:
            raise RuntimeError("XeLaTeX is required to assemble the independent figure panels")
        tex = Path(found)
    env = {**os.environ, "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"}
    env["PATH"] = str(tex.parent) + os.pathsep + env.get("PATH", "")
    records = []
    for name, figure_number in GROUPS.items():
        template = ROOT / "paper/figure_groups" / f"{name}.tex"
        panels = re.findall(r"\\PanelDirectory/([^{}]+\.pdf)", template.read_text("utf-8"))
        if len(panels) != 2 or any(not (panel_directory / p).is_file() for p in panels):
            raise ValueError(f"Two independently rendered panels are required: {name}")
        source = r"""\documentclass[border=2pt,12pt]{standalone}
\usepackage{ctex,graphicx,caption,subcaption,fontspec}
\setmainfont{Times New Roman}
\setCJKmainfont{SimSun}
% Caption-size baseline from the pinned CUMCMThesis template.
\DeclareCaptionFont{song}{\songti}
\DeclareCaptionFont{minusfour}{\zihao{-4}}
\captionsetup[figure]{font={song,minusfour,bf},position=bottom}
\captionsetup[table]{font={song,minusfour,bf},position=top}
\linespread{1.35}
\renewcommand{\figurename}{图}
\input{figure_style.tex}
\renewcommand{\PanelDirectory}{@@PANELS@@}
\begin{document}
\begin{minipage}{16cm}
\captionsetup{type=figure}
\setcounter{figure}{@@NUMBER@@}
\centering
\input{figure_groups/@@NAME@@.tex}
\end{minipage}
\end{document}
""".replace("@@PANELS@@", panel_directory.as_posix()).replace("@@NUMBER@@", str(figure_number - 1)).replace("@@NAME@@", name)
        tex_source = build_dir / f"{name}.tex"
        tex_source.write_text(source, "utf-8")
        result = subprocess.run([
            str(tex), "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
            f"-output-directory={build_dir.as_posix()}", tex_source.as_posix(),
        ], cwd=ROOT / "paper", env=env, capture_output=True)
        (build_dir / f"{name}.console.log").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"LaTeX figure assembly failed; see {build_dir / (name + '.console.log')}")
        compiled = build_dir / f"{name}.pdf"
        with pymupdf.open(compiled) as doc:
            if len(doc) != 1:
                raise ValueError(f"Expected a single-page figure group: {name}")
            doc[0].get_pixmap(dpi=600, alpha=False).save(output / f"{name}.png")
            (output / f"{name}.svg").write_text(doc[0].get_svg_image(text_as_path=True), "utf-8")
        shutil.copyfile(compiled, output / f"{name}.pdf")
        records.append({
            "id": name, "figure_number": figure_number,
            "panels": panels, "template": template.relative_to(ROOT).as_posix(),
            "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
            "panel_sha256": {p: hashlib.sha256((panel_directory / p).read_bytes()).hexdigest() for p in panels},
            "files": {f"{name}.{ext}": hashlib.sha256((output / f"{name}.{ext}").read_bytes()).hexdigest()
                      for ext in ("pdf", "png", "svg")},
        })
    receipt = {"assembly": "independent_panels_then_latex", "raster_dpi": 600,
               "figure_title": "centered", "figure_note": "separate_small_left_aligned_paragraph",
               "caption_size_source": "cumcmthesis: font={song,minusfour,bf}; minusfour=\\zihao{-4}",
               "figure_note_pt": 9,
               "style_sha256": hashlib.sha256((ROOT / "paper/figure_style.tex").read_bytes()).hexdigest(),
               "groups": records}
    (output / "figure-assembly.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panels", type=Path, default=ROOT / "results/final-analysis")
    parser.add_argument("--output", type=Path, default=ROOT / "results/final-analysis")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/figure-groups")
    args = parser.parse_args()
    receipt = build_groups(args.panels, args.output, args.build_dir)
    print(json.dumps({"assembly": receipt["assembly"], "groups": len(receipt["groups"]), "panels": 10}))
