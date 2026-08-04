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
import re
import base64
import pandas as pd
import os
import hashlib

# Binary instrument exports handled via GSAS-II importers (needs an on-disk path).
_BINARY_XRD_EXTENSIONS = {".raw"}

# Fingerprints for ICDD PDF / calculated reflection-list cards whose header looks
# like "2-Theta  d(?)  I(f)  ( h k l)  ...". These are sparse stick patterns, not
# measured scans, and intensity is the I(f) column (3rd number), not the d column.
_REFLECTION_IF_RE = re.compile(r"I\(\s*f\s*\)", re.IGNORECASE)
_REFLECTION_HKL_RE = re.compile(r"h\s+k\s+l", re.IGNORECASE)
_PAREN_GROUP_RE = re.compile(r"\([^)]*\)")


def parse_columnar_xrd_text(text):
    """
    Parse a generic 2/3-column powder pattern (whitespace, comma, or semicolon
    delimited) into a DataFrame with ``Angle``/``Intensity`` columns (and an
    optional ``Sigma`` column when a third column is present throughout).

    Comment lines starting with ``#``, ``;``, ``!``, ``%`` or ``//`` are skipped,
    as are blank lines and any row that does not start with two parseable floats.
    """
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "!", "%", "//")):
            continue
        parts = [part for part in re.split(r"[\s,;]+", stripped) if part]
        if len(parts) < 2:
            continue
        try:
            angle = float(parts[0])
            intensity = float(parts[1])
            sigma = float(parts[2]) if len(parts) >= 3 else None
        except ValueError:
            continue
        rows.append((angle, intensity, sigma))

    if not rows:
        raise ValueError(
            "Could not parse diffraction data. Expected a LOOP CSV with "
            "'Angle,Intensity' or a 2/3-column powder pattern text file."
        )

    df = pd.DataFrame(rows, columns=["Angle", "Intensity", "Sigma"])
    if df["Sigma"].isna().all():
        df = df.drop(columns=["Sigma"])
    return df


# Physical upper bound for a 2θ axis (max is 180°; real scans stop well below).
# A parsed "angle" beyond this means the columns were misread.
_MAX_PLAUSIBLE_TWO_THETA = 180.0


def is_rigaku_asc_text(text):
    """True if ``text`` looks like a Rigaku ASCII export (implicit 2θ axis).

    These store the angle axis only as ``*START``/``*STOP``/``*STEP`` header
    keys, with the data section holding intensity counts (several per line). They
    are commonly saved with a ``.csv``/``.asc`` extension.
    """
    head = text[:4000].upper()
    return ("*START" in head and "*STEP" in head) or "RAS_DATA_START" in head


def parse_rigaku_asc_text(text):
    """Parse a Rigaku ASCII pattern: .ras files carry explicit angle/intensity rows;
    .asc files reconstruct the 2θ axis from the ``*START``/``*STOP``/``*STEP`` headers."""
    if "RAS_DATA_START" in text[:4000].upper():
        return _parse_rigaku_ras_rows(text)

    def _header_value(key):
        match = re.search(rf"\*{key}\s*=\s*([0-9.eE+-]+)", text)
        return float(match.group(1)) if match else None

    start = _header_value("START")
    stop = _header_value("STOP")
    step = _header_value("STEP")

    intensities = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        tokens = [tok for tok in re.split(r"[\s,;]+", stripped) if tok]
        try:
            values = [float(tok) for tok in tokens]
        except ValueError:
            continue  # not a pure data line
        intensities.extend(values)

    if start is None or not intensities:
        raise ValueError("Rigaku ASCII file is missing *START or has no data.")

    n = len(intensities)
    # Prefer the exact endpoints over the rounded *STEP when both are present.
    if stop is not None and n > 1:
        effective_step = (stop - start) / (n - 1)
    elif step:
        effective_step = step
    else:
        raise ValueError("Rigaku ASCII file is missing *STEP/*STOP.")

    angles = [start + i * effective_step for i in range(n)]
    return pd.DataFrame({"Angle": angles, "Intensity": intensities})


def _parse_rigaku_ras_rows(text):
    """Parse .ras data rows: ``angle intensity [attenuation]`` per line."""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        tokens = [tok for tok in re.split(r"[\s,;]+", stripped) if tok]
        if len(tokens) < 2:
            continue
        try:
            angle, intensity = float(tokens[0]), float(tokens[1])
        except ValueError:
            continue
        if 0 <= angle <= _MAX_PLAUSIBLE_TWO_THETA:
            rows.append((angle, intensity))
    if not rows:
        raise ValueError("Rigaku .ras file has no data rows.")
    return pd.DataFrame(rows, columns=["Angle", "Intensity"])


def _extract_rigaku_metadata(text):
    """Collect the ``*KEY = value`` header fields from a Rigaku ASCII export."""
    metadata = [("Source", "Rigaku ASCII export")]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("*") or "=" not in stripped:
            continue  # skip data rows and markers like *BEGIN/*END/*EOF
        key, value = stripped[1:].split("=", 1)
        key = key.strip()
        if key:
            metadata.append((key, value.strip()))
    return metadata


