"""Fail CI when tracked files contain forbidden runtime artifacts or obvious secrets."""
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
forbidden_suffixes = {'.nes', '.bin', '.iso', '.chd', '.cue', '.rom', '.bios', '.srm', '.dosz', '.tar', '.zip', '.7z', '.rar', '.cache', '.png', '.jpg', '.jpeg'}
forbidden_dirs = {'data', 'library', 'payload', 'state', 'backups', 'metadata-cache', '__pycache__'}
problems = []
for rel in filter(None, files):
    path = root / rel
    if path.suffix.lower() in forbidden_suffixes or forbidden_dirs.intersection(Path(rel).parts) or Path(rel).name == '.env':
        problems.append(rel + ': runtime/binary artifact')
        continue
    if path.stat().st_size > 1024 * 1024:
        problems.append(rel + ': unexpectedly large source file')
        continue
    text = path.read_text('utf-8')
    if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)', text):
        problems.append(rel + ': potential credential')
    if re.search(r'192\.168\.\d+\.\d+|C:[\\/]Users[\\/]', text):
        problems.append(rel + ': personal deployment address/path')
if problems:
    raise SystemExit('\n'.join(problems))
print(f'Public tree check passed: {len(list(filter(None, files)))} source files')
