"""Helpers that build tiny synthetic slides and job descriptions. No real slides in the repo."""

from __future__ import annotations

from pathlib import Path

from surf_transfer.models import KIND_MRXS, KIND_SINGLE, Job, ValidationOptions


def make_pyramidal_tiff(path: Path, size=(2048, 1536), levels=3, tile=256) -> Path:
    import numpy as np
    import tifffile

    rng = np.random.default_rng(1)
    w, h = size
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path) as tw:
        for i in range(levels):
            data = rng.integers(0, 255, (h >> i, w >> i, 3), dtype=np.uint8)
            tw.write(
                data,
                tile=(tile, tile),
                photometric="rgb",
                compression="zlib",
                subfiletype=0 if i == 0 else 1,
            )
    return path


def make_flat_tiff(path: Path) -> Path:
    import numpy as np
    import tifffile

    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.zeros((300, 300, 3), np.uint8))
    return path


def make_fake_mrxs(
    root: Path,
    name="X",
    dats=("Data0000.dat", "Data0001.dat"),
    declared=None,
    with_ini=True,
    with_index=True,
    index_file=None,
) -> list[str]:
    """A directory shaped like an MRXS slide (content is not a real slide).
    Returns the member rel_paths."""
    declared = list(dats if declared is None else declared)
    folder = root / "T" / name
    folder.mkdir(parents=True, exist_ok=True)
    rels = []
    if with_index:
        (root / "T" / f"{name}.mrxs").write_bytes(b"not a real mrxs")
        rels.append(f"T/{name}.mrxs")
    if with_ini:
        lines = ["[GENERAL]", "SLIDE_ID = x"]
        if index_file:
            lines += ["[HIERARCHICAL]", f"INDEXFILE = {index_file}"]
        lines += ["[DATAFILE]", f"FILE_COUNT = {len(declared)}"]
        lines += [f"FILE_{i} = {n}" for i, n in enumerate(declared)]
        (folder / "Slidedat.ini").write_text("\n".join(lines) + "\n")
        rels.append(f"T/{name}/Slidedat.ini")
    for n in dats:
        (folder / n).write_bytes(b"dat")
        rels.append(f"T/{name}/{n}")
    return rels


def job_for(root: Path, rels: list[str], kind=KIND_SINGLE, index=None, **opts) -> Job:
    index_path = index
    if index_path is None:
        index_path = (
            next((r for r in rels if r.lower().endswith(".mrxs")), None)
            if kind == KIND_MRXS
            else rels[0]
        )
    job_kind = opts.pop("job_kind", "validate")
    extract_dir = opts.pop("extract_dir", None)
    return Job(
        kind=job_kind,
        slide_key="slide:test",
        slide_kind=kind,
        name="X",
        root_dir=str(root),
        members=tuple((r, (root / r).stat().st_size) for r in rels),
        index_path=index_path,
        options=ValidationOptions(**opts),
        extract_dir=extract_dir,
    )
