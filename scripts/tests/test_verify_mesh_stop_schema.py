"""Verifies that mesh and stop schema inputs remain bounded static data in the
composition reader. Untrusted imports, expressions and executable source never
run while computing their migration digests.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_verify_composition import _write, _write_migration, composition


@pytest.fixture(params=[("mesh", "089.001", "089.002"), ("stop", "089.002", "089.003")])
def schema(request):
    stem, predecessor, revision = request.param
    symbol = stem.upper() + "_SCHEMA_STATEMENTS"
    imported = f"from astralplane.database.{stem}_schema import {symbol}\n"
    registry = (
        f"M = Migration(name={stem!r}, source_revisions=({predecessor!r},), "
        f"target_revision={revision!r}, checksum=_statements_checksum({symbol}), "
        "operation=_apply)\nMIGRATION_REGISTRY = MigrationRegistry((M,))\n"
    )
    return SimpleNamespace(stem=stem, symbol=symbol, imported=imported, registry=registry,
                           predecessor=predecessor, revision=revision)


def read(tmp_path: Path, schema, source: str, imports: str | None = None) -> str:
    root = _write_migration(tmp_path / "Plane", (schema.imported if imports is None else imports)
                            + schema.registry)
    _write(root / f"src/astralplane/database/{schema.stem}_schema.py", source)
    return composition._plane_migration_digest(root)


def test_literal_strip_matches_independent_digest_and_changed_bytes_are_bound(tmp_path, schema):
    source = f'{schema.symbol} = ("  SELECT 1  ".strip(), "SELECT 2")\n'
    checksum = hashlib.sha256(b'["SELECT 1","SELECT 2"]').hexdigest()
    manifest = [{"name": schema.stem, "source_revisions": [schema.predecessor],
                 "target_revision": schema.revision, "checksum": checksum}]
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                                        separators=(",", ":")).encode("ascii")).hexdigest()
    assert read(tmp_path, schema, source) == expected
    assert read(tmp_path, schema, source.replace("SELECT 2", "SELECT 3")) != expected


@pytest.mark.parametrize("value", [
    '["SELECT 1"]', 'tuple(["SELECT 1"])', '("SELECT 1".rstrip(),)',
    '("SELECT 1".strip("x"),)', '("SELECT 1".strip(*()),)',
    '("SELECT 1".strip(**dict()),)', '("SELECT 1".strip().strip(),)',
    '("  ".strip(),)', '()', '(1,)', '(*OTHER,)', '("SELECT 1",) * 4097',
])
def test_unreviewed_expressions_are_refused(tmp_path, schema, value):
    with pytest.raises(composition.CompositionError):
        read(tmp_path, schema, f"{schema.symbol} = {value}\n")


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "alias", "rebound", "nested"])
def test_import_identity_and_binding_are_required(tmp_path, schema, mutation):
    imports = {
        "duplicate": schema.imported + schema.imported,
        "foreign": schema.imported.replace("from astralplane.database.", "from arbitrary."),
        "alias": schema.imported.replace(schema.symbol, f"{schema.symbol} as ALIAS"),
        "rebound": schema.imported + f'{schema.symbol} = ("CHANGED",)\n',
        "nested": "if True:\n    " + schema.imported,
    }
    with pytest.raises(composition.CompositionError):
        read(tmp_path, schema, f'{schema.symbol} = ("SELECT 1",)\n', imports[mutation])


def test_source_statements_and_callable_items_never_execute(tmp_path, schema):
    marker = tmp_path / "candidate-executed"
    executable = f'__import__("pathlib").Path({str(marker)!r}).touch()'
    for source in (f'{schema.symbol} = ("SELECT 1",)\n{executable}\n',
                   f'{schema.symbol} = ({executable},)\n'):
        with pytest.raises(composition.CompositionError):
            read(tmp_path, schema, source)
        assert not marker.exists()


def test_literal_item_bound_is_retained(tmp_path, schema):
    source = f"{schema.symbol} = " + repr(("SELECT 1",) * 4097) + "\n"
    with pytest.raises(composition.CompositionError, match="bounded nonempty"):
        read(tmp_path, schema, source)
