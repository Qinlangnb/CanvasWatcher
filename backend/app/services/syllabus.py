import re

import fitz
from bs4 import BeautifulSoup


def extract_html_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "nav", "noscript"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def extract_pdf_text(content: bytes) -> str:
    with fitz.open(stream=content, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document).strip()

