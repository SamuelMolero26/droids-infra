"""CLI helpers: --docs validation."""

from __future__ import annotations

import click
import pytest

from droids_agents import cli


def _make_file(p, content: bytes = b"x") -> str:
    p.write_bytes(content)
    return str(p)


def test_validate_docs_accepts_pdf_md_txt(tmp_path) -> None:
    a = _make_file(tmp_path / "a.md")
    b = _make_file(tmp_path / "b.pdf", b"%PDF-1.4")
    c = _make_file(tmp_path / "c.txt")
    out = cli._validate_docs((a, b, c))
    assert {bn for _, bn in out} == {"a.md", "b.pdf", "c.txt"}


def test_validate_docs_rejects_missing(tmp_path) -> None:
    with pytest.raises(click.UsageError, match="path not found"):
        cli._validate_docs((str(tmp_path / "nope.md"),))


def test_validate_docs_rejects_bad_extension(tmp_path) -> None:
    bad = _make_file(tmp_path / "x.docx")
    with pytest.raises(click.UsageError, match="extension"):
        cli._validate_docs((bad,))


def test_validate_docs_rejects_basename_duplicates(tmp_path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    a = _make_file(tmp_path / "dup.md")
    b = _make_file(sub / "dup.md")  # same basename in different dirs
    with pytest.raises(click.UsageError, match="basename"):
        cli._validate_docs((a, b))


def test_validate_docs_rejects_total_size_cap(tmp_path) -> None:
    big = _make_file(tmp_path / "big.txt", b"\0" * (6 * 1024 * 1024))
    with pytest.raises(click.UsageError, match="total size"):
        cli._validate_docs((big,))
