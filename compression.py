from __future__ import annotations

"""Compression adapters for cjsonl storage. Stdlib gzip is the default."""

import gzip
import os
import shutil
import subprocess
from contextlib import contextmanager
from typing import Iterator, Protocol, TextIO


class Compressor(Protocol):
    name: str
    extension: str

    def compress_file(self, src_path: str, dst_path: str) -> None:
        ...

    def decompress_file(self, src_path: str, dst_path: str) -> None:
        ...

    def open_text(self, path: str, *, encoding: str = "utf-8") -> TextIO:
        ...


class GzipCompressor:
    name = "gzip"
    extension = ".gz"

    def __init__(self, *, level: int = 6, chunk_size: int = 1024 * 1024) -> None:
        self.level = level
        self.chunk_size = chunk_size

    def compress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        with open(src_path, "rb") as src, gzip.open(tmp, "wb", compresslevel=self.level) as dst:
            shutil.copyfileobj(src, dst, length=self.chunk_size)
        os.replace(tmp, dst_path)

    def decompress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        with gzip.open(src_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, length=self.chunk_size)
        os.replace(tmp, dst_path)

    def open_text(self, path: str, *, encoding: str = "utf-8") -> TextIO:
        return gzip.open(path, "rt", encoding=encoding)  # type: ignore[return-value]


class NoopCompressor:
    name = "none"
    extension = ""

    def __init__(self, *, chunk_size: int = 1024 * 1024) -> None:
        self.chunk_size = chunk_size

    def compress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, dst_path)

    def decompress_file(self, src_path: str, dst_path: str) -> None:
        self.compress_file(src_path, dst_path)

    def open_text(self, path: str, *, encoding: str = "utf-8") -> TextIO:
        return open(path, "r", encoding=encoding)


class PigzCompressor:
    """
    pigz adapter using an external `pigz` binary.

    Output is gzip-compatible, so readers can use normal gzip.open(). This is
    useful when write/rollover speed matters and multiple CPU cores are available.
    """

    name = "pigz"
    extension = ".gz"

    def __init__(self, *, level: int = 6, processes: int | None = None, binary: str = "pigz") -> None:
        self.level = level
        self.processes = processes
        self.binary = binary

    def _base_cmd(self) -> list[str]:
        cmd = [self.binary, f"-{self.level}", "-c"]
        if self.processes is not None:
            cmd.extend(["-p", str(self.processes)])
        return cmd

    def compress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        with open(src_path, "rb") as src, open(tmp, "wb") as dst:
            subprocess.run(self._base_cmd(), stdin=src, stdout=dst, check=True)
        os.replace(tmp, dst_path)

    def decompress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        cmd = [self.binary, "-d", "-c"]
        with open(src_path, "rb") as src, open(tmp, "wb") as dst:
            subprocess.run(cmd, stdin=src, stdout=dst, check=True)
        os.replace(tmp, dst_path)

    def open_text(self, path: str, *, encoding: str = "utf-8") -> TextIO:
        # pigz output is gzip-compatible; stdlib gzip reader is portable.
        return gzip.open(path, "rt", encoding=encoding)  # type: ignore[return-value]


class ExternalCompressor:
    """
    Generic external compressor adapter.

    compress_cmd/decompress_cmd are argv lists. Use "{src}" and "{dst}"
    placeholders when the tool expects paths. If no placeholders are present,
    stdin/stdout streaming is used.
    """

    def __init__(
        self,
        *,
        name: str,
        extension: str,
        compress_cmd: list[str],
        decompress_cmd: list[str],
        reader: Compressor | None = None,
    ) -> None:
        self.name = name
        self.extension = extension
        self.compress_cmd = compress_cmd
        self.decompress_cmd = decompress_cmd
        self.reader = reader or GzipCompressor()

    @staticmethod
    def _run(cmd_template: list[str], src_path: str, dst_path: str) -> None:
        cmd = [part.format(src=src_path, dst=dst_path) for part in cmd_template]
        has_src_dst = any("{src}" in part or "{dst}" in part for part in cmd_template)
        if has_src_dst:
            subprocess.run(cmd, check=True)
        else:
            with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
                subprocess.run(cmd, stdin=src, stdout=dst, check=True)

    def compress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        self._run(self.compress_cmd, src_path, tmp)
        os.replace(tmp, dst_path)

    def decompress_file(self, src_path: str, dst_path: str) -> None:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        tmp = dst_path + ".tmp"
        self._run(self.decompress_cmd, src_path, tmp)
        os.replace(tmp, dst_path)

    def open_text(self, path: str, *, encoding: str = "utf-8") -> TextIO:
        return self.reader.open_text(path, encoding=encoding)


def compress_file(
    src_path: str,
    dst_path: str,
    *,
    compressor: Compressor | None = None,
    remove_src: bool = False,
) -> None:
    comp = compressor or GzipCompressor()
    comp.compress_file(src_path, dst_path)
    if remove_src:
        os.remove(src_path)


def decompress_file(
    src_path: str,
    dst_path: str,
    *,
    compressor: Compressor | None = None,
    remove_src: bool = False,
) -> None:
    comp = compressor or GzipCompressor()
    comp.decompress_file(src_path, dst_path)
    if remove_src:
        os.remove(src_path)


# Backward-compatible names for current cjson.py API.
def gzip_file(src_path: str, dst_path: str, *, compresslevel: int = 6, remove_src: bool = False) -> None:
    compress_file(src_path, dst_path, compressor=GzipCompressor(level=compresslevel), remove_src=remove_src)


def gunzip_file(src_path: str, dst_path: str, *, remove_src: bool = False) -> None:
    decompress_file(src_path, dst_path, compressor=GzipCompressor(), remove_src=remove_src)
