import {test} from 'node:test';
import assert from 'node:assert/strict';
import {detectShell, pickBash} from '../src/tools/shell.js';

// #1177 (epic #1144): per-command shell routing so BOTH bash and PowerShell work — PowerShell cmdlets run in
// PowerShell, POSIX/shell-agnostic commands in Git Bash (when installed).

test('#1177 detectShell: PowerShell cmdlets → powershell, POSIX/agnostic → bash', () => {
  assert.equal(detectShell('Get-ChildItem'), 'powershell');
  assert.equal(detectShell('Get-ChildItem -Recurse'), 'powershell');
  assert.equal(detectShell('gci | Select-Object Name'), 'powershell');
  assert.equal(detectShell('$env:PATH'), 'powershell');
  assert.equal(detectShell('ls -la'), 'bash');
  assert.equal(detectShell('cd /x && ls -la'), 'bash');
  assert.equal(detectShell('grep -rn foo .'), 'bash');
  assert.equal(detectShell('git status'), 'bash'); // shell-agnostic defaults to bash
});

test('#1177 pickBash: Program Files, Scoop, or an override; POSIX → null', () => {
  const gitBashPath = 'C:\\Program Files\\Git\\bin\\bash.exe';
  assert.equal(pickBash('win32', (p) => p === gitBashPath), gitBashPath);
  const scoop = 'C:\\Users\\me\\scoop\\apps\\git\\current\\bin\\bash.exe'; // #1183: Scoop install
  assert.equal(pickBash('win32', (p) => p === scoop, undefined, 'C:\\Users\\me'), scoop);
  assert.equal(pickBash('win32', () => false), null); // none of the well-known paths → PATH fallback / PowerShell
  assert.equal(pickBash('linux', () => true), null); // POSIX uses the default shell
  assert.equal(pickBash('win32', (p) => p === 'D:\\git\\bash.exe', 'D:\\git\\bash.exe'), 'D:\\git\\bash.exe');
});
