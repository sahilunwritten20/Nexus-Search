f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()

# Fix docstring indentation: 5 spaces -> 8 spaces
content = content.replace(
    b'    def test_crawler_cli_embeds_via_sync(self):\r\n     """Bug',
    b'    def test_crawler_cli_embeds_via_sync(self):\r\n        """Bug'
)

with open(f, 'wb') as fp:
    fp.write(content)
print('Fixed')