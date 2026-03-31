"""Matplotlib notebook helpers for volumetric visualization."""

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure


def show_slice(
    volume: np.ndarray,
    axis: int = 0,
    index: int | None = None,
    *,
    ax: Axes | None = None,
    cmap: str = 'gray',
    title: str | None = None,
) -> tuple[Figure, Axes]:
    """Display a single 2D slice from a 3D volume.

    Parameters
    ----------
    volume : np.ndarray
        3D array of shape ``(z, y, x)``.
    axis : int
        Axis to slice along (0=axial, 1=coronal, 2=sagittal).
    index : int | None
        Slice index along *axis*.  ``None`` uses the midpoint.
    ax : Axes | None
        Matplotlib axes to draw on.  Creates a new figure if ``None``.
    cmap : str
        Colormap name.
    title : str | None
        Optional title.

    Returns
    -------
    tuple[Figure, Axes]
    """
    if index is None:
        index = volume.shape[axis] // 2

    slc = np.take(volume, index, axis=axis)

    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=(6, 6))
    else:
        fig = ax.get_figure()

    ax.imshow(slc, cmap=cmap, origin='lower')  # type: ignore[union-attr]
    if title:
        ax.set_title(title)  # type: ignore[union-attr]
    ax.axis('off')  # type: ignore[union-attr]

    return fig, ax  # type: ignore[return-value]


def show_orthogonal(
    volume: np.ndarray,
    *,
    indices: tuple[int | None, int | None, int | None] = (
        None,
        None,
        None,
    ),
    cmap: str = 'gray',
    title: str | None = None,
) -> tuple[Figure, list[Axes]]:
    """Display three orthogonal slices from a 3D volume.

    Parameters
    ----------
    volume : np.ndarray
        3D array of shape ``(z, y, x)``.
    indices : tuple
        Slice indices for each axis.  ``None`` uses midpoints.
    cmap : str
        Colormap name.
    title : str | None
        Optional suptitle.

    Returns
    -------
    tuple[Figure, list[Axes]]
    """
    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    labels = ['Axial', 'Coronal', 'Sagittal']

    for i, (ax, label) in enumerate(zip(axes, labels, strict=False)):
        idx = indices[i] if indices[i] is not None else None
        show_slice(volume, axis=i, index=idx, ax=ax, cmap=cmap, title=label)

    if title:
        fig.suptitle(title, fontsize=14)
    fig.tight_layout()

    return fig, list(axes)
