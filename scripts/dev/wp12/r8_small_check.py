"""Quick check: does read_docx_file parse a small bomb at all? (5 MiB XML)"""
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.getcwd())
payload = (b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + b'<w:p><w:r><w:t>lorem ipsum dolor sit amet </w:t></w:r></w:p>' * 350_000 + b'</w:body></w:document>')
path = os.path.join(tempfile.mkdtemp(), "small.docx")
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    zf.writestr("_rels/.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    zf.writestr("word/_rels/document.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    zf.writestr("word/document.xml", payload)
print(f"payload {len(payload)/1048576:.1f} MiB, docx {os.path.getsize(path)//1024} KiB")
import tracemalloc
tracemalloc.start()
from nexus_search.ingestion.connectors.files import read_docx_file
from pathlib import Path
t0 = time.time()
text = read_docx_file(Path(path))
cur, hi = tracemalloc.get_traced_memory()
print(f"parse_time={time.time()-t0:.1f}s text_len={len(text)} tracemalloc_peak={hi/1048576:.0f} MiB")
