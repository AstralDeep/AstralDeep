// Exercises the publication checker against real Git objects, staged changes and working files in isolated repositories.
// Synthetic credential-shaped values are assembled at runtime and diagnostics must never reveal their contents.

import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { deflateSync } from 'node:zlib';

const SOURCE = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const CHECKER = 'scripts/check_publication_fixtures.mjs';
const CONTRACT = 'scripts/publication_fixture_contract.json';
const MODULE = 'scripts/lib/publication-secrets.mjs';
const FIXTURES = [
  'backend/llm_config/tests/test_call_llm_credential_resolution.py',
  'backend/tests/test_install_doctor.py',
  'backend/tests/test_llm_credential_operation_close.py',
  'backend/tests/test_phi_redactor.py',
  'backend/tests/test_project_review_targets.py',
  'backend/tests/test_remote_no_secret_leak_063.py',
  'backend/tests/test_system_llm_credential.py',
  'scripts/tests/test_acceptance_mutations.py',
  'scripts/tests/test_install_doctor.py',
];
const token = letter => ['gh', 'p_', letter.repeat(36)].join('');
const privateHeader = ['-----', 'BEGIN ', 'PRIVATE ', 'KEY', '-----'].join('');

function git(repository, args) {
  return execFileSync('git', ['--no-replace-objects', ...args], {
    cwd: repository, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function write(repository, path, contents) {
  mkdirSync(dirname(join(repository, path)), { recursive: true });
  writeFileSync(join(repository, path), contents);
}

function commit(repository) {
  git(repository, ['add', '-A']);
  git(repository, ['-c', 'user.name=Publication test', '-c', 'user.email=publication@example.test',
    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Synthetic fixture']);
  return git(repository, ['rev-parse', 'HEAD']);
}

function fixture(t, files = {}) {
  const repository = mkdtempSync(join(tmpdir(), 'publication-fixtures-'));
  t.after(() => rmSync(repository, { recursive: true, force: true }));
  git(repository, ['init', '-q']);
  for (const path of [CHECKER, CONTRACT, MODULE]) {
    mkdirSync(dirname(join(repository, path)), { recursive: true });
    copyFileSync(join(SOURCE, path), join(repository, path));
  }
  for (const path of FIXTURES) write(repository, path, '# Synthetic fixture.\n');
  for (const [path, contents] of Object.entries(files)) write(repository, path, contents);
  return { repository, base: commit(repository) };
}

function run({ repository, base }, args = ['--working-tree']) {
  const result = spawnSync(process.execPath, [join(repository, CHECKER), '--base', base, ...args], {
    cwd: repository, encoding: 'utf8', maxBuffer: 1_048_576,
  });
  assert.equal(result.stderr, '');
  assert.ok(result.status === 0 || result.status === 1, `Checker did not return a bounded result: ${result.status}`);
  const diagnostics = JSON.parse(result.stdout);
  assert.ok(Array.isArray(diagnostics));
  for (const item of diagnostics) assert.deepEqual(Object.keys(item), ['path', 'reason']);
  return { ...result, diagnostics };
}

function rejected(result, path, reason = 'publication_secret', samples = []) {
  assert.equal(result.status, 1);
  assert.ok(result.diagnostics.some(item => item.path === path && item.reason === reason),
    `Expected ${reason} for ${path}, received ${JSON.stringify(result.diagnostics)}`);
  for (const sample of samples) assert.equal(result.stdout.includes(sample), false);
}

test('contract pins the shared scanner and every required source fixture', () => {
  const contract = JSON.parse(readFileSync(join(SOURCE, CONTRACT), 'utf8'));
  assert.deepEqual(contract.fixtures, FIXTURES);
  assert.equal(contract.scanner.contract, 'astral-publication-secrets/v1');
  assert.equal(createHash('sha256').update(readFileSync(join(SOURCE, MODULE))).digest('hex'), contract.scanner.sha256);
});

test('an unrelated edit inherits exactly the token count from the immutable base blob', t => {
  const sample = token('a');
  const f = fixture(t, { 'sample.py': `value = '${sample}'\n` });
  write(f.repository, 'sample.py', `value = '${sample}'\nunrelated = True\n`);
  const head = commit(f.repository);
  assert.equal(run(f, ['--head', head]).status, 0);
  assert.equal(run(f, []).status, 0);
});

test('extra copies, new values and newly tracked copies cannot inherit unrelated allowances', async t => {
  for (const [name, contents, path] of [
    ['extra copy', `${token('a')}\n${token('a')}\n`, 'sample.py'],
    ['new value', `${token('b')}\n`, 'sample.py'],
    ['new path', `${token('a')}\n`, 'new.py'],
  ]) {
    await t.test(name, sub => {
      const f = fixture(sub, { 'sample.py': `${token('a')}\n` });
      write(f.repository, path, contents);
      const head = commit(f.repository);
      rejected(run(f, ['--head', head]), path, 'publication_secret', [token('a'), token('b')]);
    });
  }
});

test('private-key headers are rejected even when inherited from a baseline blob', t => {
  const f = fixture(t, { 'sample.py': `${privateHeader}\n` });
  write(f.repository, 'sample.py', `${privateHeader}\nunrelated\n`);
  rejected(run(f), 'sample.py', 'publication_secret', [privateHeader]);
});

test('explicitly registered synthetic values are never baseline-exempt', async t => {
  const sample = ['registered', '-synthetic-', 'credential'].join('');
  const f = fixture(t, { 'sample.py': `${sample}\n` });
  write(f.repository, 'sample.py', `${sample}\nunrelated\n`);
  const { checkPublicationFixtures } = await import(pathToFileURL(join(f.repository, CHECKER)).href);
  const diagnostics = await checkPublicationFixtures({ ...f, workingTree: true, registeredSecrets: [sample] });
  assert.deepEqual(diagnostics, [{ path: 'sample.py', reason: 'publication_secret' }]);
  assert.equal(JSON.stringify(diagnostics).includes(sample), false);
  assert.deepEqual(await checkPublicationFixtures({ ...f, registeredSecrets: null }),
    [{ path: '<repository>', reason: 'invalid_arguments' }]);
});

test('all named fixtures are strict even when their source is unchanged', t => {
  const sample = token('c');
  const f = fixture(t, Object.fromEntries(FIXTURES.map(path => [path, `value = '${sample}'\n`])));
  const result = run(f, ['--head', f.base]);
  assert.equal(result.status, 1);
  assert.deepEqual(result.diagnostics, FIXTURES.map(path => ({ path, reason: 'publication_secret' })));
  assert.equal(result.stdout.includes(sample), false);
});

test('named fixtures may construct detector payloads at runtime without publishing their shape', t => {
  const f = fixture(t);
  write(f.repository, FIXTURES[0], 'value = "gh" + "p_" + "z" * 36\n');
  assert.equal(run(f).status, 0);
});

test('working-tree checks include staged-only, unstaged and untracked candidate bytes', t => {
  const f = fixture(t, { 'staged.py': 'before\n', 'unstaged.py': 'before\n' });
  write(f.repository, 'staged.py', `${token('d')}\n`);
  git(f.repository, ['add', 'staged.py']);
  write(f.repository, 'staged.py', 'clean working copy\n');
  write(f.repository, 'unstaged.py', `${token('e')}\n`);
  write(f.repository, 'untracked.py', `${token('f')}\n`);
  const result = run(f);
  assert.deepEqual(result.diagnostics.map(item => item.path), ['staged.py', 'unstaged.py', 'untracked.py']);
  for (const sample of ['d', 'e', 'f'].map(token)) assert.equal(result.stdout.includes(sample), false);
});

test('candidate index and worktree edits cannot replace the selected baseline or immutable head', t => {
  const inherited = token('g');
  const f = fixture(t, { 'sample.py': `${inherited}\n` });
  write(f.repository, 'sample.py', `${inherited}\nhead edit\n`);
  const head = commit(f.repository);
  write(f.repository, 'sample.py', `${token('h')}\n`);
  git(f.repository, ['add', 'sample.py']);
  write(f.repository, 'sample.py', `${token('i')}\n`);
  assert.equal(run(f, ['--head', head]).status, 0);
  rejected(run(f), 'sample.py');
});

test('clean unstaged fixture repairs are inspected instead of unchanged index bytes', t => {
  const f = fixture(t, { [FIXTURES[0]]: `${token('j')}\n` });
  write(f.repository, FIXTURES[0], 'value = "gh" + "p_" + "j" * 36\n');
  assert.equal(run(f).status, 0);
});

test('missing, mutable and non-commit base references fail closed without Git error output', async t => {
  for (const mode of ['missing', 'mutable', 'tree']) {
    await t.test(mode, sub => {
      const f = fixture(sub);
      const base = mode === 'missing' ? '0'.repeat(40) : mode === 'mutable' ? 'HEAD'
        : git(f.repository, ['rev-parse', `${f.base}^{tree}`]);
      rejected(run({ ...f, base }), '<repository>', mode === 'mutable' ? 'invalid_arguments' : 'invalid_base');
    });
  }
});

test('missing and hash-tampered base blobs cannot supply inherited token allowances', async t => {
  for (const mode of ['missing', 'tampered']) {
    await t.test(mode, sub => {
      const f = fixture(sub, { 'sample.py': `${token('k')}\n` });
      write(f.repository, 'sample.py', `${token('k')}\nnew line\n`);
      const head = commit(f.repository);
      const oid = git(f.repository, ['rev-parse', `${f.base}:sample.py`]);
      const location = join(f.repository, '.git/objects', oid.slice(0, 2), oid.slice(2));
      if (mode === 'missing') rmSync(location);
      else {
        const replacement = Buffer.from('altered baseline\n');
        rmSync(location);
        writeFileSync(location, deflateSync(Buffer.concat([
          Buffer.from(`blob ${replacement.length}\0`), replacement,
        ])));
      }
      rejected(run(f, ['--head', head]), 'sample.py', 'baseline_blob_invalid', [token('k')]);
    });
  }
});

test('tampered base trees cannot prove that a candidate path is new', t => {
  const f = fixture(t);
  const oid = git(f.repository, ['rev-parse', `${f.base}^{tree}`]);
  const replacement = Buffer.from('');
  const location = join(f.repository, '.git/objects', oid.slice(0, 2), oid.slice(2));
  rmSync(location);
  writeFileSync(location,
    deflateSync(Buffer.concat([Buffer.from(`tree ${replacement.length}\0`), replacement])));
  rejected(run(f), '<repository>', 'invalid_base_tree');
});

test('missing heads, incompatible options and duplicate flags produce bounded argument failures', t => {
  const f = fixture(t);
  rejected(run(f, ['--head', '0'.repeat(40)]), '<repository>', 'invalid_head');
  for (const args of [['--head', f.base, '--working-tree'], ['--base', f.base], ['--unknown'], ['--head', 'HEAD']]) {
    rejected(run(f, args), '<repository>', 'invalid_arguments');
  }
});

test('strict fixtures cannot disappear, become binary or be replaced with a symlink', async t => {
  for (const [mode, reason] of [
    ['missing', 'fixture_missing'], ['binary', 'fixture_not_text'], ['symlink', 'fixture_not_regular'],
  ]) {
    await t.test(mode, sub => {
      const f = fixture(sub);
      const path = FIXTURES[0];
      if (mode === 'binary') write(f.repository, path, Buffer.from([0, 1, 2]));
      else {
        rmSync(join(f.repository, path));
        if (mode === 'symlink') symlinkSync('missing-target', join(f.repository, path));
      }
      rejected(run(f), path, reason);
    });
  }
});

test('scanner byte tampering fails before the modified JavaScript can execute', t => {
  const f = fixture(t);
  write(f.repository, MODULE, `${readFileSync(join(f.repository, MODULE), 'utf8')}\nthrow new Error('not executed');\n`);
  rejected(run(f), MODULE, 'scanner_integrity');
});

test('contract version and scanner digest tampering fail closed', async t => {
  for (const field of ['version', 'digest']) {
    await t.test(field, sub => {
      const f = fixture(sub);
      const contract = JSON.parse(readFileSync(join(f.repository, CONTRACT), 'utf8'));
      if (field === 'version') contract.scanner.contract = 'unexpected';
      else contract.scanner.sha256 = '0'.repeat(64);
      write(f.repository, CONTRACT, JSON.stringify(contract));
      rejected(run(f), CONTRACT, 'invalid_fixture_contract');
    });
  }
});

test('harmless binary changes remain allowed while unusual text paths are inspected', t => {
  const f = fixture(t);
  write(f.repository, 'binary.bin', Buffer.from([0, 0xFF, 0x01, 0xFE]));
  const path = 'quoted "path"\nname.txt';
  write(f.repository, path, `${token('m')}\n`);
  const result = run(f);
  rejected(result, path, 'publication_secret', [token('m')]);
  assert.equal(result.diagnostics.length, 1);
});

test('credential detections inspect NUL and invalid UTF-8 in every candidate source', async t => {
  for (const source of ['new', 'modified', 'staged', 'head']) {
    for (const encoding of ['nul', 'invalid-utf8']) {
      for (const kind of ['token', 'registered', 'private-key']) {
        await t.test(`${source}/${encoding}/${kind}`, async sub => {
          const path = 'evidence.bin';
          const f = fixture(sub, source === 'new' ? {} : { [path]: 'harmless baseline\n' });
          const sample = kind === 'token' ? token('q') : kind === 'private-key' ? privateHeader
            : ['registered', 'binary', 'fixture'].join('-');
          const boundary = encoding === 'nul' ? Buffer.from([0]) : Buffer.from([0xFF, 0xFE]);
          write(f.repository, path, Buffer.concat([boundary, Buffer.from(sample), boundary]));
          let head;
          if (source === 'head') {
            head = commit(f.repository);
            write(f.repository, path, 'harmless working copy\n');
          } else if (source === 'staged') {
            git(f.repository, ['add', path]);
            write(f.repository, path, 'harmless working copy\n');
          }
          let result;
          if (kind === 'registered') {
            const { checkPublicationFixtures } = await import(pathToFileURL(join(f.repository, CHECKER)).href);
            const diagnostics = await checkPublicationFixtures({
              ...f, ...(head ? { head } : { workingTree: true }), registeredSecrets: [sample],
            });
            result = { diagnostics, status: diagnostics.length ? 1 : 0, stdout: JSON.stringify(diagnostics) };
          } else result = run(f, head ? ['--head', head] : ['--working-tree']);
          rejected(result, path, 'publication_secret', [sample]);
        });
      }
    }
  }
});

test('lossy UTF-8 baseline conversion preserves inherited counts but refuses an extra binary copy', t => {
  const sample = token('r');
  const prefix = Buffer.from([0xFF, 0]);
  const original = Buffer.concat([prefix, Buffer.from(sample)]);
  const f = fixture(t, { 'evidence.bin': original });
  write(f.repository, 'evidence.bin', Buffer.concat([original, Buffer.from('\nunrelated\n')]));
  assert.equal(run(f).status, 0);
  write(f.repository, 'evidence.bin', Buffer.concat([original, Buffer.from(`\n${sample}\n`)]));
  rejected(run(f), 'evidence.bin', 'publication_secret', [sample]);
});

test('credential-shaped filenames are redacted from diagnostics', t => {
  const f = fixture(t);
  const path = `${token('n')}.txt`;
  write(f.repository, path, `${token('o')}\n`);
  rejected(run(f), '<redacted-path>', 'publication_secret', [token('n'), token('o')]);
});

test('working-tree symlinks are inspected as link text without following their targets', t => {
  const f = fixture(t);
  const outside = mkdtempSync(join(tmpdir(), 'publication-outside-'));
  t.after(() => rmSync(outside, { recursive: true, force: true }));
  writeFileSync(join(outside, 'outside.txt'), `${token('p')}\n`);
  symlinkSync(join(outside, 'outside.txt'), join(f.repository, 'link.txt'));
  assert.equal(run(f).status, 0);
});
