"""
ingestion/extract.py
--------------------
Step 1 of the ingestion pipeline.

Reads the source PDF and produces a list of raw elements, each with:
  - text        : str
  - chapter_num : int
  - chapter_title: str
  - section_num : str  (e.g. "11.2")
  - section_title: str
  - subsection_title: str | None
  - page        : int
  - element_type: "paragraph" | "table" | "figure_caption"

Tables are converted to plain English sentences via GPT-4o before being
returned as elements with element_type="table".

Output is saved to data/processed/raw_elements_ot.jsonl for inspection.

Usage:
    python -m ingestion.extract
"""

# TODO: implement PDF text extraction using PyMuPDF (fitz)
# TODO: implement table extraction using pdfplumber
# TODO: implement GPT-4o table-to-text conversion (prompt in config.yaml: table_to_text)
# TODO: parse chapter/section/subsection from headings or TOC
# TODO: save output to cfg.paths.raw_elements_ot as JSONL


def extract_pdf(pdf_path: str) -> list[dict]:
    """
    Extract all text elements from a PDF with full metadata.

    Args:
        pdf_path: Path to the source PDF file.

    Returns:
        List of element dicts (paragraph, table, figure_caption).
    """
    raise NotImplementedError


def convert_table_to_text(table_text: str) -> str:
    """
    Convert a raw table string to readable English sentences using GPT-4o.
    Uses the 'table_to_text' prompt from config.yaml.

    Args:
        table_text: Raw table content as a string.

    Returns:
        Plain English paragraph describing the table contents.
    """
    raise NotImplementedError


if __name__ == "__main__":
    from config import cfg
    elements = extract_pdf(cfg.paths.raw_ot_pdf)
    print(f"Extracted {len(elements)} elements")
