f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()
idx = content.find(b'def test_crawler_cli_embeds_via_sync')
line = content[idx:idx+80]
for i, b in enumerate(line):
    ch = chr(b) if 32 <= b < 127 else '.'
    if i >= 40 and i < 60:
        print(f'{i:3d}: {b:02x} ({ch})')