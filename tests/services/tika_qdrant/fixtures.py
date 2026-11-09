"""Generate small, genuine PDF/DOCX/TXT documents without binary repo assets."""

import io
import zipfile
from pathlib import Path

TEXT = "Calibration fixture: calibrate the temperature sensor at zero and 100 C."
DOCUMENTS = {
    "calibration": "Calibrate a temperature sensor using ice water at zero degrees "
    "and boiling water at 100 degrees. Adjust the offset and verify accuracy.",
    "printer": "Replace the printer paper roll, clean the print head, and "
    "check the USB printer connection when printing receipts.",
    "inventory": "Count inventory stock, record warehouse quantities, and "
    "reorder products when the inventory falls below the minimum level.",
}
TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}


def pdf_bytes() -> bytes:
    """Return a one-page PDF with a real cross-reference table and text stream."""
    stream = f"BT /F1 10 Tf 30 750 Td ({TEXT}) Tj ET\n".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        result
        + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )


def docx_bytes() -> bytes:
    """Return a valid OOXML document containing the known fixture text."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
            'package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{TEXT}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def write_documents(directory: Path) -> None:
    """Write generated fixture documents to the disposable asset directory."""
    directory.mkdir()
    for suffix, data in {
        "pdf": pdf_bytes(),
        "docx": docx_bytes(),
        "txt": TEXT.encode(),
    }.items():
        (directory / f"document.{suffix}").write_bytes(data)
