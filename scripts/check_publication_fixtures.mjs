// Checks publication fixtures and changed blobs with the shared scanner against a caller-selected immutable Git base.
// Git objects supply all baselines, while diagnostics contain only paths and bounded reason codes.

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, readlinkSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const TOOL_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SECRET_CONTRACT = 'astral-publication-secrets/v1';
const SCANNER_SHA256 = 'b77d3d57f685a384410e04dec20b2c69e2f505f1a70cb4f965b6cd4aea686896';
const MAXIMUM_BYTES = 64 * 1024 * 1024;
const IMMUTABLE_SHA = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
const BLOB_MODES = new Set(['100644', '100755', '120000']);
const REGULAR_MODES = new Set(['100644', '100755']);

class CheckFailure extends Error {
  constructor(reason, path = '<repository>') {
    super(reason);
    this.reason = reason;
    this.path = path;
  }
}

function fail(reason, path) {
  throw new CheckFailure(reason, path);
}

function validPath(path) {
  return typeof path === 'string' && path.length > 0 && !path.includes('\0') && !path.includes('\\')
    && path.split('/').every(part => part.length > 0 && part !== '.' && part !== '..');
}

function decode(bytes) {
  if (bytes.includes(0)) return null;
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function git(repository, args, input) {
  return execFileSync('git', ['--no-replace-objects', ...args], {
    cwd: repository, input, maxBuffer: MAXIMUM_BYTES, stdio: ['pipe', 'pipe', 'pipe'],
  });
}

function objectDigest(type, bytes, length) {
  return createHash(length === 40 ? 'sha1' : 'sha256')
    .update(`${type} ${bytes.length}\0`).update(bytes).digest('hex');
}

function readObject(repository, oid, type, reason, path) {
  try {
    const bytes = git(repository, ['cat-file', type, oid]);
    if (objectDigest(type, bytes, oid.length) !== oid) fail(reason, path);
    return bytes;
  } catch {
    fail(reason, path);
  }
}

function readTree(repository, revision, role) {
  if (!IMMUTABLE_SHA.test(revision ?? '')) fail(`invalid_${role}`);
  const commit = readObject(repository, revision, 'commit', `invalid_${role}`);
  const root = /^tree ([a-f0-9]+)\n/.exec(commit.toString('utf8'))?.[1];
  if (!IMMUTABLE_SHA.test(root ?? '')) fail(`invalid_${role}`);
  let rows;
  try {
    const bytes = git(repository, ['ls-tree', '-r', '-t', '-z', '--full-tree', revision]);
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    rows = text.split('\0').filter(Boolean);
  } catch {
    fail(`invalid_${role}_tree`);
  }
  const entries = new Map();
  const trees = new Set([root]);
  for (const row of rows) {
    const match = /^(\d{6}) (blob|tree|commit) ([a-f0-9]+)\t([\s\S]+)$/.exec(row);
    if (!match || !IMMUTABLE_SHA.test(match[3]) || !validPath(match[4])) fail(`invalid_${role}_tree`);
    const [, mode, type, oid, path] = match;
    if (type === 'tree') trees.add(oid);
    else entries.set(path, { mode, type, oid });
  }
  for (const oid of trees) readObject(repository, oid, 'tree', `invalid_${role}_tree`);
  return entries;
}

async function loadScanner() {
  let contract;
  let bytes;
  try {
    contract = JSON.parse(readFileSync(join(TOOL_ROOT, 'scripts/publication_fixture_contract.json'), 'utf8'));
    if (contract.version !== 'astral-publication-fixtures/v1'
      || contract.scanner?.path !== 'scripts/lib/publication-secrets.mjs'
      || contract.scanner?.contract !== SECRET_CONTRACT || contract.scanner?.sha256 !== SCANNER_SHA256
      || !Array.isArray(contract.fixtures) || contract.fixtures.length === 0
      || !contract.fixtures.every(validPath) || new Set(contract.fixtures).size !== contract.fixtures.length) {
      fail('invalid_fixture_contract', 'scripts/publication_fixture_contract.json');
    }
    bytes = readFileSync(join(TOOL_ROOT, contract.scanner.path));
  } catch (error) {
    if (error instanceof CheckFailure) throw error;
    fail('invalid_fixture_contract', 'scripts/publication_fixture_contract.json');
  }
  if (createHash('sha256').update(bytes).digest('hex') !== SCANNER_SHA256) {
    fail('scanner_integrity', contract.scanner.path);
  }
  const scanner = await import(`data:text/javascript;base64,${bytes.toString('base64')}`);
  if (scanner.PUBLICATION_SECRET_CONTRACT !== SECRET_CONTRACT
    || typeof scanner.containsPublicationSecret !== 'function') fail('scanner_contract', contract.scanner.path);
  return { scanner: scanner.containsPublicationSecret, fixtures: new Set(contract.fixtures) };
}

function indexEntries(repository) {
  let rows;
  try {
    rows = new TextDecoder('utf-8', { fatal: true })
      .decode(git(repository, ['ls-files', '--stage', '-z'])).split('\0').filter(Boolean);
  } catch {
    fail('invalid_index');
  }
  const entries = new Map();
  for (const row of rows) {
    const match = /^(\d{6}) ([a-f0-9]+) (\d)\t([\s\S]+)$/.exec(row);
    if (!match || !IMMUTABLE_SHA.test(match[2]) || !validPath(match[4])) fail('invalid_index');
    if (match[3] !== '0') fail('unmerged_index', match[4]);
    entries.set(match[4], { mode: match[1], oid: match[2], type: match[1] === '160000' ? 'commit' : 'blob' });
  }
  return entries;
}

function worktreeFile(repository, path) {
  let location = repository;
  const parts = path.split('/');
  try {
    for (const part of parts.slice(0, -1)) {
      location = join(location, part);
      const info = lstatSync(location);
      if (!info.isDirectory() || info.isSymbolicLink()) fail('unsafe_file_type', path);
    }
    location = join(location, parts.at(-1));
    const info = lstatSync(location);
    if (info.isSymbolicLink()) return { bytes: Buffer.from(readlinkSync(location)), mode: '120000' };
    if (info.isDirectory()) return { bytes: null, mode: '160000' };
    if (!info.isFile()) fail('unsafe_file_type', path);
    if (info.size > MAXIMUM_BYTES) fail('file_too_large', path);
    return { bytes: readFileSync(location), mode: info.mode & 0o111 ? '100755' : '100644' };
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    if (error instanceof CheckFailure) throw error;
    fail('working_file_unreadable', path);
  }
}

export async function checkPublicationFixtures({
  repository = process.cwd(), base, head, workingTree = false, registeredSecrets = [],
} = {}) {
  const diagnostics = new Map();
  let scanner;
  const diagnosticSecrets = Array.isArray(registeredSecrets)
    ? registeredSecrets.filter(value => typeof value === 'string') : [];
  const add = (path, reason) => {
    const safe = scanner?.(path, '', diagnosticSecrets) ? '<redacted-path>' : path;
    diagnostics.set(`${safe}\0${reason}`, { path: safe, reason });
  };
  try {
    const loaded = await loadScanner();
    scanner = loaded.scanner;
    const fixtures = loaded.fixtures;
    if (workingTree && head !== undefined) fail('invalid_arguments');
    if (!Array.isArray(registeredSecrets) || !registeredSecrets.every(value => typeof value === 'string')) {
      fail('invalid_arguments');
    }
    const baseline = readTree(repository, base, 'base');
    const baselineBytes = new Map();
    const inspect = (path, mode, bytes) => {
      const strict = fixtures.has(path);
      if (!BLOB_MODES.has(mode)) {
        if (strict) add(path, 'fixture_not_regular');
        return;
      }
      if (strict && !REGULAR_MODES.has(mode)) {
        add(path, 'fixture_not_regular');
        return;
      }
      const text = bytes.toString('utf8');
      if (strict && decode(bytes) === null) add(path, 'fixture_not_text');
      let previous = '';
      const entry = baseline.get(path);
      if (!strict && entry?.type === 'blob') {
        if (!baselineBytes.has(path)) {
          const original = readObject(repository, entry.oid, 'blob', 'baseline_blob_invalid', path);
          baselineBytes.set(path, original.toString('utf8'));
        }
        previous = baselineBytes.get(path);
      }
      if (scanner(text, previous, registeredSecrets)) add(path, 'publication_secret');
    };
    const inspectEntry = (path, entry, role) => {
      if (!BLOB_MODES.has(entry.mode) && !fixtures.has(path)) return;
      const bytes = entry.type === 'blob'
        ? readObject(repository, entry.oid, 'blob', `${role}_blob_invalid`, path) : Buffer.alloc(0);
      inspect(path, entry.mode, bytes);
    };
    if (workingTree) {
      const index = indexEntries(repository);
      let others;
      try {
        others = new TextDecoder('utf-8', { fatal: true })
          .decode(git(repository, ['ls-files', '--others', '--exclude-standard', '-z'])).split('\0').filter(Boolean);
      } catch {
        fail('invalid_working_tree');
      }
      if (!others.every(validPath)) fail('invalid_working_tree');
      for (const [path, entry] of index) {
        const original = baseline.get(path);
        if (entry.oid !== original?.oid || entry.mode !== original?.mode) {
          inspectEntry(path, entry, 'index');
        }
      }
      const paths = new Set([...index.keys(), ...others, ...fixtures]);
      for (const path of paths) {
        const file = worktreeFile(repository, path);
        if (!file) {
          if (fixtures.has(path)) add(path, 'fixture_missing');
          continue;
        }
        if (!file.bytes) {
          if (fixtures.has(path)) add(path, 'fixture_not_regular');
          continue;
        }
        const original = baseline.get(path);
        if (fixtures.has(path) || file.mode !== original?.mode
          || objectDigest('blob', file.bytes, base.length) !== original?.oid) inspect(path, file.mode, file.bytes);
      }
    } else {
      if (head === undefined) {
        try { head = git(repository, ['rev-parse', '--verify', 'HEAD']).toString('utf8').trim(); }
        catch { fail('invalid_head'); }
      }
      const candidate = readTree(repository, head, 'head');
      for (const fixture of fixtures) if (!candidate.has(fixture)) add(fixture, 'fixture_missing');
      for (const [path, entry] of candidate) {
        const original = baseline.get(path);
        if (fixtures.has(path) || entry.oid !== original?.oid || entry.mode !== original?.mode) {
          inspectEntry(path, entry, 'head');
        }
      }
    }
  } catch (error) {
    add(error instanceof CheckFailure ? error.path : '<repository>',
      error instanceof CheckFailure ? error.reason : 'check_failed');
  }
  return [...diagnostics.values()].sort((a, b) => {
    const left = `${a.path}\0${a.reason}`;
    const right = `${b.path}\0${b.reason}`;
    return left < right ? -1 : left > right ? 1 : 0;
  });
}

function argumentsFor(values) {
  const options = {};
  for (let index = 0; index < values.length; index += 1) {
    const flag = values[index];
    if (flag === '--working-tree' && options.workingTree === undefined) options.workingTree = true;
    else if ((flag === '--base' || flag === '--head') && options[flag.slice(2)] === undefined) {
      const value = values[++index];
      if (!value || !IMMUTABLE_SHA.test(value)) fail('invalid_arguments');
      options[flag.slice(2)] = value;
    } else fail('invalid_arguments');
  }
  if (!options.base || options.workingTree && options.head !== undefined) fail('invalid_arguments');
  return options;
}

if (process.argv[1] && pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url) {
  let result;
  try { result = await checkPublicationFixtures(argumentsFor(process.argv.slice(2))); }
  catch { result = [{ path: '<repository>', reason: 'invalid_arguments' }]; }
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = result.length ? 1 : 0;
}
