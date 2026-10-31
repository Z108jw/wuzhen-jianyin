from __future__ import annotations

from pathlib import Path

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "测试论文.md"


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")

    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    document.save(SOURCE.with_suffix(".docx"))

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(SOURCE.with_suffix(".pdf")), pagesize=A4)
    width, height = A4
    y = height - 60
    pdf.setFont("STSong-Light", 11)
    for paragraph in text.splitlines():
        line = paragraph
        while line:
            part, line = line[:55], line[55:]
            if y < 50:
                pdf.showPage()
                pdf.setFont("STSong-Light", 11)
                y = height - 60
            pdf.drawString(48, y, part)
            y -= 18
        y -= 4
    pdf.save()
    print(SOURCE.with_suffix(".docx"))
    print(SOURCE.with_suffix(".pdf"))


if __name__ == "__main__":
    main()

