#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared image I/O for the NightScribe tools.

Used by ``tools/align_stack_recon.py`` and ``tools/mosaic_from_recon.py`` so the
loading and saving rules live in one place:

- ``load_image`` reads TIF/PNG/FITS as float32 2D (FITS via astropy; 8-bit is
  scaled to the 16-bit range).
- ``save_tiff`` writes a 16-bit TIF (tifffile if available, PIL otherwise).
- ``save_preview`` writes an 8-bit PNG for inspection, percentile-stretched and
  optionally downscaled.

Keep this module dependency-light: numpy + PIL/opencv only.
"""

import os

import cv2
import numpy as np


def load_image(path):
    """Load a TIF/PNG/FITS image as a 2D float32 array.

    FITS is read with astropy (applies BZERO/BSCALE); anything else goes through
    PIL, which handles 8/16-bit TIF and 16-bit PNG without losing range. An
    RGB image is reduced to its luminance.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".fit", ".fits", ".fts"):
        try:
            from astropy.io import fits
        except ImportError:
            raise SystemExit("Reading FITS needs astropy: pip install astropy")
        data = fits.getdata(path)
        if data.ndim > 2:
            raise SystemExit("{}: expected a 2D image".format(path))
        return np.asarray(data, dtype=np.float32)

    from PIL import Image
    arr = np.array(Image.open(path))
    if arr.ndim > 2:
        arr = arr[..., :3].mean(axis=2)
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) * 257.0   # scale to the 16-bit range
    return arr.astype(np.float32)


def save_tiff(path, arr):
    """Write a 2D array as a 16-bit TIF (tifffile if available, else PIL).

    The range is clipped to [0, 65535] and rounded; the relative scale is not
    touched (the inputs already live in that range).
    """
    out = np.rint(np.clip(arr, 0, 65535)).astype(np.uint16)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    try:
        import tifffile
        tifffile.imwrite(path, out, photometric="minisblack")
    except ImportError:
        from PIL import Image
        Image.fromarray(out, mode="I;16").save(path)


def save_preview(path, arr, max_width=2000, lo=1.0, hi=99.7):
    """Write an 8-bit PNG preview, percentile-stretched, optionally downscaled.

    Pixels at 0 are ignored when computing the stretch so a black border does
    not darken the preview.
    """
    a = arr.astype(np.float32)
    vals = a[a > 0]
    if vals.size == 0:
        vals = a.ravel()
    lo_v, hi_v = np.percentile(vals, [lo, hi])
    if hi_v <= lo_v:
        hi_v = lo_v + 1.0
    out = np.clip((a - lo_v) / (hi_v - lo_v) * 255.0, 0, 255).astype(np.uint8)
    h, w = out.shape
    if max_width and w > max_width:
        s = float(max_width) / w
        out = cv2.resize(out, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    from PIL import Image
    Image.fromarray(out, mode="L").save(path)