def is_reflection_list_text(text):
    """True if ``text`` looks like an ICDD PDF / calculated reflection-list card."""
    head = text[:8000]
    return bool(_REFLECTION_IF_RE.search(head)) and bool(_REFLECTION_HKL_RE.search(head))


def parse_reflection_list_text(text):
    """
    Parse an ICDD PDF / calculated reflection-list card into a stick pattern.

    Rows look like ``36.681  2.4480  62.1  ( 1 1 1)  18.341 ...`` where the
    columns are ``2-Theta, d, I(f), (h k l), ...``. Intensity is the I(f) column,
    *not* the d column. We locate the data header, then for each row strip the
    parenthesised ``(h k l)`` group and read the remaining numbers, taking the
    1st as the angle and the 3rd as the intensity. The returned DataFrame is
    tagged ``plot_style="stick"`` so :func:`render_xrd_plot` draws vertical
    reflections instead of a connected line.
    """
    lines = text.splitlines()
    header_index = None
    for idx, line in enumerate(lines):
        if _REFLECTION_IF_RE.search(line) and _REFLECTION_HKL_RE.search(line):
            header_index = idx
            break

    data_lines = lines[header_index + 1:] if header_index is not None else lines
    rows = []
    for line in data_lines:
        # Data rows always carry the parenthesised (h k l); this also screens out
        # preamble lines such as "CELL: 7.309 x 7.522 ..." that have no parens.
        if not _PAREN_GROUP_RE.search(line):
            continue
        cleaned = _PAREN_GROUP_RE.sub(" ", line)
        nums = []
        for token in re.split(r"[\s,;]+", cleaned.strip()):
            if not token:
                continue
            try:
                nums.append(float(token))
            except ValueError:
                continue
        if len(nums) < 3:
            continue
        rows.append((nums[0], nums[2]))

    if not rows:
        raise ValueError("No reflections could be parsed from the PDF reference card.")

    df = pd.DataFrame(rows, columns=["Angle", "Intensity"])
    df.attrs["plot_style"] = "stick"
    return df


def _extract_reflection_metadata(text):
    """Pull a few useful header fields off a PDF reference card."""
    metadata = [("Source", "ICDD PDF reference card")]
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if first_line:
        metadata.append(("Reference", first_line))
    lam = re.search(r"Lambda\s*=\s*([0-9.]+)", text, re.IGNORECASE)
    if lam:
        metadata.append(("Lambda", lam.group(1)))
    return metadata


def parse_xrd_file(file_or_path, filename):
    """
    Parse an XRD file and return ``(metadata, df)`` like :func:`xrd_parse`.
    ``df`` always carries ``Angle``/``Intensity`` columns so it can be handed
    straight to :func:`render_xrd_plot`.

    Format is chosen by *content* (not just extension), since the same logical
    format may arrive as ``.txt`` or ``.csv``:

      * binary Bruker ``.raw``        -> GSAS-II importer (needs an on-disk path)
      * ICDD PDF reflection-list card -> stick pattern, intensity = I(f) column
      * LOOP CSV (``Angle,Intensity``)-> metadata header + data
      * everything else               -> generic 2/3-column powder pattern
    """
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in _BINARY_XRD_EXTENSIONS:
        # Imported lazily so the text paths never pull in the GSAS-II runtime.
        from catalog.gsas_runtime import read_powder_pattern

        if hasattr(file_or_path, "read"):
            raise ValueError(
                "Binary XRD formats must be parsed from a file path, not a stream."
            )
        return [], read_powder_pattern(str(file_or_path))

    text = _read_xrd_text(file_or_path)

    if is_reflection_list_text(text):
        return _extract_reflection_metadata(text), parse_reflection_list_text(text)

    if is_rigaku_asc_text(text):
        return _extract_rigaku_metadata(text), parse_rigaku_asc_text(text)

    if "Angle,Intensity" in text:
        return xrd_parse(io.StringIO(text))

    df = parse_columnar_xrd_text(text)
    # Guard against silently mis-parsing an unrecognised layout: a real 2θ axis
    # never exceeds 180°, so anything beyond that means we grabbed the wrong
    # column. Fail loudly here so the caller records a warning instead of
    # rendering a meaningless plot.
    if float(df["Angle"].max()) > _MAX_PLAUSIBLE_TWO_THETA:
        raise ValueError(
            "Parsed 2θ values exceed 180°, so this file does not look like a "
            "two-column angle/intensity pattern. Its format may be unsupported."
        )
    return [], df


def _read_xrd_text(file_or_path):
    """Read text from a file-like object (bytes or str) or a filesystem path."""
    if hasattr(file_or_path, "read"):
        content = file_or_path.read()
        if hasattr(file_or_path, "seek"):
            file_or_path.seek(0)
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="ignore")
        return str(content)
    with open(file_or_path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


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
    if df.attrs.get("plot_style") == "stick":
        # Sparse reflection list (e.g. ICDD PDF card): draw discrete reflections
        # as vertical sticks rather than connecting them with a line.
        ax.vlines(df["Angle"], 0, df["Intensity"], lw=max(linewidth, 0.8), color="C0")
        ax.set_ylim(bottom=0)
    else:
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

