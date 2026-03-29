#!/usr/bin/env python3
"""
Run this script on the VPS to generate your Polymarket CLOB API keys.
It reads your wallet private key from stdin (not stored anywhere).

Usage:
  python3 setup_polymarket_keys.py
"""

import subprocess
import sys

# Install dependency if needed
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
except ImportError:
    print("Installing py-clob-client...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "py-clob-client", "--break-system-packages"])
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

import getpass
import os

print("\n=== Polymarket CLOB API Key Generator ===\n")
print("Your private key will NOT be stored or shown — it is used only to generate the API key.\n")

pk = getpass.getpass("Enter your wallet private key (starts with 0x): ").strip()

if not pk.startswith("0x"):
    pk = "0x" + pk

try:
    client = ClobClient(
        "https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON
    )
    creds = client.create_or_derive_api_creds()

    print("\n=== YOUR API CREDENTIALS ===")
    print(f"API_KEY:        {creds.api_key}")
    print(f"API_SECRET:     {creds.api_secret}")
    print(f"API_PASSPHRASE: {creds.api_passphrase}")
    print("\n=== COPY THESE INTO ~/.openclaw/.env ===\n")
    print(f"POLYMARKET_PRIVATE_KEY={pk}")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    print("\nDone. Now run:")
    print("  nano ~/.openclaw/.env")
    print("  (paste the lines above, save, done)\n")

except Exception as e:
    print(f"\nERROR: {e}")
    print("\nMake sure your private key is correct and your wallet has MATIC for gas on Polygon.")
