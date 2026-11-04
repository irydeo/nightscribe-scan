#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-align and stack the SHG reconstructions (recon) of one mosaic row.

Purpose
-------
JSol'Ex aligns frames with a local per-tile model. When the scans of one row
are not geometrically identical (non-deterministic start, preview FPS different
from the capture FPS, Dec drift), the tile has to absorb displacements of tens
of pixels. A small tile fails to match and the average comes out soft; raising
it to 128 px fixes that but does not address the cause.

This tool does the opposite: it estimates **one global transform per image**
(translation + rotation + scale) with SIFT, optionally refines it locally, and
only then averages. The result is a sharp stack you can compare with JSol'Ex's
and feed into the mosaic.

Inputs / outputs
----------------
- Reads: two or more 2D images of the same row, TIF/PNG/FITS (16-bit preferred).
- Writes: a 16-bit TIF stack, an optional PNG preview, and a JSON report with
  every transform and metric.

Algorithm
---------
1. Load TIF/PNG/FITS as float32.
2. Percentile-stretch and downsample (``--work-scale``, 0.5 by default).
3. SIFT keypoints + BFMatcher (Lowe ratio) + ``estimateAffinePartial2D`` with
   RANSAC (translation + rotation + scale). Fallback: euclidean ECC.
4. Build a common canvas: the union of the transformed images, or the
   reference's own canvas with ``--canvas ref``.
5. Optional local refine by per-tile phase correlation with a smooth field
   (``--local-tile``; off by default).
6. Match each image's disk level to the reference using the median over the
   overlap (gain only; ``--normalize gain``).
7. Stack with mean, median or sigma clipping and write a 16-bit TIF.
8. Write a JSON report so the stack can be reproduced exactly.

Usage
-----
Global alignment only::

    python3 tools/align_stack_recon.py \
        --inputs recon/0000_*.tif recon/0001_*.tif recon/0002_*.tif \
        --output stacks/row1_stacked.tif \
        --local-tile 0 --stack sigma --json stacks/row1_stacked.json

