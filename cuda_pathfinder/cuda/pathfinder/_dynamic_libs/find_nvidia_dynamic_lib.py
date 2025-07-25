# SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import functools
import glob
import os
from collections.abc import Sequence
from typing import Optional

from cuda.pathfinder._dynamic_libs.load_dl_common import DynamicLibNotFoundError
from cuda.pathfinder._dynamic_libs.supported_nvidia_libs import (
    IS_WINDOWS,
    is_suppressed_dll_file,
)
from cuda.pathfinder._utils.find_sub_dirs import find_sub_dirs_all_sitepackages


def _no_such_file_in_sub_dirs(
    sub_dirs: Sequence[str], file_wild: str, error_messages: list[str], attachments: list[str]
) -> None:
    error_messages.append(f"No such file: {file_wild}")
    for sub_dir in find_sub_dirs_all_sitepackages(sub_dirs):
        attachments.append(f'  listdir("{sub_dir}"):')
        for node in sorted(os.listdir(sub_dir)):
            attachments.append(f"    {node}")


def _find_so_using_nvidia_lib_dirs(
    libname: str, so_basename: str, error_messages: list[str], attachments: list[str]
) -> Optional[str]:
    nvidia_sub_dirs = ("nvidia", "*", "nvvm", "lib64") if libname == "nvvm" else ("nvidia", "*", "lib")
    file_wild = so_basename + "*"
    for lib_dir in find_sub_dirs_all_sitepackages(nvidia_sub_dirs):
        # First look for an exact match
        so_name = os.path.join(lib_dir, so_basename)
        if os.path.isfile(so_name):
            return so_name
        # Look for a versioned library
        # Using sort here mainly to make the result deterministic.
        for so_name in sorted(glob.glob(os.path.join(lib_dir, file_wild))):
            if os.path.isfile(so_name):
                return so_name
    _no_such_file_in_sub_dirs(nvidia_sub_dirs, file_wild, error_messages, attachments)
    return None


def _find_dll_under_dir(dirpath: str, file_wild: str) -> Optional[str]:
    for path in sorted(glob.glob(os.path.join(dirpath, file_wild))):
        if not os.path.isfile(path):
            continue
        if not is_suppressed_dll_file(os.path.basename(path)):
            return path
    return None


def _find_dll_using_nvidia_bin_dirs(
    libname: str, lib_searched_for: str, error_messages: list[str], attachments: list[str]
) -> Optional[str]:
    nvidia_sub_dirs = ("nvidia", "*", "nvvm", "bin") if libname == "nvvm" else ("nvidia", "*", "bin")
    for bin_dir in find_sub_dirs_all_sitepackages(nvidia_sub_dirs):
        dll_name = _find_dll_under_dir(bin_dir, lib_searched_for)
        if dll_name is not None:
            return dll_name
    _no_such_file_in_sub_dirs(nvidia_sub_dirs, lib_searched_for, error_messages, attachments)
    return None


def _get_cuda_home() -> Optional[str]:
    cuda_home = os.environ.get("CUDA_HOME")
    if cuda_home is None:
        cuda_home = os.environ.get("CUDA_PATH")
    return cuda_home


def _find_lib_dir_using_cuda_home(libname: str) -> Optional[str]:
    cuda_home = _get_cuda_home()
    if cuda_home is None:
        return None
    subdirs: tuple[str, ...]
    if IS_WINDOWS:
        subdirs = (os.path.join("nvvm", "bin"),) if libname == "nvvm" else ("bin",)
    else:
        subdirs = (
            (os.path.join("nvvm", "lib64"),)
            if libname == "nvvm"
            else (
                "lib64",  # CTK
                "lib",  # Conda
            )
        )
    for subdir in subdirs:
        dirname = os.path.join(cuda_home, subdir)
        if os.path.isdir(dirname):
            return dirname
    return None


def _find_so_using_lib_dir(
    lib_dir: str, so_basename: str, error_messages: list[str], attachments: list[str]
) -> Optional[str]:
    so_name = os.path.join(lib_dir, so_basename)
    if os.path.isfile(so_name):
        return so_name
    error_messages.append(f"No such file: {so_name}")
    attachments.append(f'  listdir("{lib_dir}"):')
    if not os.path.isdir(lib_dir):
        attachments.append("    DIRECTORY DOES NOT EXIST")
    else:
        for node in sorted(os.listdir(lib_dir)):
            attachments.append(f"    {node}")
    return None


def _find_dll_using_lib_dir(
    lib_dir: str, libname: str, error_messages: list[str], attachments: list[str]
) -> Optional[str]:
    file_wild = libname + "*.dll"
    dll_name = _find_dll_under_dir(lib_dir, file_wild)
    if dll_name is not None:
        return dll_name
    error_messages.append(f"No such file: {file_wild}")
    attachments.append(f'  listdir("{lib_dir}"):')
    for node in sorted(os.listdir(lib_dir)):
        attachments.append(f"    {node}")
    return None


class _FindNvidiaDynamicLib:
    def __init__(self, libname: str):
        self.libname = libname
        self.error_messages: list[str] = []
        self.attachments: list[str] = []
        self.abs_path = None

        if IS_WINDOWS:
            self.lib_searched_for = f"{libname}*.dll"
            if self.abs_path is None:
                self.abs_path = _find_dll_using_nvidia_bin_dirs(
                    libname,
                    self.lib_searched_for,
                    self.error_messages,
                    self.attachments,
                )
        else:
            self.lib_searched_for = f"lib{libname}.so"
            if self.abs_path is None:
                self.abs_path = _find_so_using_nvidia_lib_dirs(
                    libname,
                    self.lib_searched_for,
                    self.error_messages,
                    self.attachments,
                )

    def retry_with_cuda_home_priority_last(self) -> None:
        cuda_home_lib_dir = _find_lib_dir_using_cuda_home(self.libname)
        if cuda_home_lib_dir is not None:
            if IS_WINDOWS:
                self.abs_path = _find_dll_using_lib_dir(
                    cuda_home_lib_dir,
                    self.libname,
                    self.error_messages,
                    self.attachments,
                )
            else:
                self.abs_path = _find_so_using_lib_dir(
                    cuda_home_lib_dir,
                    self.lib_searched_for,
                    self.error_messages,
                    self.attachments,
                )

    def raise_if_abs_path_is_None(self) -> str:  # noqa: N802
        if self.abs_path:
            return self.abs_path
        err = ", ".join(self.error_messages)
        att = "\n".join(self.attachments)
        raise DynamicLibNotFoundError(f'Failure finding "{self.lib_searched_for}": {err}\n{att}')


@functools.cache
def find_nvidia_dynamic_lib(libname: str) -> str:
    return _FindNvidiaDynamicLib(libname).raise_if_abs_path_is_None()
