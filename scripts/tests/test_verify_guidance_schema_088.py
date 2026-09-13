"""The 088.005 schema is an exact static tuple, never executable input."""
import hashlib
import json

import pytest
from test_verify_composition import composition, _write, _write_migration

SYMBOL = "GUIDANCE_SCHEMA_STATEMENTS"
IMPORT = f"from astralplane.database.guidance_schema import {SYMBOL}\n"
REGISTRY = (f'M = Migration(name="plane-088-guidance", source_revisions=("088.004",), '
            f'target_revision="088.005", checksum=_statements_checksum({SYMBOL}), operation=_apply)\n'
            'MIGRATION_REGISTRY = MigrationRegistry((M,))\n')


def digest(tmp_path, source, import_source=IMPORT):
    root = _write_migration(tmp_path / "Plane", import_source + REGISTRY)
    _write(root / "src/astralplane/database/guidance_schema.py", source)
    return composition._plane_migration_digest(root)


def test_guidance_static_digest_matches_canonical_manifest(tmp_path):
    checksum = hashlib.sha256(b'["SELECT 1"]').hexdigest()
    manifest = [{"name": "plane-088-guidance", "source_revisions": ["088.004"],
                 "target_revision": "088.005", "checksum": checksum}]
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                                        separators=(",", ":")).encode("ascii")).hexdigest()
    assert digest(tmp_path, f'{SYMBOL} = ("SELECT 1",)\n') == expected


@pytest.mark.parametrize("source", [
    f'{SYMBOL} = ["SELECT 1"]\n',
    f'from typing import Final\n{SYMBOL}: Final = ("SELECT 1",)\n',
    f'{SYMBOL} = ("SELECT 1".strip(),)\n',
    f'{SYMBOL} = ("SELECT 1",)\nimport os\n',
    f'{SYMBOL} = ()\n',
])
def test_guidance_never_expands_literal_grammar(tmp_path, source):
    with pytest.raises(composition.CompositionError):
        digest(tmp_path, source)


@pytest.mark.parametrize("import_source", [
    IMPORT + IMPORT,
    IMPORT.replace("from astralplane.database.", "from other."),
    IMPORT.replace(SYMBOL, f"{SYMBOL} as ALIAS"),
    IMPORT + f'{SYMBOL} = ("CHANGED",)\n',
])
def test_guidance_import_is_exact_and_not_rebound(tmp_path, import_source):
    with pytest.raises(composition.CompositionError):
        digest(tmp_path, f'{SYMBOL} = ("SELECT 1",)\n', import_source)
