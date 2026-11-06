"""Uji cepat tanpa UI: python cli_test.py lab.pdf "anamnesis..." "pertanyaan"."""
import sys

from langflow_client import LangflowError, ask, new_session_id, read_pdf, upload_file

if len(sys.argv) != 4:
    sys.exit('Pemakaian: python cli_test.py lab.pdf "anamnesis pasien" "pertanyaan"')

pdf, anam, q = sys.argv[1:4]
try:
    data = open(pdf, "rb").read()
except OSError as e:
    sys.exit(f"File tidak bisa dibuka: {e}")

text, note = read_pdf(data)
print(f"[PDF] {len(text)} karakter terbaca. {note}")
path = None
try:
    path = upload_file(pdf.replace("\\", "/").split("/")[-1], data)
except LangflowError as e:
    print(f"[Upload dilewati] {e}")
try:
    print(ask(q, anam, path, new_session_id(), text))
except LangflowError as e:
    sys.exit(f"[GAGAL] {e}")
