"""Uji cepat tanpa UI: python cli_test.py lab.pdf "anamnesis..." "pertanyaan"."""
import sys
from langflow_client import ask, extract_pdf_text, new_session_id, upload_file

pdf, anam, q = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(pdf, "rb").read()
path = upload_file(pdf.replace("\\", "/").split("/")[-1], data)
print(ask(q, anam, path, new_session_id(), extract_pdf_text(data)))
