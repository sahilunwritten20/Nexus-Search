"""WP12 R8 — zip-bomb RSS: parent samples child peak-RSS while python-docx
parses a docx whose word/document.xml declares ~150 MiB of valid XML
(under the 512 MiB default bound). Hard cap 150 s, then kill."""
import os
import subprocess
import sys
import tempfile
import time
import zipfile

MEGA = 1024 * 1024
REPS = 2_600_000  # ~150 MiB XML

payload = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:body>' + (b'<w:p><w:r><w:t>lorem ipsum dolor sit amet </w:t></w:r></w:p>' * REPS) + b'</w:body></w:document>'
)
print(f"payload XML: {len(payload)/MEGA:.0f} MiB")

path = os.path.join(tempfile.mkdtemp(), "bomb.docx")
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>')
    zf.writestr("_rels/.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                '</Relationships>')
    zf.writestr("word/_rels/document.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    zf.writestr("word/document.xml", payload)
print(f"bomb.docx on disk: {os.path.getsize(path)/1024:.0f} KiB")

child = r"""
import sys
from nexus_search.ingestion.connectors.files import read_docx_file
from pathlib import Path
text = read_docx_file(Path(sys.argv[1]))
print(f"done text_len={len(text)}")
"""
script = os.path.join(tempfile.mkdtemp(), "r8_child.py")
with open(script, "w") as f:
    f.write(child)
env = dict(os.environ)
env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CAP = 120
proc = subprocess.Popen([sys.executable, script, path], env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def child_rss_mib(pid, debug=False):
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True).stdout.strip()
    try:
        last_field = out.rsplit(",", 1)[1]          # '"2,800 K"' or '2,800 K\n'
        kb = float(last_field.replace('"', "").replace(",", "").replace(" K", ""))
        v = kb / 1024
    except (IndexError, ValueError):
        v = 0.0
    if debug:
        print(f"  [dbg pid={pid}] raw={out!r} -> {v} MiB", flush=True)
    return v


peak, t0, first_sample = 0.0, time.time(), None
while time.time() - t0 < CAP and proc.poll() is None:
    rss = child_rss_mib(proc.pid, debug=(t0 and time.time() - t0 < 12))
    if first_sample is None:
        first_sample = rss
    peak = max(peak, rss)
    time.sleep(3)
finished = proc.poll() is not None
if not finished:
    proc.kill()
proc.wait()
try:
    child_out = proc.stdout.read().strip()[-300:]
except Exception:
    child_out = ""
print(f"child says: {child_out!r}")
print(f"first sample: {first_sample} MiB")
print(f"finished={finished} OBSERVED PEAK RSS of parser: {peak:.0f} MiB "
      f"(payload {len(payload)/MEGA:.0f} MiB XML from a {os.path.getsize(path)//1024} KiB docx, "
      f"default NEXUS_MAX_DECOMPRESSED_BYTES=512 MiB)")
