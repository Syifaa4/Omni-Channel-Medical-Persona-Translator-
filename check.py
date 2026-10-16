"""Cek koneksi lewat terminal: python check.py"""
from langflow_client import diagnose, key_info

print("API key:", key_info(), "\n")
for step, ok, detail in diagnose():
    print(("[OK]   " if ok else "[GAGAL]"), step, "-", detail)
