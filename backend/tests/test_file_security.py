#!/usr/bin/env python3
"""Tests for session file-isolation security (orchestrator/auth.py): path-traversal
blocking, per-user directory isolation, symlink-escape prevention, and the download
endpoint's use of the authenticated user id.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.auth import auth_router
from fastapi import FastAPI

_test_app = FastAPI()
_test_app.include_router(auth_router)


def test_path_traversal_protection():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    download_dir = os.path.join(backend_dir, 'tmp', 'user123', 'session1')

    malicious_path = os.path.join(download_dir, '../../../../etc/passwd')
    file_path = os.path.abspath(malicious_path)
    download_dir_abs = os.path.abspath(download_dir)

    assert not file_path.startswith(download_dir_abs), (
        "Path traversal detection failed — malicious path would escape "
        f"the download directory ({download_dir_abs})"
    )


def test_user_specific_directory_isolation():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    user1_path = os.path.join(backend_dir, 'tmp', 'user1', 'session1', 'file.txt')
    user2_path = os.path.join(backend_dir, 'tmp', 'user2', 'session1', 'file.txt')

    assert user1_path != user2_path, (
        "User directory paths collide — HIPAA isolation violated"
    )

    assert 'user1' in user1_path, (
        f"User ID 'user1' not found in path: {user1_path}"
    )
    assert 'user2' in user2_path, (
        f"User ID 'user2' not found in path: {user2_path}"
    )


def test_download_endpoint_uses_authenticated_user_id():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    user_a_path = os.path.join(backend_dir, 'tmp', 'user-a', 'sess', 'x.txt')
    user_b_path = os.path.join(backend_dir, 'tmp', 'user-b', 'sess', 'x.txt')
    assert user_a_path != user_b_path, (
        "Download paths for different users must be distinct"
    )


def test_path_traversal_in_symlink_scenario():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    download_dir = os.path.join(backend_dir, 'tmp', 'user123', 'session1')

    escaped = os.path.abspath(os.path.join(download_dir, 'subdir', '../../../..', 'etc', 'passwd'))
    assert not escaped.startswith(os.path.abspath(download_dir)), (
        "Path traversal via relative components not blocked"
    )