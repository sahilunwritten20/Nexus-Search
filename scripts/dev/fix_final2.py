f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()

# The issue: 4 spaces are on the blank line before the def line
# Current structure: .../page")\r\n\r\n    def test...
# The 4 spaces are on the blank line, not on the def line
# Fix: move the 4 spaces from the blank line to the def line

# Pattern: \r\n\r\n    def test_crawler_cli_embeds_via_sync(self):
# Should become: \r\n\r\n    def test_crawler_cli_embeds_via_sync(self):
# But the 4 spaces need to be on the def line, not the blank line

# Current bytes around function:
# .../page")\r\n\r\n    def test...
# Hex: 0a 20 20 20 20 64 65 66... (newline, 4 spaces, def)
# Should be: 0a 0a 20 20 20 20 64 65 66... (newline, newline, 4 spaces, def)

# The issue: there's only ONE newline (0a) before the 4 spaces, not two
# The 'Before' shows \r\n\r\n    but the hex shows only one 0a before the spaces

# Fix: add a newline before the 4 spaces
content = content.replace(
    b'\r\n\r\n    def test_crawler_cli_embeds_via_sync(self):',
    b'\r\n\r\n    def test_crawler_cli_embeds_via_sync(self):'
)

with open(f, 'wb') as fp:
    fp.write(content)
print('Fixed')