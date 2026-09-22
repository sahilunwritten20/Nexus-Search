f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()

# Fix indentation for test_crawler_cli_embeds_via_sync
content = content.replace(b'def test_crawler_cli_embeds_via_sync(self):', b'    def test_crawler_cli_embeds_via_sync(self):')

with open(f, 'wb') as fp:
    fp.write(content)
print('Fixed')