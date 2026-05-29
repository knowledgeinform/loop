# Script for plotting XRD Data
# may add other utility functions here in the future
# (e.g. the actual data processing / numerical 
# heavy duty stuff)

from django.utils.text import get_valid_filename
from django.utils import timezone

import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg') # headless mode for scripts
import io
import base64
import pandas as pd
import os
import hashlib

def xrd_parse(csv_file):
    """
    Reads XRD CSV file with metadata headers before the data.
    Returns:
      - metadata: list of (key, value) tuples
      - df: DataFrame with Angle and Intensity columns
    """
    
    # Support file-like or filepath input
    if hasattr(csv_file, 'read'):
        content = csv_file.read()
        if isinstance(content, bytes):
            content = content.decode('utf-8')
        lines = content.splitlines()
    else:
        with open(csv_file, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()

    header_line_index = None
    for i, line in enumerate(lines):
        if line.strip() == "Angle,Intensity":
            header_line_index = i
            break

    if header_line_index is None:
        raise ValueError("CSV header 'Angle,Intensity' not found.")

    # Extract metadata lines: all lines before the header line
    metadata_lines = lines[:header_line_index]

    metadata = []
    for line in metadata_lines:
        # Split by commas but preserve all parts (even if > 2 columns)
        parts = [p.strip() for p in line.split(",")]

        # If just one item or empty, store as key with empty value
        if len(parts) == 1:
            metadata.append((parts[0], ""))
        else:
            key = parts[0]
            value = ", ".join(parts[1:])  # join rest as one string
            metadata.append((key, value))

    # Extract data part as DataFrame
    data_str = "\n".join(lines[header_line_index:])
    df = pd.read_csv(io.StringIO(data_str))

    return metadata, df

def render_xrd_plot(
    df,
    figsize=(8,5),
    linewidth=0.5,
    dpi=150,
    encode_base64=True
):
    """
    Given a DataFrame with 'Angle' and 'Intensity' cols, draw
    the standard XRD pattern. If encode_base64=True returns a
    base64-encoded PNG data URI; otherwise returns the raw bytes.
    """
    fig, ax = plt.subplots(figsize=figsize)
    ax.plot(df["Angle"], df["Intensity"], lw=linewidth)
    ax.set_xlabel("Angle (2θ)")
    ax.set_ylabel("Intensity [a.u.]")
    ax.set_title("X-ray Diffraction Pattern")
    ax.grid(True)
    # clean up
    for spine in ("top","right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    plt.close(fig)
    buf.seek(0)
    raw = buf.read()

    if encode_base64:
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:image/png;base64,{b64}"
    else:
        return raw

# creates plot with peak locations overlaid
def render_overlay_plot(df, peaks):
    fig, ax = plt.subplots()

    ax.plot(df["Angle"], df["Intensity"])  # no label, so no legend

    for pk in peaks:
        tt = pk["two_theta"] if isinstance(pk, dict) else pk[0]
        if tt is None:
            continue
        ax.axvline(float(tt), linestyle="--", color="red", alpha=0.6, linewidth=1)  # no label

    ax.set_xlabel("2θ (°)")
    ax.set_ylabel("Intensity")
    ax.set_title("XRD Pattern")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    datauri = "data:image/png;base64," + base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return datauri


# for removing degrees symbols, etc.
def sanitize_filename(filename):
    """
    Sanitize filename using Django's built-in function to prevent 
    path traversal and remove problematic characters
    """
    filename = get_valid_filename(filename)
    if not filename.lower().endswith('.csv'):
        filename += '.csv'
    return filename


def timestamped(instance, orig_name):
    base, ext = os.path.splitext(orig_name)
    ts = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    return os.path.join("uploads", f"{base}_{ts}{ext}")


def get_sha256(uploaded_file):
    """
    Compute SHA-256 while streaming; leaves pointer at start.
    """
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
    uploaded_file.seek(0)
    return hasher.hexdigest()