Mimic the tile-128 mode that already works in JSol'Ex, but from a global
transform::

    python3 tools/align_stack_recon.py \
        --inputs recon/*.tif --output stacks/row.tif --local-tile 128

Options
-------
--inputs        TIF/PNG/FITS of the same row, in order (required)
--output        output 16-bit TIF (required)
--json          JSON report (default: next to the TIF)
--ref N         reference image index (default 0)
--method        sift | ecc (default sift)
--work-scale    downsampling for feature detection (default 0.5)
--max-features  SIFT feature cap (default 12000)
--ratio         Lowe ratio test (default 0.8)
--ransac-px     RANSAC threshold in reduced-image pixels (default 2.0)
--canvas        union | ref (default union)
--local-tile    tile size in px for the local refine; 0 = off (default 0)
--local-step    tile step; 0 = tile/2
--max-local-shift  reject tiles with a larger residual (default 48)
--local-response   minimum phaseCorrelation response (default 0.08)
--stack         mean | median | sigma (default sigma)
--sigma         sigma-clip threshold (default 3.0)
--normalize     gain | none (default gain)
--preview       optional PNG for inspection
--quiet         no progress output

What to check
-------------
- ``rms`` > 3 px: weak overlap or noisy data.
- ``inliers`` < 50: inspect the pair; try ``--method ecc``.
- per-image ``scale`` differing by more than 1%: the capture changed rate; the
  tool corrects it, but fix it at the source.
- ``rotation`` > 1 deg: usually the automatic ellipse correction in JSol'Ex.
- The stack must not be softer than the best single frame (it is normally a
  little sharper because noise averages down).

JSON report
-----------
``reference_index``, ``canvas``, ``aspect``, ``images[]`` (each with ``global``
scale/rotation/tx/ty/inliers/rms, ``local`` stats and ``gain``), ``output``,
``elapsed_s``. Enough to reproduce the stack without re-running the fit.

Dependencies
------------
numpy, opencv-python. Optional: astropy (FITS), tifffile (TIF; PIL fallback).

See also
--------
- ``docs/GUIA_USUARIO.md`` / ``docs/USER_GUIDE.md`` — user guide, "The tools"
  and "Deep dive" sections.
- ``tools/mosaic_from_recon.py`` — stitches the per-row stacks.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys
import time

import cv2
import numpy as np


# ──────────────────────────────────────────────────────────────────────────────
# Input / output — shared with mosaic_from_recon.py
# ──────────────────────────────────────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _imgio import load_image, save_tiff, save_preview  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────────────────────

def _similarity_params(M):
    """Descompone una matriz afín 2x3 en escala, rotación (grados) y traslación."""
    scale = math.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2)
    rot = math.degrees(math.atan2(M[1, 0], M[0, 0]))
    return scale, rot, M[0, 2], M[1, 2]


def estimate_global_transform(ref, mov, work_scale=0.5, max_features=12000,
                              ratio=0.8, ransac_px=2.0, method="sift",
                              ecc_max_iter=200, verbose=True):
    """Estima la similaridad mov -> ref y devuelve (M, info).

    Se trabaja sobre una versión reducida y estirada por percentiles. ``M``
    queda en coordenadas de resolución completa.
    """
    if method == "ecc":
        return _estimate_ecc(ref, mov, work_scale, ecc_max_iter, verbose)

    def prep(a, scale):
        lo, hi = np.percentile(a, [1.0, 99.5])
        if hi <= lo:
            hi = lo + 1.0
        b = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
        if scale != 1.0:
            b = cv2.resize(b, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
        return b

    a = prep(ref, work_scale)
    b = prep(mov, work_scale)

    sift = cv2.SIFT_create(nfeatures=max_features, contrastThreshold=0.004,
                           edgeThreshold=20)
    k1, d1 = sift.detectAndCompute(a, None)
    k2, d2 = sift.detectAndCompute(b, None)
    if d1 is None or d2 is None or len(k1) < 10 or len(k2) < 10:
        if verbose:
            print("    SIFT: pocos puntos ({} / {}); probando ECC".format(
                len(k1), len(k2)))
        return _estimate_ecc(ref, mov, work_scale, ecc_max_iter, verbose)

    bf = cv2.BFMatcher()
    pairs = bf.knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < ratio * p[1].distance]
    if len(good) < 12:
        if verbose:
            print("    SIFT: solo {} parejas útiles; probando ECC".format(len(good)))
        return _estimate_ecc(ref, mov, work_scale, ecc_max_iter, verbose)

    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    M_small, inliers = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=ransac_px,
        maxIters=10000, confidence=0.999)
    if M_small is None:
        if verbose:
            print("    SIFT: RANSAC no encontró modelo; probando ECC")
        return _estimate_ecc(ref, mov, work_scale, ecc_max_iter, verbose)

    inl = inliers.ravel().astype(bool)
    pred = cv2.transform(src[inl], M_small).reshape(-1, 2)
    rms_small = float(np.sqrt(np.mean(np.sum((pred - dst[inl, 0, :]) ** 2, axis=1))))

    # Deshacer la reducción: M_full = S^-1 * M_small * S
    S = np.array([[work_scale, 0, 0], [0, work_scale, 0], [0, 0, 1]], np.float64)
    S_inv = np.array([[1.0 / work_scale, 0, 0], [0, 1.0 / work_scale, 0],
                      [0, 0, 1]], np.float64)
    M_full = S_inv.dot(np.vstack([M_small, [0, 0, 1]])).dot(S)
    M = M_full[:2, :].astype(np.float64)
    rms = rms_small / work_scale

    scale, rot, tx, ty = _similarity_params(M)
    info = {
        "method": "sift",
        "matches": len(good),
        "inliers": int(inl.sum()),
        "inlier_ratio": float(inl.sum()) / len(good),
        "rms_px": rms,
        "scale": scale,
        "rotation_deg": rot,
        "tx": tx,
        "ty": ty,
        "n_keypoints_ref": len(k1),
        "n_keypoints_mov": len(k2),
    }
    return M, info


def _estimate_ecc(ref, mov, work_scale, max_iter, verbose):
    """Fallback: ECC euclídeo (traslación + rotación) sobre la imagen reducida."""
    def prep(a):
        lo, hi = np.percentile(a, [1.0, 99.5])
        if hi <= lo:
            hi = lo + 1.0
        b = np.clip((a - lo) / (hi - lo), 0, 1).astype(np.float32)
        if work_scale != 1.0:
            b = cv2.resize(b, None, fx=work_scale, fy=work_scale,
                           interpolation=cv2.INTER_AREA)
        return b

    a = prep(ref)
    b = prep(mov)
    warp = np.eye(2, 3, dtype=np.float32)
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, max_iter, 1e-6)
    try:
        cc, warp = cv2.findTransformECC(a, b, warp, cv2.MOTION_EUCLIDEAN,
                                        crit, None, 5)
    except cv2.error as e:
        raise SystemExit(
            "No se pudo alinear (SIFT y ECC fallaron). Comprueba que las "
            "imágenes se solapan. Detalle: {}".format(e))

    S = np.array([[work_scale, 0, 0], [0, work_scale, 0], [0, 0, 1]], np.float64)
    S_inv = np.array([[1.0 / work_scale, 0, 0], [0, 1.0 / work_scale, 0],
                      [0, 0, 1]], np.float64)
    M_full = S_inv.dot(np.vstack([warp, [0, 0, 1]])).dot(S)
    M = M_full[:2, :].astype(np.float64)

    scale, rot, tx, ty = _similarity_params(M)
    info = {
        "method": "ecc",
        "matches": 0,
        "inliers": 0,
        "inlier_ratio": 0.0,
        "rms_px": float("nan"),
        "scale": scale,
        "rotation_deg": rot,
        "tx": tx,
        "ty": ty,
        "ecc_cc": float(cc),
    }
    if verbose:
        print("    ECC: cc={:.4f} escala={:.4f} rot={:+.3f}°".format(cc, scale, rot))
    return M, info


def union_canvas(shapes, transforms):
    """Lienzo que contiene todas las imágenes transformadas.

    Devuelve (offset_x, offset_y, width, height): la traslación que hay que
    aplicar después de cada ``M`` para que todo quede en coordenadas positivas.
    """
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for (h, w), M in zip(shapes, transforms):
        corners = np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]],
                           dtype=np.float64).T
        pts = M.dot(corners)
        minx = min(minx, pts[0].min())
        maxx = max(maxx, pts[0].max())
        miny = min(miny, pts[1].min())
        maxy = max(maxy, pts[1].max())
    # minx/miny negativos -> desplazar
    off_x = -minx
    off_y = -miny
    width = int(math.ceil(maxx - minx))
    height = int(math.ceil(maxy - miny))
    return off_x, off_y, width, height


def warp_image(img, M, off, out_shape, flags=cv2.INTER_LANCZOS4):
    """Aplica M + traslación de lienzo y devuelve (imagen, máscara válida)."""
    h, w = out_shape
    T = np.array([[1, 0, off[0]], [0, 1, off[1]]], dtype=np.float64)
    M2 = T.dot(np.vstack([M, [0, 0, 1]]))[:2, :].astype(np.float32)
    out = cv2.warpAffine(img, M2, (w, h), flags=flags,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    ones = np.ones(img.shape, np.float32)
    mask = cv2.warpAffine(ones, M2, (w, h), flags=cv2.INTER_NEAREST,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.5
    return out, mask


# ──────────────────────────────────────────────────────────────────────────────
# Refinado local
# ──────────────────────────────────────────────────────────────────────────────

def _fill_field(vals, weights, sigma):
    """Interpola un campo disperso con un suavizado gaussiano normalizado."""
    num = cv2.GaussianBlur(vals * weights, (0, 0), sigma)
    den = cv2.GaussianBlur(weights, (0, 0), sigma)
    out = np.zeros_like(num)
    good = den > 1e-6
    out[good] = num[good] / den[good]
    return out


def local_refine(ref, mov, valid, tile=128, step=None, max_shift=48.0,
                 resp_min=0.08, verbose=True):
    """Refina la alineación con correlación de fase por teselas.

    ``mov`` ya está transformada a coordenadas de la referencia. Se mide el
    desplazamiento residual de cada tesela y se aplica un campo suavizado.

    Devuelve (imagen_refinada, dx, dy, stats) con dx/dy en píxeles a resolución
    completa. ``phaseCorrelate(ref, mov)`` devuelve el desplazamiento de ``mov``
    respecto de ``ref``, así que para alinear se muestrea ``mov`` en (x+dx, y+dy).
    """
    if tile is None or tile < 32:
        return mov, None, None, {"enabled": False}

    step = step or max(16, tile // 2)
    h, w = ref.shape
    ys = list(range(tile // 2, h - tile // 2 + 1, step))
    xs = list(range(tile // 2, w - tile // 2 + 1, step))
    if not ys or not xs:
        return mov, None, None, {"enabled": False}

    gx = np.zeros((len(ys), len(xs)), np.float32)
    gy = np.zeros_like(gx)
    gw = np.zeros_like(gx)
    win = cv2.createHanningWindow((tile, tile), cv2.CV_32F)

    ref_f = ref.astype(np.float32)
    mov_f = mov.astype(np.float32)
    used = 0
    for i, y in enumerate(ys):
        y0, y1 = y - tile // 2, y + tile // 2
        for j, x in enumerate(xs):
            x0, x1 = x - tile // 2, x + tile // 2
            if not valid[y0:y1, x0:x1].all():
                continue
            a = ref_f[y0:y1, x0:x1]
            b = mov_f[y0:y1, x0:x1]
            if a.std() < 50 or b.std() < 50:
                continue
            # phaseCorrelate modifica sus entradas in situ (aplica la ventana),
            # así que hay que pasar copias o se corrompe la imagen entera.
            (dx, dy), resp = cv2.phaseCorrelate(a.copy(), b.copy(), win)
            if resp < resp_min or math.hypot(dx, dy) > max_shift:
                continue
            gx[i, j] = dx
            gy[i, j] = dy
            gw[i, j] = resp
            used += 1

    if used < 8:
        if verbose:
            print("    Refinado local: solo {} teselas válidas; se omite".format(used))
        return mov, None, None, {"enabled": False, "tiles": used}

    sigma = max(2.0, step * 1.5)
    fx = _fill_field(gx, gw, sigma)
    fy = _fill_field(gy, gw, sigma)

    # Rejilla -> resolución completa. Los centros de tesela están en (xs, ys);
    # cv2.resize interpola entre las esquinas de la rejilla, que es una buena
    # aproximación al muestrear el campo suavizado.
    fx_full = cv2.resize(fx, (w, h), interpolation=cv2.INTER_LINEAR)
    fy_full = cv2.resize(fy, (w, h), interpolation=cv2.INTER_LINEAR)
    cov = cv2.resize((gw > 0).astype(np.float32), (w, h),
                     interpolation=cv2.INTER_LINEAR)
    fx_full[cov < 0.05] = 0.0
    fy_full[cov < 0.05] = 0.0

    mapx, mapy = np.meshgrid(np.arange(w, dtype=np.float32),
                             np.arange(h, dtype=np.float32))
    out = cv2.remap(mov_f, mapx + fx_full, mapy + fy_full,
                    interpolation=cv2.INTER_LANCZOS4,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    mag = np.hypot(fx, fy)[gw > 0]
    stats = {
        "enabled": True,
        "tiles": used,
        "tile": tile,
        "step": step,
        "shift_median_px": float(np.median(mag)),
        "shift_p95_px": float(np.percentile(mag, 95)),
        "shift_max_px": float(mag.max()),
    }
    if verbose:
        print("    Refinado local: {} teselas  mediana={:.2f}px  p95={:.2f}px".format(
            used, stats["shift_median_px"], stats["shift_p95_px"]))
    return out, fx_full, fy_full, stats


# ──────────────────────────────────────────────────────────────────────────────
# Normalización y apilado
# ──────────────────────────────────────────────────────────────────────────────

def normalize_gain(ref, mov, mask):
    """Escala ``mov`` para que la mediana del disco coincida con la de ``ref``.

    Solo ganancia (multiplicativa): no toca el nivel de fondo, que en un recon
    ya está a cero y un offset artificial crearía un escalón en el mosaico.
    """
    if mask.sum() < 1000:
        return mov, 1.0
    r = float(np.median(ref[mask]))
    m = float(np.median(mov[mask]))
    if m <= 0:
        return mov, 1.0
    gain = r / m
    return mov * gain, gain


def stack_images(images, masks, method="sigma", sigma=3.0):
    """Apila una lista de imágenes 2D con máscaras válidas.

    ``mean``: media de los píxeles válidos.
    ``median``: mediana de los píxeles válidos.
    ``sigma``: recorte sigma sobre la mediana (MAD) y media de los inliers.

    Las zonas sin datos quedan a 0.
    """
    n = len(images)
    if n == 0:
        raise ValueError("No hay imágenes que apilar")
    if n == 1:
        return images[0] * masks[0]

    stack = np.stack([im.astype(np.float32) for im in images], axis=0)
    valid = np.stack([m.astype(bool) for m in masks], axis=0)
    cnt = valid.sum(axis=0)

    stack_masked = np.where(valid, stack, np.nan)

    # np.nanmedian avisa con RuntimeWarning en las columnas sin ningún píxel
    # válido (esquinas del lienzo); es esperado y no es un error.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if method == "mean":
            out = np.nanmean(stack_masked, axis=0)
        elif method == "median":
            out = np.nanmedian(stack_masked, axis=0)
        else:
            med = np.nanmedian(stack_masked, axis=0)
            mad = np.nanmedian(np.abs(stack_masked - med), axis=0)
            scale = 1.4826 * mad
            ok = np.abs(stack_masked - med) <= sigma * np.maximum(scale, 1e-6)
            ok &= valid
            stack2 = np.where(ok, stack, np.nan)
            out = np.nanmean(stack2, axis=0)
            # Si el recorte dejó una zona sin píxeles, usar la mediana.
            bad = ~np.isfinite(out) & (cnt > 0)
            out[bad] = med[bad]

    out = np.where(np.isfinite(out) & (cnt > 0), out, 0.0)
    return out.astype(np.float32)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Pre-alinea y apila reconstrucciones SHG de una fila.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--inputs", nargs="+", required=True,
                   help="TIF/PNG/FITS de la misma fila, en orden.")
    p.add_argument("--output", required=True, help="TIF de salida (16 bits).")
    p.add_argument("--json", default=None,
                   help="Informe JSON (por defecto, junto al TIF).")
    p.add_argument("--ref", type=int, default=0,
                   help="Índice de la imagen de referencia.")
    p.add_argument("--method", choices=["sift", "ecc"], default="sift",
                   help="Método de alineación global.")
    p.add_argument("--work-scale", type=float, default=0.5,
                   help="Factor de reducción para detectar puntos.")
    p.add_argument("--max-features", type=int, default=12000)
    p.add_argument("--ratio", type=float, default=0.8,
                   help="Ratio test de Lowe.")
    p.add_argument("--ransac-px", type=float, default=2.0,
                   help="Umbral RANSAC en píxeles de la imagen reducida.")
    p.add_argument("--canvas", choices=["union", "ref"], default="union",
                   help="Lienzo de salida: unión de todas o el de la referencia.")
    p.add_argument("--local-tile", type=int, default=0,
                   help="Tesela del refinado local en px; 0 = desactivado.")
    p.add_argument("--local-step", type=int, default=0,
                   help="Paso entre teselas; 0 = tile/2.")
    p.add_argument("--max-local-shift", type=float, default=48.0,
                   help="Desplazamiento residual máximo aceptado por tesela (px).")
    p.add_argument("--local-response", type=float, default=0.08,
                   help="Respuesta mínima de phaseCorrelate.")
    p.add_argument("--stack", choices=["mean", "median", "sigma"],
                   default="sigma", help="Método de apilado.")
    p.add_argument("--sigma", type=float, default=3.0,
                   help="Umbral de recorte sigma.")
    p.add_argument("--normalize", choices=["gain", "none"], default="gain",
                   help="Igualado de brillo por la mediana del solape.")
    p.add_argument("--preview", default=None,
                   help="PNG de vista previa opcional.")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    t_start = time.time()

    if not args.inputs:
        raise SystemExit("No hay entradas.")
    for path in args.inputs:
        if not os.path.isfile(path):
            raise SystemExit("No existe: {}".format(path))
    if not (0 <= args.ref < len(args.inputs)):
        raise SystemExit("--ref fuera de rango (0..{}).".format(len(args.inputs) - 1))

    log = (lambda *a: None) if args.quiet else print

    log("=" * 78)
    log("Pre-alineado y apilado de recon")
    log("=" * 78)
    log("Imágenes: {}".format(len(args.inputs)))
    for i, p in enumerate(args.inputs):
        log("  [{}] {}{}".format(i, p, "  (ref)" if i == args.ref else ""))

    images = []
    for p in args.inputs:
        images.append(load_image(p))
    shapes = [im.shape for im in images]
    log("Tamaños: {}".format(", ".join("{}x{}".format(w, h) for h, w in shapes)))
    if len(set(shapes)) > 1:
        log("  (tamaños distintos: el lienzo será su unión)")

    ref_idx = args.ref
    ref = images[ref_idx]

    # ── 1. Transformaciones globales ─────────────────────────────────────────
    log("\n1) Alineación global ({})".format(args.method))
    transforms = []
    infos = []
    for i, img in enumerate(images):
        if i == ref_idx:
            M = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
            info = {"method": "identity", "matches": 0, "inliers": 0,
                    "inlier_ratio": 1.0, "rms_px": 0.0, "scale": 1.0,
                    "rotation_deg": 0.0, "tx": 0.0, "ty": 0.0}
        else:
            M, info = estimate_global_transform(
                ref, img, work_scale=args.work_scale,
                max_features=args.max_features, ratio=args.ratio,
                ransac_px=args.ransac_px, method=args.method,
                verbose=not args.quiet)
        transforms.append(M)
        infos.append(info)
        if i == ref_idx:
            log("  [{}] referencia".format(i))
        else:
            log("  [{}] escala={:.5f} rot={:+.3f}°  t=({:+.1f},{:+.1f})px  "
                "inliers={}/{}  rms={:.2f}px".format(
                    i, info["scale"], info["rotation_deg"], info["tx"],
                    info["ty"], info["inliers"], info["matches"], info["rms_px"]))
            if info["inliers"] < 12:
                log("      AVISO: pocos inliers; revisa el solape o usa --method ecc")

    # ── 2. Lienzo común ──────────────────────────────────────────────────────
    if args.canvas == "ref":
        off = (0.0, 0.0)
        out_h, out_w = ref.shape
    else:
        off_x, off_y, out_w, out_h = union_canvas(shapes, transforms)
        off = (off_x, off_y)
    log("\n2) Lienzo: {}x{} (offset {:.1f},{:.1f})".format(
        out_w, out_h, off[0], off[1]))

    warped = []
    masks = []
    for i, img in enumerate(images):
        w, m = warp_image(img, transforms[i], off, (out_h, out_w))
        warped.append(w)
        masks.append(m)

    # ── 3. Refinado local ────────────────────────────────────────────────────
    local_stats = []
    if args.local_tile and args.local_tile >= 32:
        log("\n3) Refinado local (tesela {}px, paso {})".format(
            args.local_tile, args.local_step or args.local_tile // 2))
        refined = []
        for i in range(len(warped)):
            if i == ref_idx:
                refined.append(warped[i])
                local_stats.append({"enabled": False, "reference": True})
                continue
            log("  [{}]".format(i))
            out, fx, fy, st = local_refine(
                warped[ref_idx], warped[i], masks[ref_idx] & masks[i],
                tile=args.local_tile, step=args.local_step or None,
                max_shift=args.max_local_shift, resp_min=args.local_response,
                verbose=not args.quiet)
            refined.append(out)
            local_stats.append(st)
        warped = refined
    else:
        log("\n3) Refinado local: desactivado")
        local_stats = [{"enabled": False} for _ in warped]

    # Recalcular las máscaras tras el refinado (el borde puede haberse movido).
    masks = []
    for i, img in enumerate(images):
        ones = np.ones(img.shape, np.float32)
        T = np.array([[1, 0, off[0]], [0, 1, off[1]]], dtype=np.float64)
        M2 = T.dot(np.vstack([transforms[i], [0, 0, 1]]))[:2, :].astype(np.float32)
        m = cv2.warpAffine(ones, M2, (out_w, out_h), flags=cv2.INTER_NEAREST,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.5
        masks.append(m)

    # ── 4. Normalización de brillo ───────────────────────────────────────────
    gains = [1.0] * len(warped)
    if args.normalize == "gain":
        log("\n4) Normalización de brillo (mediana del solape)")
        for i in range(len(warped)):
            if i == ref_idx:
                continue
            common = masks[ref_idx] & masks[i]
            if common.sum() > 100000:
                # Usar solo el interior del disco evita que el borde mande.
                lo, hi = np.percentile(warped[ref_idx][common], [20, 99])
                interior = common & (warped[ref_idx] > lo + 0.15 * (hi - lo))
                use = interior if interior.sum() > 50000 else common
            else:
                use = common
            warped[i], g = normalize_gain(warped[ref_idx], warped[i], use)
            gains[i] = g
            log("  [{}] ganancia={:.4f}".format(i, g))

    # ── 5. Apilado ───────────────────────────────────────────────────────────
    log("\n5) Apilado ({}{})".format(
        args.stack, ", sigma={}".format(args.sigma) if args.stack == "sigma" else ""))
    stacked = stack_images(warped, masks, method=args.stack, sigma=args.sigma)

    # Máscara final (para el informe y la vista previa).
    final_mask = np.zeros(stacked.shape, bool)
    for m in masks:
        final_mask |= m

    # ── 6. Escritura ─────────────────────────────────────────────────────────
    save_tiff(args.output, stacked)
    log("  Guardado: {}".format(args.output))
    if args.preview:
        save_preview(args.preview, stacked)
        log("  Vista previa: {}".format(args.preview))

    json_path = args.json
    if json_path is None:
        json_path = os.path.splitext(args.output)[0] + ".json"
    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "tools/align_stack_recon.py",
        "inputs": [os.path.abspath(p) for p in args.inputs],
        "reference_index": ref_idx,
        "canvas": {"width": int(out_w), "height": int(out_h),
                   "offset_x": float(off[0]), "offset_y": float(off[1])},
        "params": {
            "method": args.method,
            "work_scale": args.work_scale,
            "max_features": args.max_features,
            "ratio": args.ratio,
            "ransac_px": args.ransac_px,
            "local_tile": args.local_tile,
            "local_step": args.local_step,
            "max_local_shift": args.max_local_shift,
            "stack": args.stack,
            "sigma": args.sigma,
            "normalize": args.normalize,
        },
        "images": [
            {
                "path": os.path.abspath(p),
                "global": infos[i],
                "local": local_stats[i],
                "gain": gains[i],
            }
            for i, p in enumerate(args.inputs)
        ],
        "output": os.path.abspath(args.output),
        "elapsed_s": round(time.time() - t_start, 2),
    }
    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    log("  Informe: {}".format(json_path))

    # ── 7. Resumen / criterios de aceptación ─────────────────────────────────
    log("\n" + "=" * 78)
    log("Resumen")
    log("=" * 78)
    worst_rms = max((i["rms_px"] for i in infos if np.isfinite(i["rms_px"])),
                    default=0.0)
    min_inl = min((i["inliers"] for i in infos[1:]), default=0)
    if worst_rms > 3.0:
        log("AVISO: residual global {:.2f}px > 3px. La captura tiene "
            "desplazamientos/rotaciones grandes o el solape es escaso.".format(worst_rms))
    if len(infos) > 1 and min_inl < 50:
        log("AVISO: algún par tiene menos de 50 inliers; revisa los datos.")
    if not (worst_rms > 3.0 or (len(infos) > 1 and min_inl < 50)):
        log("Alineación dentro de criterios (rms máx {:.2f}px, inliers mín {}).".format(
            worst_rms, min_inl))
    log("Tiempo: {:.1f}s".format(time.time() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
