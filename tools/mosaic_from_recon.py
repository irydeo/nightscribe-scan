#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stitch the per-row stacks into a mosaic by translation.

Purpose
-------
Typical input: the TIFs produced by ``tools/align_stack_recon.py`` (one per row)
or JSol'Ex stacks. The composition is a **translation** of each panel onto a
common canvas, with brightness matching and feather blending. It does not
rescale panels: if two rows have different scales, fix that earlier (capture or
common geometry), not here.

Why translation only
--------------------
The automatic mosaic tends to rescale one row and normalise brightness, adding
a second interpolation on top of an already anisotropic recon. That is what
makes it look worse than the individual frames. Doing the translation here,
explicitly, keeps the seam clean and the geometry auditable.

Recon anisotropy
----------------
The SHG recon is not isotropic: the slit axis (Y) comes out compressed relative
to the scan axis (X) because the slit image is magnified differently from what
the mount rate sets. ``--aspect auto`` measures X/Y on panels whose disk is
complete and stretches Y by that factor, so the disk is round and the Dec
offset can be converted with the **same** plate scale in X and Y. Without it
the mosaic comes out stretched and the Dec offsets do not line up.

How each panel is placed
------------------------
1. ``--offsets dx,dy`` in pixels, one per image. The reliable path: the measured
   displacement (from the capture log or a previous alignment).
