"""Offline chapter navigation must survive the notebook-to-HTML relocation."""
from pathlib import Path

from scripts import execute_notebooks as executor


def test_html_links_keep_chapters_documents_and_external_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "ROOT", tmp_path)
    monkeypatch.setattr(executor, "OUT", tmp_path / "build/notebooks")
    notebook = tmp_path / "notebooks/00_总览.ipynb"
    chapter = tmp_path / "notebooks/07_报告.ipynb"
    guide = tmp_path / "docs/复现.md"
    for path in (notebook, chapter, guide):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    html = ('<a href="07_报告.ipynb#案例">chapter</a>'
            '<a href="../docs/复现.md">guide</a>'
            '<a href="https://example.org/a.ipynb">web</a>'
            '<a href="#本页">anchor</a>')
    rewritten = executor.html_reading_links(html, notebook)
    assert 'href="07_%E6%8A%A5%E5%91%8A.html#%E6%A1%88%E4%BE%8B"' in rewritten
    assert 'href="../../docs/%E5%A4%8D%E7%8E%B0.md"' in rewritten
    assert 'href="https://example.org/a.ipynb"' in rewritten
    assert 'href="#本页"' in rewritten


def test_chapter_navigation_follows_reading_order_not_filename_order(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "ROOT", tmp_path)
    folder = tmp_path / "notebooks"
    folder.mkdir()
    for name in ("01_数据.ipynb", "02_标注.ipynb", "08_范围.ipynb"):
        (folder / name).write_text("", encoding="utf-8")
    nav = executor.reading_navigation(folder / "08_范围.ipynb")
    assert "上一章 · 01" in nav
    assert "下一章 · 02" in nav
    assert 'href="index.html"' in nav
