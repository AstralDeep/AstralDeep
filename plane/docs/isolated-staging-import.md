# Isolated backend/web staging import

`scripts/import_staging_fixture.py` is Plane's bounded, source-owned staging
import contract (`astralplane.synthetic-staging-import/v1`). It accepts only
the existing synthetic `066.001` pre-split fixture, with an explicitly reviewed
SHA-256 over its SQL, inventories, expected records, blobs, loader and historical
baseline builder. It is not an arbitrary SQL importer or a production backup
restore interface. No existing database, schema or blob destination is adopted.

Provision a new empty PostgreSQL database named
`astralplane_qualification_<32 lowercase hex characters>`. Use the same ID below.
Keep the DSN in `ASTRALPLANE_QUALIFICATION_DATABASE_URL`, never a command argument
or evidence file. Supply an explicit host and user; service files, implicit
database names, connection options and user-selected schema names are refused.
The connected database identity and empty catalog are checked under Plane's
migration advisory lock before importing. No application or other writer may
use this database before preparation completes.

Run from the clean, exact pinned Plane checkout with Python 3.11 and its declared
driver dependency installed:

```text
python scripts/import_staging_fixture.py --qualification-id ID --expected-fixture-sha256 REVIEWED_DIGEST --blob-root /isolated/new/attachments
```

The command exits 2 on a rejected import; it never reports success after a
failed database/blob operation. The resulting public JSON identifies the source
schema, complete fixture digest, loader/importer identities, catalog and blob
fingerprints. It deliberately includes `release_authorized: false`. Preserve
the output with the exact Plane commit and image identities. The source fixture
digest is available for review from
`tests.fixtures.pre_split.loader.fixture_digest()`; protected automation must
bind the reviewed value rather than accept a candidate's self-declared digest.

The output's `database_options` names the generated schema. Add those options
to the application's `DATABASE_URL` and bind `ATTACHMENT_UPLOAD_ROOT` to the same
isolated attachment root. The application must then run its ordinary Plane
initialization and required product reconciliation before admitting requests.
The source-owned qualification scripts are not installed as a production API.
A qualification driver may mount a verified Plane checkout read-only into a
transient container; the final application runs its installed, pinned package.

For a historical baseline, use the exact baseline Plane checkout/environment
to run `scripts/migrate_qualification_database.py` from this checkout, explicitly
setting `PYTHONPATH` to the baseline checkout's `src` directory:

```text
python /candidate-plane/scripts/migrate_qualification_database.py --qualification-id ID --expected-revision 088.003 --expected-migration-digest a3d3ac43bee48b0ca6832cca1e4a347db0a3f838af908b8545edb11e7e94272a
```

The observed sandbox baseline on 2026-09-21 is Plane
`4a07d59a448c1960ce2ae3f35e605f4d78c4a3f9`. Verify the checkout before execution.
The command rejects a different loaded registry and a different target database
or schema. It runs only the ordinary guarded migration registry. It explicitly
reports that product reconciliation and release authorization remain incomplete.

Before starting the candidate application, take a paired PostgreSQL custom-format
backup and non-following verified blob snapshot. Preserve schema ownership/ACLs,
all encryption and audit keys and external authority/replay state in an actual
upgrade rehearsal. Reapply the candidate registry, verify complete current catalog
and repeat-safe no-op, and compare all retained records and blob fingerprints.
Rehearse restoration into isolated storage and repeat the guarded upgrade. Follow
[migration and recovery](migration-and-recovery.md); never downgrade with table
drops or rewritten revision/digest markers.

Synthetic rehearsal proves the registry's baseline path and representative byte
continuity. It does not prove cryptographic access to live encrypted credentials,
real authenticated user flows, LETS enforcement, enabled voice workers or live
dataset completeness. Those remain separate application qualification checks.