2. ``--dec-offsets`` (arcsec from the disk centre, positive = north) +
   ``--pixel-scale`` (arcsec/px, **after** the anisotropy correction). Use it
   when the panels are disk-centred (e.g. JSol'Ex stacks with autocrop).
3. If neither is given, panels are overlaid at (0,0).

``--auto`` refines with phase correlation on the overlap, but only accepts the
correction when the response is high (``--min-response``); with decorrelated
granulation or few spots the response is low and it is better to leave the
nominal position alone.

Warnings
--------
* If panel disk diameters differ by more than ``--scale-tol`` (2% by default) it
  warns: a real scale difference is not fixed by translation and will leave a
  seam.
* ``--auto-scale`` / ``--scales`` correct it **explicitly**, scaling each panel
  about its own disk centre (the offsets stay valid). It is opt-in and adds a
  second interpolation, hence the warning.
* If the vertical residual after refinement exceeds 3 px it warns.

Usage
-----
With measured offsets (recommended)::

    python3 tools/mosaic_from_recon.py \
        --inputs stacks/row1_stacked.tif stacks/row2_stacked.tif \
        --offsets 0,0 142,-1342 \
        --output mosaic/mosaic.tif --preview mosaic/mosaic.png --crop

With the nominal Dec (disk-centred panels)::

    python3 tools/mosaic_from_recon.py \
        --inputs jsol/00_stacked.tif jsol/01_stacked.tif \
        --dec-offsets +630 -630 --pixel-scale 0.4257 \
        --output mosaic/mosaic.tif

Correcting a scale difference between rows (last resort)::

    python3 tools/mosaic_from_recon.py \
        --inputs stacks/row1.tif stacks/row2.tif \
        --offsets 0,0 142,-1342 --auto-scale --scale-tol 0.005 \
        --output mosaic/mosaic.tif

Options
-------
--inputs       one TIF per row, in order (required)
--output       output TIF (required)
--json         JSON report (default: next to the TIF)
--preview      optional PNG for inspection
--offsets      dx,dy per image in pixels (highest priority)
--dec-offsets  Dec of each row in arcsec from the centre (needs --pixel-scale)
--pixel-scale  plate scale in arcsec/px (after the anisotropy correction)
--dec-sign     1 | -1, Dec -> Y direction (default 1)
--no-auto      do not refine the position with the overlap
--min-response minimum correlation response to apply the auto correction (0.15)
--aspect       auto | none | <X/Y> (default auto)
--scales       explicit scale factor per panel (after anisotropy)
--auto-scale   measure disk diameters and scale each panel to the reference
--scale-axis   both | x | y axis of the rescale (default both)
--scale-tol    with --auto-scale, do not apply below this; also warning level
--normalize    gain | none (default gain)
--blend        feather | none (default feather)
--feather      blend ramp width in px (default 80)
--crop         trim the empty border of the final canvas
--quiet        no progress output

What to check
-------------
- Disk diameters differing by more than 2%: translation will leave a seam
  (use ``--auto-scale``/``--scales`` or fix it in capture).
- ``resp`` < 0.15: the auto correction is not applied (good).
- Vertical residual > 3 px: revisit the offset.

JSON report
-----------
``aspect_y_stretch``, ``disk_diameters_px``, ``scales``, ``scale_axis``,
``scale_pads_px``, ``positions_px``, ``gains``, ``measured_overlap``, ``canvas``.
Enough to reproduce the mosaic exactly.

NightScribe link
----------------
Use ``dec_offset_measured_arcsec`` from NightScribe's ``scan_log.csv`` for the
``--offsets``/``--dec-offsets``, and ``slit``/``S_Y`` from ``session_meta.json``
for the anisotropy.

Dependencies
------------
numpy, opencv-python. Optional: astropy (FITS), tifffile (TIF; PIL fallback).

See also
--------
- ``GUIA_USUARIO.md`` / ``USER_GUIDE.md`` — user guide, "The tools" and
  "Deep dive" sections.
- ``tools/align_stack_recon.py`` — stacks the frames of each row.
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
# Input / output — shared with align_stack_recon.py
# ──────────────────────────────────────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _imgio import load_image, save_tiff, save_preview  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Máscara de contenido y anisotropía
# ──────────────────────────────────────────────────────────────────────────────

def content_mask(img, dilate=60):
    """Máscara del disco: separa el Sol del fondo del recon.

    El fondo de un recon no es cero (suele estar en ~1500-2000 ADU) y no es
    plano: si se usa Otsu a secas, en las zonas donde el fondo sube se cuela
    como si fuera disco. Aquí se fija un umbral relativo al fondo y al pico
    del disco, se toma el componente mayor, se rellenan los huecos y se
    dilata para recuperar el limbo (tenue por el oscurecimiento hacia el
    borde).
    """
    vals = img.ravel()
    bg = float(np.percentile(vals, 20))
    peak = float(np.percentile(vals, 99.5))
    if peak <= bg + 1.0:
        return np.ones(img.shape, bool)
    for frac in (0.5, 0.3, 0.15):
        thr = bg + frac * (peak - bg)
        m = (img > thr).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        if n <= 1:
            continue
        k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[k, cv2.CC_STAT_AREA] < 1000:
            continue
        m = (lab == k).astype(np.uint8)

        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        filled = np.zeros_like(m)
        if cnts:
            cv2.drawContours(filled, cnts, -1, 1, thickness=cv2.FILLED)
        if dilate > 0:
            filled = cv2.dilate(filled, np.ones((3, 3), np.uint8),
                                iterations=max(1, int(round(dilate / 3.0))))
        return filled.astype(bool)
    return np.ones(img.shape, bool)


def estimate_aspect(masks, border=3):
    """Cociente X/Y medido en los discos que no tocan el borde.

    Devuelve ``(aspecto, n)``. ``aspecto > 1`` significa Y comprimido.
    """
    vals = []
    for m in masks:
        ys, xs = np.nonzero(m)
        if len(ys) == 0:
            continue
        h, w = m.shape
        x0, x1 = xs.min(), xs.max()
        y0, y1 = ys.min(), ys.max()
        if (x0 <= border or y0 <= border or
                x1 >= w - 1 - border or y1 >= h - 1 - border):
            continue
        dy = float(y1 - y0 + 1)
        if dy <= 0:
            continue
        vals.append(float(x1 - x0 + 1) / dy)
    if not vals:
        return 1.0, 0
    vals.sort()
    return float(vals[len(vals) // 2]), len(vals)


def stretch_y(img, factor):
    """Estira el eje Y por ``factor`` manteniendo X y el origen."""
    if abs(factor - 1.0) < 1e-6:
        return img
    h, w = img.shape
    return cv2.resize(img, (w, max(1, int(round(h * factor)))),
                      interpolation=cv2.INTER_LANCZOS4)


def scale_about_disk(img, mask, fx, fy):
    """Escala la imagen por (fx, fy) **alrededor del centro del disco**.

    Mantener el centro fijo es lo que permite seguir usando los mismos
    ``--offsets``: al escalar sobre el propio centro, el disco no se mueve.
    El lienzo se amplía si hace falta para no recortar el disco escalado.
    Devuelve ``(imagen, máscara, pad_x, pad_y)``; el desplazamiento del panel
    hay que reducirlo en (pad_x, pad_y) para que el centro quede donde estaba.
    """
    if abs(fx - 1.0) < 1e-6 and abs(fy - 1.0) < 1e-6:
        return img, mask, 0, 0
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return img, mask, 0, 0
    h, w = img.shape
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(ys.min()), float(ys.max())
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)

    nx0 = cx + fx * (x0 - cx)
    nx1 = cx + fx * (x1 - cx)
    ny0 = cy + fy * (y0 - cy)
    ny1 = cy + fy * (y1 - cy)
    pad_l = max(0, int(math.ceil(-nx0)))
    pad_t = max(0, int(math.ceil(-ny0)))
    pad_r = max(0, int(math.ceil(nx1 - (w - 1))))
    pad_b = max(0, int(math.ceil(ny1 - (h - 1))))
    out_w, out_h = w + pad_l + pad_r, h + pad_t + pad_b

    M = np.array([[fx, 0.0, pad_l + cx - fx * cx],
                  [0.0, fy, pad_t + cy - fy * cy]], np.float32)
    out = cv2.warpAffine(img, M, (out_w, out_h), flags=cv2.INTER_LANCZOS4,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    m = cv2.warpAffine(mask.astype(np.float32), M, (out_w, out_h),
                       flags=cv2.INTER_NEAREST,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.5
    return out, m, pad_l, pad_t


# ──────────────────────────────────────────────────────────────────────────────
# Colocación y mezcla
# ──────────────────────────────────────────────────────────────────────────────

def place(img, mask, dx, dy, canvas_shape, off_x, off_y):
    """Coloca ``img`` (solo donde ``mask``) con su esquina en (dx,dy)."""
    out = np.zeros(canvas_shape, np.float32)
    out_m = np.zeros(canvas_shape, bool)
    h, w = img.shape
    x0 = int(round(dx - off_x))
    y0 = int(round(dy - off_y))
    sx0 = max(0, -x0)
    sy0 = max(0, -y0)
    sx1 = min(w, canvas_shape[1] - x0)
    sy1 = min(h, canvas_shape[0] - y0)
    if sx1 <= sx0 or sy1 <= sy0:
        return out, out_m
    sub_img = img[sy0:sy1, sx0:sx1]
    sub_m = mask[sy0:sy1, sx0:sx1]
    dst = out[y0 + sy0:y0 + sy1, x0 + sx0:x0 + sx1]
    dst[sub_m] = sub_img[sub_m]
    out_m[y0 + sy0:y0 + sy1, x0 + sx0:x0 + sx1] = sub_m
    return out, out_m


def feather_weights(mask, feather):
    """Peso de mezcla: 1 en el interior, rampa a 0 en el borde del disco."""
    if feather <= 0:
        return mask.astype(np.float32)
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    w = np.clip(dist / float(feather), 0.0, 1.0)
    w[~mask] = 0.0
    return w.astype(np.float32)


def refine_shift(ref, mov, mask_ref, mask_mov, max_shift=300.0):
    """Residuo de ``mov`` respecto a ``ref`` en la ventana con más textura.

    Devuelve ``(dx, dy, resp)`` o ``None``. Se elige la mayor ventana cuadrada
    centrada en la zona de solape con textura suficiente, porque el centro del
    disco (donde la distancia al borde es máxima) suele ser liso y da una
    correlación falsa.
    """
    overlap = mask_ref & mask_mov
    if overlap.sum() < 20000:
        return None
    dist = cv2.distanceTransform(overlap.astype(np.uint8), cv2.DIST_L2, 5)
    h, w = overlap.shape
    size = int(min(1024, 2 * dist.max()))
    size = (size // 32) * 32
    if size < 256:
        return None
    # Probar varias ventanas candidatas y quedarse con la de más contraste.
    best = None
    cy0, cx0 = np.unravel_index(int(np.argmax(dist)), dist.shape)
    for frac in (0.0, 0.35, 0.7):
        cy = int(cy0 * (1 - frac) + h * 0.5 * frac)
        cx = int(cx0 * (1 - frac) + w * 0.5 * frac)
        ya = max(0, min(h - size, cy - size // 2))
        xa = max(0, min(w - size, cx - size // 2))
        if overlap[ya:ya + size, xa:xa + size].mean() < 0.98:
            continue
        a = ref[ya:ya + size, xa:xa + size]
        b = mov[ya:ya + size, xa:xa + size]
        if a.std() < 50 or b.std() < 50:
            continue
        win = cv2.createHanningWindow((size, size), cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(a.copy(), b.copy(), win)
        if not np.isfinite(dx) or math.hypot(dx, dy) > max_shift:
            continue
        score = min(float(a.std()), float(b.std()))
        if best is None or score > best[0]:
            best = (score, dx, dy, resp)
    if best is None:
        return None
    return float(best[1]), float(best[2]), float(best[3])


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Compone un mosaico por traslación de los stacks de fila.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--inputs", nargs="+", required=True,
                   help="Un TIF por fila, en orden (norte -> sur o al revés).")
    p.add_argument("--output", required=True)
    p.add_argument("--json", default=None)
    p.add_argument("--preview", default=None)

    g = p.add_argument_group("posicionamiento")
    g.add_argument("--offsets", nargs="+", default=None,
                   help="dx,dy en píxeles por imagen (medidos). Prioridad.")
    g.add_argument("--dec-offsets", nargs="+", type=float, default=None,
                   help="Dec de cada fila en arcsec respecto al centro "
                        "(positivo = norte). Necesita --pixel-scale.")
    g.add_argument("--pixel-scale", type=float, default=None,
                   help="Escala de placa en arcsec/px (tras la anisotropía).")
    g.add_argument("--dec-sign", type=int, choices=[1, -1], default=1,
                   help="Signo de la relación Dec -> Y del sensor.")
    g.add_argument("--no-auto", dest="auto", action="store_false",
                   help="No refinar la posición con el solape.")
    g.set_defaults(auto=True)
    g.add_argument("--min-response", type=float, default=0.15,
                   help="Respuesta mínima de la correlación para aplicar la "
                        "corrección automática.")
    g.add_argument("--aspect", default="auto",
                   help="'auto' mide X/Y y estira Y; 'none' no toca; un número "
                        "fuerza el factor (X/Y > 1 = Y comprimido).")

    g = p.add_argument_group("reescalado (opcional)")
    g.add_argument("--scales", nargs="+", type=float, default=None,
                   help="Factor de escala por panel, tras la anisotropía "
                        "(uno por imagen; la referencia suele ser 1.0).")
    g.add_argument("--auto-scale", action="store_true",
                   help="Mide el diámetro de cada disco y escala cada panel "
                        "para igualarlo al de la referencia.")
    g.add_argument("--scale-axis", choices=["both", "x", "y"], default="both",
                   help="Eje del reescalado: 'x' para una diferencia de tasa "
                        "de barrido, 'both' para una diferencia de aumento.")
    g.add_argument("--scale-tol", type=float, default=0.02,
                   help="Con --auto-scale, no aplica si la diferencia de "
                        "diámetro es menor; también umbral del aviso.")

    g = p.add_argument_group("radiometría y mezcla")
    g.add_argument("--normalize", choices=["gain", "none"], default="gain",
                   help="Iguala el brillo por la mediana del solape del disco.")
    g.add_argument("--blend", choices=["feather", "none"], default="feather")
    g.add_argument("--feather", type=float, default=80.0,
                   help="Ancho de la rampa de mezcla en píxeles.")
    g.add_argument("--crop", action="store_true",
                   help="Recorta el borde sin datos del lienzo final.")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def parse_offsets(values):
    out = []
    for v in values:
        parts = v.replace(";", ",").split(",")
        if len(parts) != 2:
            raise SystemExit("--offsets espera 'dx,dy'; recibido: {}".format(v))
        out.append((float(parts[0]), float(parts[1])))
    return out


def main(argv=None):
    args = parse_args(argv)
    t0 = time.time()
    log = (lambda *a: None) if args.quiet else print

    n = len(args.inputs)
    for path in args.inputs:
        if not os.path.isfile(path):
            raise SystemExit("No existe: {}".format(path))

    log("=" * 78)
    log("Mosaico a partir de stacks de fila")
    log("=" * 78)

    images = []
    masks = []
    for i, path in enumerate(args.inputs):
        img = load_image(path)
        m = content_mask(img)
        images.append(img)
        masks.append(m)
        ys, xs = np.nonzero(m)
        if len(ys):
            dw, dh = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
        else:
            dw, dh = 0, 0
        log("  [{}] {}  {}x{}  disco {}x{} px".format(
            i, path, img.shape[1], img.shape[0], dw, dh))

    # ── Anisotropía común del recon ──────────────────────────────────────────
    if args.aspect == "auto":
        y_factor, n_asp = estimate_aspect(masks)
        if n_asp == 0:
            y_factor = 1.0
            log("\nAVISO: ningún disco completo para medir X/Y; no se "
                "corrige la anisotropía.")
    elif args.aspect == "none":
        y_factor, n_asp = 1.0, 0
    else:
        y_factor, n_asp = float(args.aspect), -1

    if abs(y_factor - 1.0) > 1e-6:
        log("\nAnisotropía: X/Y = {:.4f} ({} disco(s)); se estira Y."
            .format(y_factor, n_asp if n_asp >= 0 else "forzada"))
        images = [stretch_y(img, y_factor) for img in images]
        masks = [stretch_y(m.astype(np.float32), y_factor) > 0.5
                 for m in masks]

    # Diámetros de disco (eje X, el fiable tras estirar Y) para el aviso y
    # para el reescalado automático.
    diameters = []
    for m in masks:
        ys, xs = np.nonzero(m)
        diameters.append(float(xs.max() - xs.min() + 1) if len(ys) else 0.0)

    # ── Reescalado opcional entre paneles ────────────────────────────────────
    scales = [1.0] * n
    if args.scales is not None:
        if len(args.scales) != n:
            raise SystemExit("--scales necesita {} valores".format(n))
        scales = [float(s) for s in args.scales]
        log("\nReescalado explícito: {} (eje {})".format(
            " ".join("{:.5f}".format(s) for s in scales), args.scale_axis))
        log("  Aviso: el reescalado añade una segunda interpolación.")
    elif args.auto_scale:
        if n > 1 and diameters[0] > 0 and min(diameters) > 0:
            scales = [diameters[0] / d for d in diameters]
            spread = (max(scales) - min(scales)) / max(scales)
            log("\nReescalado automático (diámetros X): {}".format(
                " ".join("{:.1f}".format(d) for d in diameters)))
            log("  factores: {}".format(
                " ".join("{:.5f}".format(s) for s in scales)))
            if spread <= args.scale_tol:
                log("  diferencia {:.2f} % <= {:.2f} %; no se aplica".format(
                    spread * 100.0, args.scale_tol * 100.0))
                scales = [1.0] * n
            else:
                log("  diferencia {:.2f} %; se aplica (eje {})".format(
                    spread * 100.0, args.scale_axis))
                log("  Aviso: el reescalado añade una segunda interpolación.")

    pads = []
    if any(abs(s - 1.0) > 1e-6 for s in scales):
        for i in range(n):
            s = scales[i]
            fx = s if args.scale_axis in ("both", "x") else 1.0
            fy = s if args.scale_axis in ("both", "y") else 1.0
            img, m, pl, pt = scale_about_disk(images[i], masks[i], fx, fy)
            images[i], masks[i] = img, m
            pads.append((pl, pt))
    else:
        pads = [(0, 0)] * n

    # ── Posiciones ───────────────────────────────────────────────────────────
    if args.offsets:
        offs = parse_offsets(args.offsets)
        if len(offs) != n:
            raise SystemExit("--offsets necesita {} pares".format(n))
        log("\nPosiciones explícitas (px):")
    elif args.dec_offsets:
        if len(args.dec_offsets) != n:
            raise SystemExit("--dec-offsets necesita {} valores".format(n))
        if not args.pixel_scale or args.pixel_scale <= 0:
            raise SystemExit("--dec-offsets requiere --pixel-scale")
        log("\nPosiciones por Dec (escala {:.4f}\"/px):".format(args.pixel_scale))
        offs = [(0.0, -args.dec_sign * dec / args.pixel_scale)
                for dec in args.dec_offsets]
    else:
        log("\nAVISO: sin --offsets ni --dec-offsets; los paneles se superponen "
            "en (0,0). Si son filas distintas, falta el desplazamiento en Dec.")
        offs = [(0.0, 0.0) for _ in images]

    # El reescalado amplía el lienzo por arriba/izquierda; compensar el offset
    # para que el centro del disco siga cayendo en el mismo sitio.
    if any(p != (0, 0) for p in pads):
        offs = [(offs[i][0] - pads[i][0], offs[i][1] - pads[i][1])
                for i in range(n)]
        log("\nCompensación del reescalado en los offsets (pad): {}".format(
            " ".join("({},{})".format(pl, pt) for pl, pt in pads)))

    for i, (dx, dy) in enumerate(offs):
        log("  [{}] dx={:+.1f}  dy={:+.1f} px".format(i, dx, dy))

    def canvas_bounds(positions):
        mnx = min(dx for dx, _ in positions)
        mny = min(dy for _, dy in positions)
        mxx = max(dx + img.shape[1] for (dx, _), img in zip(positions, images))
        mxy = max(dy + img.shape[0] for (_, dy), img in zip(positions, images))
        return mnx, mny, int(math.ceil(mxx - mnx)), int(math.ceil(mxy - mny))

    def place_all():
        out_p, out_m = [], []
        for img, m, (dx, dy) in zip(images, masks, offs):
            p, pm = place(img, m, dx, dy, (ch, cw), off_x, off_y)
            out_p.append(p)
            out_m.append(pm)
        return out_p, out_m

    off_x, off_y, cw, ch = canvas_bounds(offs)
    log("\nLienzo: {}x{}".format(cw, ch))
    placed, pmasks = place_all()

    # ── Refinado por correlación de fase ─────────────────────────────────────
    measured = []
    if args.auto and n > 1:
        log("\nRefinado con el solape (respuesta mínima {:.2f}):".format(
            args.min_response))
        changed = False
        for i in range(1, n):
            r = refine_shift(placed[i - 1], placed[i],
                             pmasks[i - 1], pmasks[i])
            if r is None:
                measured.append(None)
                log("  [{}]-[{}]: no medible".format(i - 1, i))
                continue
            dx, dy, resp = r
            measured.append({"dx": dx, "dy": dy, "response": resp})
            log("  [{}]-[{}]: dx={:+.2f} dy={:+.2f} px  resp={:.3f}".format(
                i - 1, i, dx, dy, resp))
            if resp >= args.min_response and math.hypot(dx, dy) < 300.0:
                offs[i] = (offs[i][0] - dx, offs[i][1] - dy)
                changed = True
                log("      -> corregido a dx={:+.1f} dy={:+.1f}".format(
                    offs[i][0], offs[i][1]))
            else:
                log("      -> respuesta baja; se mantiene la posición nominal")
        if changed:
            off_x, off_y, cw, ch = canvas_bounds(offs)
            log("  Lienzo corregido: {}x{}".format(cw, ch))
            placed, pmasks = place_all()

    # ── Igualado de brillo ───────────────────────────────────────────────────
    gains = [1.0] * n
    if args.normalize == "gain":
        log("\nNormalización de brillo (mediana del solape del disco):")
        for i in range(1, n):
            common = pmasks[i - 1] & pmasks[i]
            if common.sum() < 1000:
                log("  [{}] sin solape de disco; ganancia=1".format(i))
                continue
            a = placed[i - 1][common]
            b = placed[i][common]
            a = a[a > 0]
            b = b[b > 0]
            if len(a) < 100 or len(b) < 100:
                continue
            g = float(np.median(a)) / float(np.median(b))
            if not np.isfinite(g) or g <= 0:
                g = 1.0
            gains[i] = g
            placed[i] = placed[i] * g
            log("  [{}] ganancia={:.4f}".format(i, g))

    # ── Mezcla ───────────────────────────────────────────────────────────────
    log("\nMezcla: {}".format(args.blend))
    if args.blend == "none":
        out = np.zeros((ch, cw), np.float32)
        for p, m in zip(placed, pmasks):
            out[m] = p[m]
    else:
        wsum = np.zeros((ch, cw), np.float32)
        acc = np.zeros((ch, cw), np.float64)
        for p, m in zip(placed, pmasks):
            w = feather_weights(m, args.feather) + (m.astype(np.float32) * 1e-3)
            wsum += w
            acc += p.astype(np.float64) * w
        good = wsum > 0
        out = np.zeros((ch, cw), np.float32)
        out[good] = (acc[good] / wsum[good]).astype(np.float32)

    # ── Recorte opcional ─────────────────────────────────────────────────────
    if args.crop:
        ys, xs = np.nonzero(out > 0)
        if len(ys):
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            out = out[y0:y1, x0:x1]
            off_x += x0
            off_y += y0
            cw, ch = out.shape[1], out.shape[0]
            log("  Recortado a {}x{}".format(cw, ch))

    save_tiff(args.output, out)
    log("  Guardado: {}".format(args.output))
    if args.preview:
        save_preview(args.preview, out)
        log("  Vista previa: {}".format(args.preview))

    json_path = args.json or (os.path.splitext(args.output)[0] + ".json")
    report = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "tools/mosaic_from_recon.py",
        "inputs": [os.path.abspath(p) for p in args.inputs],
        "aspect_y_stretch": y_factor,
        "disk_diameters_px": diameters,
        "scales": scales,
        "scale_axis": args.scale_axis,
        "scale_pads_px": [{"x": int(pl), "y": int(pt)} for pl, pt in pads],
        "positions_px": [{"dx": float(dx), "dy": float(dy)} for dx, dy in offs],
        "positions_arcsec": (
            [float(v) for v in args.dec_offsets] if args.dec_offsets else None),
        "pixel_scale": args.pixel_scale,
        "dec_sign": args.dec_sign,
        "gains": gains,
        "measured_overlap": measured,
        "canvas": {"width": int(cw), "height": int(ch),
                   "offset_x": float(off_x), "offset_y": float(off_y)},
        "blend": args.blend,
        "feather": args.feather,
        "output": os.path.abspath(args.output),
        "elapsed_s": round(time.time() - t0, 2),
    }
    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    log("  Informe: {}".format(json_path))

    # ── Criterios ────────────────────────────────────────────────────────────
    log("\n" + "=" * 78)
    if n > 1:
        scaled = any(abs(s - 1.0) > 1e-6 for s in scales)
        dmin, dmax = min(diameters), max(diameters)
        if dmax > 0 and (dmax - dmin) / dmax > args.scale_tol and not scaled:
            log("AVISO: los discos difieren {:.1f} % en diámetro. Una "
                "traslación no corrige una diferencia de escala; arréglala en "
                "la captura/JSol'Ex o usa --auto-scale (añade interpolación)."
                .format((dmax - dmin) / dmax * 100.0))
        if scaled:
            log("Reescalado aplicado: {}".format(
                " ".join("{:.5f}".format(s) for s in scales)))
        worst = max((abs(m["dy"]) for m in measured if m), default=0.0)
        if worst > 3.0:
            log("AVISO: residuo vertical {:.1f}px (>3px).".format(worst))
        else:
            log("Solape dentro de criterios (residuo vertical máx {:.1f}px).".format(
                worst))
    log("Tiempo: {:.1f}s".format(time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
