#!/usr/bin/env python3
"""Create .env once; never rotate an existing installation's identity or keys."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
text = (root / '.env.example').read_text()
lines = []
for line in text.splitlines():
    if '=' in line and not line.startswith('#'):
        key, value = line.split('=', 1)
        if not value and key != 'COMPOSE_PROFILES' and not key.startswith('MODERATION_'):
            line = f'{key}={secrets.token_hex(24)}'
    lines.append(line)
fd = os.open(root / '.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('\n'.join(lines) + '\n')
print('Created .env. Set WEB_BASE_URL and any optional profiles before starting.')
