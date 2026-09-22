f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()
idx = content.find(b'def test_crawler_cli_embeds_via_sync')
# Show 20 bytes before
print('Bytes before def:')
for i, b in enumerate(content[max(0,idx-20):idx]):
    print(f'{i:3d}: {b:02x} ({chr(b) if 32 <= b < 127 else "."})')