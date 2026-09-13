"""Guidance schemas are exact static tuples, never executable input."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_verify_composition import composition, _write, _write_migration

@pytest.fixture(params=[("guidance", "088.004", "088.005"),
                        ("selected_input", "088.005", "088.006")])
def schema(request):
    stem, predecessor, revision = request.param
    symbol = stem.upper() + "_SCHEMA_STATEMENTS"
    name = "plane-088-" + stem
    imported = f"from astralplane.database.{stem}_schema import {symbol}\n"
    registry = (f'M = Migration(name={name!r}, source_revisions=({predecessor!r},), '
                f'target_revision={revision!r}, checksum=_statements_checksum({symbol}), operation=_apply)\n'
                'MIGRATION_REGISTRY = MigrationRegistry((M,))\n')
    return SimpleNamespace(stem=stem, symbol=symbol, name=name, imported=imported,
                           registry=registry, predecessor=predecessor, revision=revision)


def digest(tmp_path, schema, source, import_source=None):
    root = _write_migration(tmp_path / "Plane", (schema.imported if import_source is None else import_source)
                            + schema.registry)
    _write(root / f"src/astralplane/database/{schema.stem}_schema.py", source)
    return composition._plane_migration_digest(root)


def test_guidance_static_digest_matches_canonical_manifest(tmp_path, schema):
    checksum = hashlib.sha256(b'["SELECT 1"]').hexdigest()
    manifest = [{"name": schema.name, "source_revisions": [schema.predecessor],
                 "target_revision": schema.revision, "checksum": checksum}]
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                                        separators=(",", ":")).encode("ascii")).hexdigest()
    assert digest(tmp_path, schema, f'{schema.symbol} = ("SELECT 1",)\n') == expected


@pytest.mark.parametrize("source", [
    '{symbol} = ["SELECT 1"]\n',
    'from typing import Final\n{symbol}: Final = ("SELECT 1",)\n',
    '{symbol} = ("SELECT 1".rstrip(),)\n',
    '{symbol} = ("SELECT 1".strip("x"),)\n',
    '{symbol} = ("SELECT 1".strip(*()),)\n',
    '{symbol} = ("SELECT 1".strip(**dict()),)\n',
    '{symbol} = ("SELECT 1".strip().strip(),)\n',
    '{symbol} = ("  ".strip(),)\n',
    '{symbol} = ("SELECT 1",)\nimport os\n',
    '{symbol} = ()\n',
])
def test_guidance_never_expands_literal_grammar(tmp_path, schema, source):
    with pytest.raises(composition.CompositionError):
        digest(tmp_path, schema, source.format(symbol=schema.symbol))


@pytest.mark.parametrize("import_source", [
    "duplicate", "foreign", "alias", "rebound",
])
def test_guidance_import_is_exact_and_not_rebound(tmp_path, schema, import_source):
    imports = {
        "duplicate": schema.imported + schema.imported,
        "foreign": schema.imported.replace("from astralplane.database.", "from other."),
        "alias": schema.imported.replace(schema.symbol, f"{schema.symbol} as ALIAS"),
        "rebound": schema.imported + f'{schema.symbol} = ("CHANGED",)\n',
    }
    with pytest.raises(composition.CompositionError):
        digest(tmp_path, schema, f'{schema.symbol} = ("SELECT 1",)\n', imports[import_source])


def test_guidance_candidate_statements_are_never_executed(tmp_path, schema):
    marker = tmp_path / "candidate-executed"
    source = (f'{schema.symbol} = ("SELECT 1",)\n'
              f'__import__("pathlib").Path({str(marker)!r}).touch()\n')
    with pytest.raises(composition.CompositionError):
        digest(tmp_path, schema, source)
    assert not marker.exists()


def test_strip_syntax_is_scoped_to_exact_selected_input_literals(tmp_path, schema):
    source = f'{schema.symbol} = ("  SELECT 1  ".strip(),)\n'
    if schema.stem == "selected_input":
        assert digest(tmp_path, schema, source) == digest(
            tmp_path, schema, f'{schema.symbol} = ("SELECT 1",)\n'
        )
    else:
        with pytest.raises(composition.CompositionError):
            digest(tmp_path, schema, source)
