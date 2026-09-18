from nexus_search.ingestion.mime import detect_mime_type


def test_pdf_mime_type():
    assert detect_mime_type("document.pdf") == "application/pdf"


def test_docx_mime_type():
    assert (
        detect_mime_type("document.docx")
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_xlsx_mime_type():
    assert (
        detect_mime_type("spreadsheet.xlsx")
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_pptx_mime_type():
    assert (
        detect_mime_type("presentation.pptx")
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_unknown_mime_type():
    assert detect_mime_type("file.unknownxyz") == "application/octet-stream"