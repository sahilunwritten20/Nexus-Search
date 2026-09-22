f = 'tests/core/test_phase4.py'
with open(f, 'rb') as fp:
    content = fp.read()

# Find the function definition
idx = content.find(b'def test_crawler_cli_embeds_via_sync(self):')

# The 4 spaces are at idx-4 to idx-1 (before 'def')
# The blank line ends at idx-5 (after \r\n\r\n)
# We need to move the 4 spaces from idx-4:idx to idx:idx+4 (before 'def')

# Current structure:
# content[idx-8:idx] = b'\r\n\r\n    '  (two newlines + 4 spaces)
# content[idx:idx+50] = b'def test_crawler_cli_embeds_via_sync(self):\r\n     '

# We want:
# content[idx-8:idx-4] = b'\r\n\r\n'  (two newlines, no spaces)
# content[idx-4:idx] = b'    '  (4 spaces before def)
# content[idx:idx+50] = b'def test_crawler_cli_embeds_via_sync(self):\r\n        ' (8 spaces after \r\n)

# Rebuild the section
before = content[:idx-4]  # up to but not including the 4 spaces
middle = b'    ' + content[idx:idx+50].replace(b'\r\n     ', b'\r\n        ')  # add 4 spaces before def, fix docstring
after = content[idx+50:]
new_content = before + middle + after

with open(f, 'wb') as fp:
    fp.write(new_content)
print('Fixed')