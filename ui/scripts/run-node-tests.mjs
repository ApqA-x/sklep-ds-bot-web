#!/usr/bin/env node
// R26-11/V26-28: обход *.test.ts(x) без shell-glob. `node --test 'src/**/*.test.ts'`
// под npm на Windows (cmd) не раскрывает одинарные кавычки — получался нулевой
// отбор при exit 0. Здесь файл-лист считается в JS (пути относительные POSIX),
// npm test остаётся кроссплатформенным.
import { readdirSync } from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const UI_ROOT = path.resolve(SCRIPT_DIR, '..');

const TEST_SUFFIXES = ['.test.ts', '.test.tsx'];

// rel-путь всегда через '/' (в т.ч. на Windows), чтобы аргументы `node --test`
// были одинаковы на всех платформах
export function collectTestFiles(rootDir, relDir = '') {
  const found = [];
  for (const entry of readdirSync(path.join(rootDir, relDir), { withFileTypes: true })) {
    const rel = relDir ? `${relDir}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      found.push(...collectTestFiles(rootDir, rel));
    } else if (entry.isFile() && TEST_SUFFIXES.some((s) => entry.name.endsWith(s))) {
      found.push(rel);
    }
  }
  return found.sort();
}

function parseArgs(argv) {
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--root') return argv[i + 1] ?? '';
    if (argv[i].startsWith('--root=')) return argv[i].slice('--root='.length);
  }
  return '';
}

function main() {
  const rootArg = parseArgs(process.argv.slice(2));
  let files;
  let cwd;
  if (rootArg) {
    // фикстура/монорепо: сканируем указанный корень, аргументы относительны к нему
    const root = path.resolve(rootArg);
    cwd = root;
    try {
      files = collectTestFiles(root);
    } catch {
      files = [];
    }
  } else {
    // npm test из ui/: те же src/...-пути и тот же cwd, что у старого
    // `node --test 'src/**/*.test.ts'`
    cwd = UI_ROOT;
    try {
      files = collectTestFiles(path.join(UI_ROOT, 'src')).map((f) => `src/${f}`);
    } catch {
      files = [];
    }
  }
  if (files.length === 0) {
    console.error('R26-11: no test files selected');
    process.exit(1);
  }
  const res = spawnSync(process.execPath, ['--test', ...files], { cwd, stdio: 'inherit' });
  if (res.error) {
    console.error(`R26-11: ${res.error.message}`);
    process.exit(1);
  }
  process.exit(res.status ?? 1);
}

function isDirectRun() {
  const entry = process.argv[1];
  if (!entry) return false;
  const self = fileURLToPath(import.meta.url);
  if (path.resolve(entry) === self) return true;
  // Windows: регистр диска в argv/cwd и в file-URL может различаться
  return (
    process.platform === 'win32' &&
    path.resolve(entry).toLowerCase() === self.toLowerCase()
  );
}

if (isDirectRun()) {
  main();
}
